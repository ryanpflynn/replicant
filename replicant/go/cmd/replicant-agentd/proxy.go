package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/ipc"
	"example.com/replicant/internal/runner/s3x"
	"example.com/replicant/internal/runner/sandbox"
)

var hopHeaders = []string{"Connection", "Keep-Alive", "Proxy-Authenticate", "Proxy-Authorization", "Te", "Trailer",
	"Transfer-Encoding", "Upgrade", "Authorization", "X-Api-Key", "Content-Length", "Host", "Accept-Encoding"}

// sanitize drops inbound credentials and X-Replicant-* (§5.1 item 2).
func sanitize(h http.Header) http.Header {
	out := h.Clone()
	for _, k := range hopHeaders {
		out.Del(k)
	}
	for k := range out {
		if strings.HasPrefix(strings.ToLower(k), "x-replicant-") {
			out.Del(k)
		}
	}
	return out
}

func (a *Agent) attribution(h http.Header) {
	a.tokMu.RLock()
	c := a.claims
	a.tokMu.RUnlock()
	h.Set("X-Replicant-Tenant-Id", c.TenantID)
	h.Set("X-Replicant-Job-Id", c.JobID)
	h.Set("X-Replicant-Run-Id", c.RunID)
	h.Set("X-Replicant-Role", c.Role)
	if h.Get("traceparent") == "" {
		h.Set("traceparent", "00-"+ids.JobTraceID(c.JobID)+"-"+fmt.Sprintf("%016x", time.Now().UnixNano()|1)+"-01")
	}
}

func (a *Agent) gwRetry() (attempts int, maxWait, base, maxDelay time.Duration, unavailable time.Duration, soft float64) {
	attempts, maxWait, base, maxDelay, unavailable, soft = 6, 900*time.Second, time.Second, 60*time.Second, 600*time.Second, 0.9
	a.evMu.Lock()
	p := a.plan
	a.evMu.Unlock()
	if p != nil {
		g := p.Agent.Gateway
		if g.Retry.MaxAttempts > 0 {
			attempts = g.Retry.MaxAttempts
		}
		if g.Retry.MaxCumulativeWaitSeconds > 0 {
			maxWait = time.Duration(g.Retry.MaxCumulativeWaitSeconds) * time.Second
		}
		if g.Retry.BaseDelayMs > 0 {
			base = time.Duration(g.Retry.BaseDelayMs) * time.Millisecond
		}
		if g.Retry.MaxDelayMs > 0 {
			maxDelay = time.Duration(g.Retry.MaxDelayMs) * time.Millisecond
		}
		if g.UnavailableAfterSeconds > 0 {
			unavailable = time.Duration(g.UnavailableAfterSeconds) * time.Second
		}
		if g.SoftBudgetFraction > 0 {
			soft = g.SoftBudgetFraction
		}
	}
	return
}

func retryable(code int) bool {
	return code == 429 || code == 529 || code == 502 || code == 503 || code == 504
}

