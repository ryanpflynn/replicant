// Command replicant-agentd is the WS-03 native sidecar (cli-invocation.md §5, ADR-03-001): sole holder of the
// run tokens, loopback auth proxy for the gateway (:7800), MCP (:7801), git (:7802) and inputs (:7803),
// token refresh, heartbeat and self-stop, steering relay, usage accounting, and the uploader's credentials.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/httpx"
	"example.com/replicant/internal/runner/ipc"
	"example.com/replicant/internal/runner/jose"
	"example.com/replicant/internal/runner/sandbox"
)

var version = "1.1.0"

func main() {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "ready":
			// startupProbe: the IPC socket answers /readyz.
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			if err := ipc.New(httpx.Env("REPLICANT_IPC_SOCKET", sandbox.IPCSocket)).Do(ctx, http.MethodGet, "/readyz", nil, nil, nil); err != nil {
				os.Exit(1)
			}
			return
		case "version":
			fmt.Println("replicant-agentd", version)
			return
		}
	}
	log := httpx.Logger("replicant-agentd")
	if err := run(log); err != nil {
		log.Error("fatal", "err", err)
		os.Exit(1)
	}
}

// Agent is agentd's state.
type Agent struct {
	log *slog.Logger
	bs  *sandbox.Bootstrap

	tokMu   sync.RWMutex
	tokens  map[string]string // audience -> token
	expires map[string]time.Time
	claims  api.RunTokenClaims
	// every token and secret held during the run (for redact-known)
	secretsSeen map[string]bool

	evMu            sync.Mutex
	events          []ipc.Event
	evCond          *sync.Cond
	usage           ipc.Usage
	turns           int
	prepared        bool
	finished        bool
	manifestWritten bool
	instructions    []ipc.InstructionsReport // InstructionsLoaded reports (§8c), capped at maxInstructionReports

	steerMu  sync.Mutex
	steering []ipc.SteeringMessage
	consumed []string
	afterSeq int

	hbMu       sync.Mutex
	lastHB     time.Time
	hbLost     bool
	cancelSent bool

	gwMu          sync.Mutex
	gwFailingFrom time.Time
	gwDown        bool

	plan *sandbox.RunPlan

	credsMu sync.Mutex
	creds   *api.AwsContainerCredentials

	client *http.Client
}

func run(log *slog.Logger) error {
	bs, err := sandbox.ReadBootstrap("")
	if err != nil {
		return fmt.Errorf("bootstrap: %w", err)
	}
	a := &Agent{log: log, bs: bs, tokens: map[string]string{}, expires: map[string]time.Time{}, secretsSeen: map[string]bool{},
		client: &http.Client{Timeout: 0}}
	a.evCond = sync.NewCond(&a.evMu)
	if err := a.loadSeed(); err != nil {
		return err
	}
	a.lastHB = time.Now()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	errc := make(chan error, 8)
	serveUnix := func(path string, h http.Handler) {
		_ = os.MkdirAll(filepath.Dir(path), 0o777)
		_ = os.Remove(path)
		l, err := net.Listen("unix", path)
		if err != nil {
			errc <- err
			return
		}
		_ = os.Chmod(path, 0o666)
		errc <- (&http.Server{Handler: h, ReadHeaderTimeout: 10 * time.Second}).Serve(l)
	}
	serveTCP := func(addr string, h http.Handler) {
		errc <- (&http.Server{Addr: addr, Handler: h, ReadHeaderTimeout: 30 * time.Second}).ListenAndServe()
	}
	go serveTCP(sandbox.LoopbackGateway, a.gatewayHandler())
	go serveTCP(sandbox.LoopbackMCP, a.mcpHandler())
	go serveTCP(sandbox.LoopbackGit, a.gitHandler())
	go serveTCP(sandbox.LoopbackInputs, a.inputsHandler())
	go serveUnix(httpx.Env("REPLICANT_UPLOADER_SOCKET", sandbox.UploaderSocket), a.uploaderHandler())
	go serveUnix(httpx.Env("REPLICANT_IPC_SOCKET", sandbox.IPCSocket), a.ipcHandler())

	go a.refreshLoop(ctx)
	go a.heartbeatLoop(ctx)
	go a.steeringLoop(ctx)
	go a.loadPlan(ctx)
	go a.budgetAtStart(ctx)
	log.Info("agentd started", "run_id", a.claims.RunID, "audiences", a.audiences())
	started := time.Now()
	defer a.emitSpan(started)
	select {
	case <-ctx.Done():
		// SIGTERM to the pod: close the run at the shim (cli-invocation.md §5.1 item 6).
		a.closeRun(context.Background())
		return nil
	case err := <-errc:
		return err
	}
}