// gatewayHandler is the :7800 loopback route to replicant-gateway-shim (audience ai-gateway).
func (a *Agent) gatewayHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ok := false
		for _, p := range []string{"/anthropic/", "/openai/", "/bedrock/", "/vertex/"} {
			if strings.HasPrefix(r.URL.Path, p) {
				ok = true
			}
		}
		tok, has := a.token(api.AudGateway)
		if !ok || !has {
			http.NotFound(w, r)
			return
		}
		a.hbMu.Lock()
		lost := a.hbLost
		a.hbMu.Unlock()
		if lost {
			http.Error(w, `{"type":"error","error":{"type":"api_error","message":"replicant: heartbeat lost, run stopping"}}`, 503)
			return
		}
		body, err := io.ReadAll(io.LimitReader(r.Body, 64<<20))
		if err != nil {
			http.Error(w, "read body", 400)
			return
		}
		isModelCall := r.Method == http.MethodPost && (strings.HasSuffix(r.URL.Path, "/v1/messages") || strings.HasSuffix(r.URL.Path, "/v1/responses") ||
			strings.Contains(r.URL.Path, "/invoke"))
		if isModelCall {
			a.evMu.Lock()
			a.turns++
			calls := a.turns
			a.evMu.Unlock()
			if p := a.currentPlan(); p != nil && p.Agent.Limits.MaxModelCalls > 0 && calls > p.Agent.Limits.MaxModelCalls {
				a.raise(ipc.EvBudgetExceeded, "run", "maxModelCalls reached")
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(402)
				io.WriteString(w, `{"type":"error","error":{"type":"budget_exceeded","message":"replicant: maxModelCalls reached"}}`)
				return
			}
		}
		attempts, maxWait, base, maxDelay, unavailable, soft := a.gwRetry()
		upstream := strings.TrimSuffix(a.bs.Endpoints.AIGateway, "/") + r.URL.RequestURI()
		var waited time.Duration
		var resp *http.Response
		for attempt := 0; attempt < attempts; attempt++ {
			req, _ := http.NewRequestWithContext(r.Context(), r.Method, upstream, bytes.NewReader(body))
			req.Header = sanitize(r.Header)
			a.attribution(req.Header)
			req.Header.Set("Authorization", "Bearer "+tok)
			if attempt > 0 {
				req.Header.Set("X-Replicant-Retry-Attempt", strconv.Itoa(attempt))
			}
			req.ContentLength = int64(len(body))
			resp, err = a.client.Do(req)
			if err == nil && !retryable(resp.StatusCode) {
				a.gatewayOK()
				break
			}
			delay := base << attempt
			if delay > maxDelay {
				delay = maxDelay
			}
			if err == nil {
				if ra, perr := strconv.Atoi(resp.Header.Get("Retry-After")); perr == nil && ra > 0 {
					delay = time.Duration(ra) * time.Second
				}
				if resp.StatusCode == 429 {
					a.evMu.Lock()
					a.usage.RateLimitedSeconds += delay.Seconds()
					a.evMu.Unlock()
				}
			}
			a.gatewayFailing(unavailable)
			if attempt == attempts-1 || waited+delay > maxWait {
				break
			}
			if err == nil {
				io.Copy(io.Discard, resp.Body)
				resp.Body.Close()
				resp = nil
			}
			waited += delay
			select {
			case <-r.Context().Done():
				return
			case <-time.After(delay):
			}
		}
		if resp == nil {
			msg := "gateway unreachable"
			if err != nil {
				msg = err.Error()
			}
			http.Error(w, `{"type":"error","error":{"type":"api_error","message":"replicant: `+jsonEsc(msg)+`"}}`, 502)
			return
		}
		defer resp.Body.Close()
		a.budgetHeaders(resp.Header, soft)
		if resp.StatusCode == 402 {
			scope := resp.Header.Get("X-Replicant-Budget-Exceeded")
			a.evMu.Lock()
			a.usage.HardLimitReached, a.usage.BudgetExceededScope = true, scope
			a.evMu.Unlock()
			a.raise(ipc.EvBudgetExceeded, scope, "")
		}
		for k, vs := range resp.Header {
			for _, v := range vs {
				w.Header().Add(k, v)
			}
		}
		w.WriteHeader(resp.StatusCode)
		fl, _ := w.(http.Flusher)
		parser := &usageParser{a: a, sse: strings.Contains(resp.Header.Get("Content-Type"), "event-stream")}
		buf := make([]byte, 32<<10)
		for {
			n, rerr := resp.Body.Read(buf)
			if n > 0 {
				parser.feed(buf[:n])
				if _, werr := w.Write(buf[:n]); werr != nil {
					return
				}
				if fl != nil {
					fl.Flush()
				}
			}
			if rerr != nil {
				break
			}
		}
		parser.done()
	})
}

func jsonEsc(s string) string {
	b, _ := json.Marshal(s)
	return strings.Trim(string(b), `"`)
}

func (a *Agent) currentPlan() *sandbox.RunPlan {
	a.evMu.Lock()
	defer a.evMu.Unlock()
	return a.plan
}

func (a *Agent) gatewayOK() {
	a.gwMu.Lock()
	wasDown := a.gwDown
	a.gwFailingFrom, a.gwDown = time.Time{}, false
	a.gwMu.Unlock()
	if wasDown {
		a.raise(ipc.EvGatewayRecovered, "", "")
	}
}

func (a *Agent) gatewayFailing(after time.Duration) {
	a.gwMu.Lock()
	if a.gwFailingFrom.IsZero() {
		a.gwFailingFrom = time.Now()
	}
	raise := !a.gwDown && time.Since(a.gwFailingFrom) > after
	if raise {
		a.gwDown = true
	}
	a.gwMu.Unlock()
	if raise {
		a.raise(ipc.EvGatewayUnavailable, "", "")
	}
}

func (a *Agent) budgetHeaders(h http.Header, soft float64) {
	limit, _ := strconv.ParseFloat(h.Get("X-Replicant-Budget-Run-Limit-Usd"), 64)
	spent, err := strconv.ParseFloat(h.Get("X-Replicant-Budget-Run-Spent-Usd"), 64)
	if err != nil {
		return
	}
	a.evMu.Lock()
	a.usage.CostUSDReported = spent
	if limit > 0 {
		a.usage.BudgetLimitUSD = limit
	}
	fire := !a.usage.SoftLimitReached && a.usage.BudgetLimitUSD > 0 && spent >= soft*a.usage.BudgetLimitUSD
	if fire {
		a.usage.SoftLimitReached = true
	}
	a.evMu.Unlock()
	if fire {
		a.raise(ipc.EvBudgetSoftLimit, "run", "")
	}
}

// usageParser reads Anthropic usage from JSON or SSE bodies without buffering the stream.
type usageParser struct {
	a    *Agent
	sse  bool
	buf  bytes.Buffer
	seen bool
}

func (p *usageParser) feed(b []byte) {
	if !p.sse {
		if p.buf.Len() < 4<<20 {
			p.buf.Write(b)
		}
		return
	}
	p.buf.Write(b)
	for {
		line, err := p.buf.ReadBytes('\n')
		if err != nil {
			p.buf.Reset()
			p.buf.Write(line)
			return
		}
		l := bytes.TrimSpace(line)
		if bytes.HasPrefix(l, []byte("data:")) {
			p.event(bytes.TrimSpace(l[5:]))
		}
	}
}

func (p *usageParser) event(data []byte) {
	var ev struct {
		Type    string `json:"type"`
		Message struct {
			Usage usageFields `json:"usage"`
		} `json:"message"`
		Usage usageFields `json:"usage"`
	}
	if json.Unmarshal(data, &ev) != nil {
		return
	}
	switch ev.Type {
	case "message_start":
		p.add(ev.Message.Usage, true)
	case "message_delta":
		p.add(usageFields{OutputTokens: ev.Usage.OutputTokens}, false)
	}
}

type usageFields struct {
	InputTokens              int64 `json:"input_tokens"`
	OutputTokens             int64 `json:"output_tokens"`
	CacheReadInputTokens     int64 `json:"cache_read_input_tokens"`
	CacheCreationInputTokens int64 `json:"cache_creation_input_tokens"`
}

func (p *usageParser) add(u usageFields, call bool) {
	a := p.a
	a.evMu.Lock()
	defer a.evMu.Unlock()
	if call && !p.seen {
		a.usage.ModelCalls++
		p.seen = true
	}
	a.usage.InputTokens += u.InputTokens
	a.usage.OutputTokens += u.OutputTokens
	a.usage.CacheReadTokens += u.CacheReadInputTokens
	a.usage.CacheWriteTokens += u.CacheCreationInputTokens
}