// loadSeed copies the projected token set into agentd's tmpfs and checks the set is consistent (§5.1 item 1).
func (a *Agent) loadSeed() error {
	seed := httpx.Env("REPLICANT_RUN_TOKEN_SEED_DIR", sandbox.TokenSeedDir)
	dir := httpx.Env("REPLICANT_RUN_TOKEN_DIR", sandbox.TokenDir)
	ents, err := os.ReadDir(seed)
	if err != nil {
		return fmt.Errorf("token seed: %w", err)
	}
	set := map[string]string{}
	for _, e := range ents {
		if e.IsDir() || strings.HasPrefix(e.Name(), ".") {
			continue
		}
		b, err := os.ReadFile(filepath.Join(seed, e.Name()))
		if err != nil {
			return err
		}
		set[e.Name()] = strings.TrimSpace(string(b))
	}
	if len(set) == 0 {
		return errors.New("token seed is empty")
	}
	return a.installTokens(dir, set)
}

func (a *Agent) installTokens(dir string, set map[string]string) error {
	var first *api.RunTokenClaims
	exp := map[string]time.Time{}
	for aud, tok := range set {
		var c api.RunTokenClaims
		if err := jose.UnsafeClaims(tok, &c); err != nil {
			return fmt.Errorf("token %s: %w", aud, err)
		}
		if c.Aud != aud {
			return fmt.Errorf("token file %s carries aud %q", aud, c.Aud)
		}
		if first == nil {
			cc := c
			first = &cc
		} else if c.RunID != first.RunID || c.TenantID != first.TenantID || c.JobID != first.JobID || c.ActingFor != first.ActingFor || c.GrantJti != first.GrantJti {
			return errors.New("token set is inconsistent")
		}
		exp[aud] = time.Unix(c.Exp, 0)
	}
	_ = os.MkdirAll(dir, 0o700)
	for aud, tok := range set {
		p := filepath.Join(dir, aud)
		tmp := p + ".tmp"
		if err := os.WriteFile(tmp, []byte(tok), 0o400); err != nil {
			return err
		}
		if err := os.Rename(tmp, p); err != nil {
			return err
		}
	}
	if legacy := os.Getenv("REPLICANT_RUN_TOKEN_FILE"); legacy != "" && filepath.Dir(legacy) != dir {
		if t, ok := set[api.AudSandbox]; ok {
			_ = os.WriteFile(legacy, []byte(t), 0o400)
		}
	}
	a.tokMu.Lock()
	for aud, tok := range set {
		a.tokens[aud] = tok
		a.expires[aud] = exp[aud]
		a.secretsSeen[tok] = true
	}
	a.claims = *first
	a.tokMu.Unlock()
	return nil
}

func (a *Agent) token(aud string) (string, bool) {
	a.tokMu.RLock()
	defer a.tokMu.RUnlock()
	t, ok := a.tokens[aud]
	return t, ok
}

func (a *Agent) audiences() []string {
	a.tokMu.RLock()
	defer a.tokMu.RUnlock()
	var out []string
	for k := range a.tokens {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func (a *Agent) hasScope(s string) bool {
	a.tokMu.RLock()
	defer a.tokMu.RUnlock()
	for _, sc := range a.claims.Scopes {
		if sc == s {
			return true
		}
	}
	// scopes differ per token; check the runner-sandbox token claims too
	if t, ok := a.tokens[api.AudSandbox]; ok {
		var c api.RunTokenClaims
		if jose.UnsafeClaims(t, &c) == nil {
			for _, sc := range c.Scopes {
				if sc == s {
					return true
				}
			}
		}
	}
	return false
}

func (a *Agent) raise(kind, reason, detail string) {
	a.evMu.Lock()
	defer a.evMu.Unlock()
	a.events = append(a.events, ipc.Event{Seq: len(a.events) + 1, Kind: kind, Reason: reason, Detail: detail, At: time.Now().UTC()})
	a.evCond.Broadcast()
	a.log.Info("ipc event", "kind", kind, "reason", reason)
}

// sandboxCall calls the runner-sandbox listener with the runner-sandbox token.
func (a *Agent) sandboxCall(ctx context.Context, method, path string, in, out any) (int, error) {
	tok, ok := a.token(api.AudSandbox)
	if !ok {
		return 0, errors.New("no runner-sandbox token")
	}
	var body *strings.Reader
	if in != nil {
		b, _ := json.Marshal(in)
		body = strings.NewReader(string(b))
	} else {
		body = strings.NewReader("")
	}
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimSuffix(a.bs.Endpoints.RunnerSandbox, "/")+path, body)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Authorization", "Bearer "+tok)
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := (&http.Client{Timeout: 40 * time.Second}).Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return resp.StatusCode, fmt.Errorf("%s %s: HTTP %d", method, path, resp.StatusCode)
	}
	if out != nil {
		return resp.StatusCode, json.NewDecoder(resp.Body).Decode(out)
	}
	return resp.StatusCode, nil
}

// refreshLoop refreshes every token when the earliest expiry is < 15 min away.
func (a *Agent) refreshLoop(ctx context.Context) {
	for {
		a.tokMu.RLock()
		earliest := time.Time{}
		for _, e := range a.expires {
			if earliest.IsZero() || e.Before(earliest) {
				earliest = e
			}
		}
		a.tokMu.RUnlock()
		if !earliest.IsZero() && time.Until(earliest) < 15*time.Minute {
			var resp api.RunTokenResponse
			if _, err := a.sandboxCall(ctx, http.MethodPost, "/sandbox/v1/token:refresh", map[string]any{}, &resp); err != nil {
				a.raise(ipc.EvTokenRefreshFailed, "", err.Error())
			} else {
				set := map[string]string{}
				for aud, e := range resp.Tokens {
					set[aud] = e.Token
				}
				// tokens not re-issued (cancel: only runner-sandbox) are dropped: their routes close
				if err := a.installTokens(httpx.Env("REPLICANT_RUN_TOKEN_DIR", sandbox.TokenDir), set); err == nil {
					a.tokMu.Lock()
					for aud := range a.tokens {
						if _, ok := set[aud]; !ok {
							delete(a.tokens, aud)
							delete(a.expires, aud)
						}
					}
					a.tokMu.Unlock()
				}
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Minute):
		}
	}
}