func (p *usageParser) done() {
	if p.sse || p.buf.Len() == 0 {
		return
	}
	var m struct {
		Type  string      `json:"type"`
		Usage usageFields `json:"usage"`
	}
	if json.Unmarshal(p.buf.Bytes(), &m) == nil && m.Type == "message" {
		p.add(m.Usage, true)
	}
}

// mcpHandler is the :7801 loopback route to memory-mcp / context-mcp (one token per audience).
func (a *Agent) mcpHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var aud, base, rest string
		switch {
		case strings.HasPrefix(r.URL.Path, "/mcp/memory"):
			aud, base, rest = api.AudMemory, a.bs.Endpoints.MemoryMCP, strings.TrimPrefix(r.URL.Path, "/mcp/memory")
		case strings.HasPrefix(r.URL.Path, "/mcp/code_search"):
			aud, base, rest = api.AudContext, a.bs.Endpoints.CodeSearchMCP, strings.TrimPrefix(r.URL.Path, "/mcp/code_search")
		case strings.HasPrefix(r.URL.Path, "/mcp/context"):
			aud, base, rest = api.AudContext, a.bs.Endpoints.ContextMCP, strings.TrimPrefix(r.URL.Path, "/mcp/context")
		}
		tok, ok := a.token(aud)
		if aud == "" || !ok || base == "" {
			http.NotFound(w, r)
			return
		}
		a.simpleProxy(w, r, strings.TrimSuffix(base, "/")+rest, "Bearer "+tok, nil)
	})
}

func (a *Agent) simpleProxy(w http.ResponseWriter, r *http.Request, target, auth string, extra map[string]string) {
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}
	req, err := http.NewRequestWithContext(r.Context(), r.Method, target, r.Body)
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	req.Header = sanitize(r.Header)
	if ce := r.Header.Get("Content-Encoding"); ce != "" {
		req.Header.Set("Content-Encoding", ce)
	}
	a.attribution(req.Header)
	req.Header.Set("Authorization", auth)
	for k, v := range extra {
		req.Header.Set(k, v)
	}
	req.ContentLength = r.ContentLength
	resp, err := a.client.Do(req)
	if err != nil {
		http.Error(w, "upstream: "+err.Error(), 502)
		return
	}
	defer resp.Body.Close()
	for k, vs := range resp.Header {
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	fl, _ := w.(http.Flusher)
	buf := make([]byte, 64<<10)
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				return
			}
			if fl != nil {
				fl.Flush()
			}
		}
		if rerr != nil {
			return
		}
	}
}

var gitLimiter = struct {
	sync.Mutex
	times []time.Time
}{}

// gitHandler is the :7802 route to repo-mirror (audience repo-mirror): upload-pack only; after prepare,
// only lazy blob fetches at ≤ 20 requests/min.
func (a *Agent) gitHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tok, ok := a.token(api.AudMirror)
		if !ok || !strings.HasPrefix(r.URL.Path, "/git/") {
			http.NotFound(w, r)
			return
		}
		if strings.Contains(r.URL.Path, "receive-pack") || r.URL.Query().Get("service") == "git-receive-pack" {
			http.Error(w, "read-only", 403)
			return
		}
		a.evMu.Lock()
		prepared := a.prepared
		a.evMu.Unlock()
		if prepared {
			gitLimiter.Lock()
			now := time.Now()
			var keep []time.Time
			for _, t := range gitLimiter.times {
				if now.Sub(t) < time.Minute {
					keep = append(keep, t)
				}
			}
			if len(keep) >= 20 {
				gitLimiter.times = keep
				gitLimiter.Unlock()
				http.Error(w, "rate limited", 429)
				return
			}
			gitLimiter.times = append(keep, now)
			gitLimiter.Unlock()
		}
		repoID := strings.SplitN(strings.TrimPrefix(r.URL.Path, "/git/"), "/", 2)[0]
		extra := map[string]string{}
		if repoID == a.bs.Repo.RepoID {
			extra["X-Replicant-Repo-Provider"] = a.bs.Repo.Provider
			extra["X-Replicant-Repo-Host"] = a.bs.Repo.Host
			extra["X-Replicant-Repo-Owner"] = a.bs.Repo.Owner
			extra["X-Replicant-Repo-Name"] = a.bs.Repo.Name
		}
		basic := "Basic " + base64.StdEncoding.EncodeToString([]byte("x-run-token:"+tok))
		a.simpleProxy(w, r, strings.TrimSuffix(a.bs.Endpoints.RepoMirror, "/")+r.URL.Path, basic, extra)
	})
}