func readStatus(name string) string {
	b, err := os.ReadFile(filepath.Join(sandbox.StatusDir, name))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

func (a *Agent) heartbeatOnce(ctx context.Context) (*api.HeartbeatResponse, error) {
	var req api.HeartbeatRequest
	if st := readStatus("disk-state"); st != "" {
		var used, limit, peak int64
		fmt.Sscan(readStatus("disk-used"), &used)
		fmt.Sscan(readStatus("disk-limit"), &limit)
		fmt.Sscan(readStatus("disk-peak"), &peak)
		if limit < 1 {
			limit = 1
		}
		req.Disk = &api.DiskReport{State: st, UsedBytes: used, LimitBytes: limit, PeakBytes: peak}
	}
	var resp api.HeartbeatResponse
	if _, err := a.sandboxCall(ctx, http.MethodPost, "/sandbox/v1/heartbeat", req, &resp); err != nil {
		return nil, err
	}
	a.hbMu.Lock()
	a.lastHB = time.Now()
	a.hbMu.Unlock()
	if resp.CancelReason != nil || !resp.Continue {
		reason := "revoked"
		if resp.CancelReason != nil {
			reason = *resp.CancelReason
		}
		a.hbMu.Lock()
		send := !a.cancelSent
		a.cancelSent = true
		a.hbMu.Unlock()
		if send {
			a.raise(ipc.EvCancelRequested, reason, "")
		}
	}
	return &resp, nil
}

// heartbeatLoop: every 30 s; 120 s without success raises heartbeat_lost and stops model proxying.
func (a *Agent) heartbeatLoop(ctx context.Context) {
	iv := time.Duration(httpx.EnvInt("REPLICANT_HEARTBEAT_SECONDS", 30)) * time.Second
	lostAfter := time.Duration(httpx.EnvInt("REPLICANT_HEARTBEAT_LOST_SECONDS", 120)) * time.Second
	for {
		if _, err := a.heartbeatOnce(ctx); err != nil {
			a.log.Warn("heartbeat failed", "err", err)
		}
		a.hbMu.Lock()
		lost := time.Since(a.lastHB) > lostAfter && !a.hbLost
		if lost {
			a.hbLost = true
		}
		a.hbMu.Unlock()
		if lost {
			a.raise(ipc.EvHeartbeatLost, "lease_lost", "")
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(iv):
		}
	}
}

// steeringLoop long-polls the inbox when the run has inbox:read.
func (a *Agent) steeringLoop(ctx context.Context) {
	for {
		if ctx.Err() != nil {
			return
		}
		a.evMu.Lock()
		done := a.finished
		a.evMu.Unlock()
		if done || !a.hasScope("inbox:read") {
			select {
			case <-ctx.Done():
				return
			case <-time.After(30 * time.Second):
			}
			continue
		}
		a.steerMu.Lock()
		after := a.afterSeq
		a.steerMu.Unlock()
		var resp api.InboxPoll
		if _, err := a.sandboxCall(ctx, http.MethodGet, fmt.Sprintf("/sandbox/v1/messages?after_seq=%d&wait_seconds=20", after), nil, &resp); err != nil {
			time.Sleep(5 * time.Second)
			continue
		}
		if len(resp.Messages) > 0 {
			a.steerMu.Lock()
			for _, m := range resp.Messages {
				if m.Seq > a.afterSeq {
					a.afterSeq = m.Seq
				}
				dup := false
				for _, s := range a.steering {
					if s.MessageID == m.MessageID {
						dup = true
					}
				}
				for _, c := range a.consumed {
					if c == m.MessageID {
						dup = true
					}
				}
				if !dup {
					a.steering = append(a.steering, ipc.SteeringMessage{Seq: m.Seq, MessageID: m.MessageID, Text: m.Text, Author: m.Author, SentAt: m.SentAt})
				}
			}
			a.steerMu.Unlock()
			a.raise(ipc.EvSteeringAvailable, "", "")
		}
	}
}

func (a *Agent) closeRun(ctx context.Context) {
	tok, ok := a.token(api.AudGateway)
	if !ok || a.bs.Endpoints.AIGateway == "" {
		return
	}
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimSuffix(a.bs.Endpoints.AIGateway, "/")+"/v1/runs/self:close", nil)
	req.Header.Set("Authorization", "Bearer "+tok)
	if resp, err := http.DefaultClient.Do(req); err == nil {
		resp.Body.Close()
		a.log.Info("run closed at gateway", "status", resp.StatusCode)
	}
}

func (a *Agent) budgetAtStart(ctx context.Context) {
	tok, ok := a.token(api.AudGateway)
	if !ok {
		return
	}
	cctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(cctx, http.MethodGet, strings.TrimSuffix(a.bs.Endpoints.AIGateway, "/")+"/v1/budget", nil)
	req.Header.Set("Authorization", "Bearer "+tok)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	var b struct {
		Run struct {
			LimitUSD float64 `json:"limit_usd"`
		} `json:"run"`
	}
	if json.NewDecoder(resp.Body).Decode(&b) == nil && b.Run.LimitUSD > 0 {
		a.evMu.Lock()
		a.usage.BudgetLimitUSD = b.Run.LimitUSD
		a.evMu.Unlock()
	}
}

// emitSpan exports span `agentd` (replicant-agentd lifetime) under the run's `sandbox.run` span (T-5b) in the job
// trace (T-1), with the gateway usage it brokered (WS-08 M3).
func (a *Agent) emitSpan(start time.Time) {
	e := otel("replicant-agentd")
	if e == nil || a.claims.RunID == "" {
		return
	}
	a.evMu.Lock()
	in, out := a.usage.InputTokens, a.usage.OutputTokens
	a.evMu.Unlock()
	e.emitSync(otelSpan{TraceID: otelJobTraceID(a.claims.JobID), SpanID: otelNewSpanID(), ParentID: otelRunSpanID(a.claims.RunID),
		Name: "agentd", Kind: 1, Start: start, End: time.Now(), Attrs: map[string]string{
			"replicant.job_id": a.claims.JobID, "replicant.run_id": a.claims.RunID, "replicant.tenant_id": a.claims.TenantID,
			"replicant.role": string(a.claims.Role), "gen_ai.usage.input_tokens": fmt.Sprint(in), "gen_ai.usage.output_tokens": fmt.Sprint(out)}})
}