// inputsHandler is the :7803 route (prepare only): GET /inputs/manifest and /inputs/<bucket_class>/<key>,
// limited to the run's own inputs/ prefixes, fetched with the run-scoped S3 session.
func (a *Agent) inputsHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		a.evMu.Lock()
		closed := a.prepared
		a.evMu.Unlock()
		if closed || r.Method != http.MethodGet || !strings.HasPrefix(r.URL.Path, "/inputs/") {
			http.NotFound(w, r)
			return
		}
		cl, err := a.s3()
		if err != nil {
			http.Error(w, err.Error(), 503)
			return
		}
		var bucket, key string
		rest := strings.TrimPrefix(r.URL.Path, "/inputs/")
		if rest == "manifest" {
			bucket, key, err = s3x.ParseURI(a.bs.InputsManifestURI)
		} else {
			class, k, _ := strings.Cut(rest, "/")
			pre := ""
			if a.bs.WS02 != nil {
				switch class {
				case "artifacts":
					pre = a.bs.WS02.ArtifactPrefixes.Artifacts
				case "session":
					pre = a.bs.WS02.ArtifactPrefixes.Session
				}
			}
			if pre == "" {
				http.NotFound(w, r)
				return
			}
			var pk string
			bucket, pk, err = s3x.ParseURI(pre)
			if err != nil || !strings.HasPrefix(k, pk+"inputs/") || strings.Contains(k, "..") {
				http.Error(w, "outside the run's inputs/", 403)
				return
			}
			key = k
		}
		if err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		b, _, err := cl.Get(r.Context(), bucket, key)
		if err != nil {
			code := 502
			if err == s3x.ErrNotFound {
				code = 404
			}
			http.Error(w, err.Error(), code)
			return
		}
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Write(b)
	})
}

// loadPlan fetches the run plan through the run's S3 session (gateway retry and limit settings).
func (a *Agent) loadPlan(ctx context.Context) {
	for i := 0; i < 30 && ctx.Err() == nil; i++ {
		if err := a.tryLoadPlan(ctx); err == nil {
			return
		}
		time.Sleep(2 * time.Second)
	}
}

func (a *Agent) tryLoadPlan(ctx context.Context) error {
	cl, err := a.s3()
	if err != nil {
		return err
	}
	b, k, err := s3x.ParseURI(a.bs.InputsManifestURI)
	if err != nil {
		return err
	}
	mb, _, err := cl.Get(ctx, b, k)
	if err != nil {
		return err
	}
	var m sandbox.InputsManifest
	if err := json.Unmarshal(mb, &m); err != nil {
		return err
	}
	for _, o := range m.Objects {
		if o.Place != "run_plan" {
			continue
		}
		pb, _, err := cl.Get(ctx, b, o.Key)
		if err != nil {
			return err
		}
		if s3x.Sha256Hex(pb) != o.SHA256 {
			return fmt.Errorf("run plan digest mismatch")
		}
		var p sandbox.RunPlan
		if err := json.Unmarshal(pb, &p); err != nil {
			return err
		}
		a.evMu.Lock()
		a.plan = &p
		if p.Effective.BudgetUSD > 0 && a.usage.BudgetLimitUSD == 0 {
			a.usage.BudgetLimitUSD = p.Effective.BudgetUSD
		}
		a.evMu.Unlock()
		return nil
	}
	return fmt.Errorf("no run_plan object")
}
