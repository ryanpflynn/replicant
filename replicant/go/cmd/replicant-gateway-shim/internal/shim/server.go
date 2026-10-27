package shim

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"replicant.example.com/go/cmd/replicant-gateway-shim/internal/ids"
	"replicant.example.com/go/cmd/replicant-gateway-shim/internal/usage"
)

// UsageSink is the subset of usage.Log the server needs.
type UsageSink interface {
	Append(usage.Event) error
	Healthy() bool
}

// Server is replicant-gateway-shim's HTTP surface (gateway-interface.openapi.yaml).
type Server struct {
	Cfg     *Config
	State   *MemState
	Auth    *Authenticator
	Quotas  *QuotaClient
	Backend Backend
	Usage   UsageSink
	Feeds   *Feeds
	Log     *slog.Logger
	Now     func() time.Time
}

// BudgetCounter is the gateway-interface BudgetCounter.
type BudgetCounter struct {
	LimitUSD    *float64 `json:"limit_usd"`
	SpentUSD    float64  `json:"spent_usd"`
	ReservedUSD float64  `json:"reserved_usd"`
	Period      string   `json:"period,omitempty"`
}

// BudgetState is the gateway-interface BudgetState.
type BudgetState struct {
	Run          BudgetCounter `json:"run"`
	Job          BudgetCounter `json:"job"`
	TenantPeriod BudgetCounter `json:"tenant_period"`
}

func (s *Server) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

// Handler returns the router.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200); _, _ = w.Write([]byte("ok\n")) })
	mux.HandleFunc("GET /readyz", s.readyz)
	mux.HandleFunc("GET /v1/budget", s.getBudget)
	mux.HandleFunc("POST /v1/runs/self:close", s.selfClose)
	mux.HandleFunc("POST /internal/v1/revocations", s.revocations)
	mux.HandleFunc("PUT /internal/v1/runs/{run_id}/policy", s.putPolicy)
	mux.HandleFunc("GET /internal/v1/runs/{run_id}/policy", s.getPolicy)
	mux.HandleFunc("PUT /internal/v1/jobs/{job_id}/budget", s.putJobBudget)
	mux.HandleFunc("PUT /internal/v1/budgets/jobs/{job_id}", s.putJobAccount)
	mux.HandleFunc("GET /internal/v1/models", s.models)
	for name, rt := range s.Cfg.File.Routes {
		rt := rt
		_ = name
		prefix := strings.TrimRight(rt.Prefix, "/")
		switch rt.Protocol {
		case "anthropic_messages":
			mux.HandleFunc("POST "+prefix+"/v1/messages", s.dataPlane("anthropic_messages", rt))
			mux.HandleFunc("POST "+prefix+"/v1/messages/count_tokens", s.dataPlane("anthropic_count_tokens", rt))
		case "openai_responses":
			mux.HandleFunc("POST "+prefix+"/v1/responses", s.dataPlane("openai_responses", rt))
		case "embeddings":
			mux.HandleFunc("POST "+prefix+"/v1/embeddings", s.dataPlane("embeddings", rt))
		}
	}
	return mux
}

func (s *Server) readyz(w http.ResponseWriter, r *http.Request) {
	ready := len(s.Cfg.Prices) > 0 && (s.Usage == nil || s.Usage.Healthy())
	body := map[string]any{"ready": ready, "backend": s.Backend.Name(), "state": s.Cfg.StateMode, "keys": s.Auth.KeyStatus()}
	j, _ := json.Marshal(body)
	w.Header().Set("Content-Type", "application/json")
	if !ready {
		w.WriteHeader(503)
	}
	_, _ = w.Write(j)
}

func jsonOut(w http.ResponseWriter, status int, v any) {
	j, _ := json.Marshal(v)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(j)
}

func (s *Server) authErr(w http.ResponseWriter, protocol string, err error) {
	var ae *AuthError
	if errors.As(err, &ae) {
		writeError(w, protocol, ae.Status, ae.Code, ae.Msg, nil)
		return
	}
	writeError(w, protocol, 401, "unauthenticated", err.Error(), nil)
}

// --- data plane ---------------------------------------------------------------------------------------

type bodyInfo struct {
	Model           string          `json:"model"`
	Stream          bool            `json:"stream"`
	MaxTokens       int             `json:"max_tokens"`
	MaxOutputTokens int             `json:"max_output_tokens"`
	Tools           json.RawMessage `json:"tools"`
	ToolChoice      json.RawMessage `json:"tool_choice"`
}

// bufWriter holds a non-streaming response so budget headers can include this request's settled spend.
type bufWriter struct {
	h      http.Header
	status int
	buf    bytes.Buffer
}

func (b *bufWriter) Header() http.Header { return b.h }
func (b *bufWriter) Write(p []byte) (int, error) {
	if b.status == 0 {
		b.status = 200
	}
	return b.buf.Write(p)
}
func (b *bufWriter) WriteHeader(s int) {
	if b.status == 0 {
		b.status = s
	}
}

func (b *bufWriter) flushTo(w http.ResponseWriter) {
	for k, vs := range b.h {
		if strings.EqualFold(k, "Trailer") {
			continue
		}
		w.Header()[k] = vs
	}
	w.Header().Set("Content-Length", strconv.Itoa(b.buf.Len()))
	if b.status == 0 {
		b.status = 200
	}
	w.WriteHeader(b.status)
	_, _ = w.Write(b.buf.Bytes())
}

type reqCtx struct {
	start     time.Time
	requestID string
	protocol  string
	caller    *Caller
	tenant    string
	jobID     string
	runID     string
	role      string
	rbID      string
	actingFor string
	purpose   string
	pipeline  string
	account   string
	evalMode  string
	model     string
	stream    bool
	retry     int
	trace     string
	span      string
}

func parseTraceparent(v string) (string, string) {
	parts := strings.Split(v, "-")
	if len(parts) == 4 && len(parts[1]) == 32 && len(parts[2]) == 16 && parts[1] != strings.Repeat("0", 32) && parts[2] != strings.Repeat("0", 16) {
		return parts[1], parts[2]
	}
	return "", ""
}

var reEvalAcct = regexp.MustCompile(`^fgw:(eval|proposal):`)

func (s *Server) dataPlane(protocol string, route Route) http.HandlerFunc {
	prefix := strings.TrimRight(route.Prefix, "/")
	return func(w http.ResponseWriter, r *http.Request) {
		rc := &reqCtx{start: s.now(), requestID: ids.New("gwu"), protocol: protocol, evalMode: "none"}
		w.Header().Set("X-Replicant-Request-Id", rc.requestID)
		rc.trace, rc.span = parseTraceparent(r.Header.Get("traceparent"))
		if v := r.Header.Get("X-Replicant-Retry-Attempt"); v != "" {
			rc.retry, _ = strconv.Atoi(v)
		}
		var fault *Fault
		if v := r.Header.Get("X-Replicant-Stub-Fault"); v != "" && s.Cfg.Backend != "enterprise" {
			f, err := ParseFaultHeader(v)
			if err != nil {
				writeError(w, protocol, 400, "invalid_request", err.Error(), nil)
				return
			}
			fault = f
		}

		caller, err := s.Auth.Authenticate(r.Header.Get("Authorization"))
		if err != nil {
			s.authErr(w, protocol, err)
			s.logReq(rc, 401, "auth_denied")
			return
		}
		rc.caller = caller
		var policy *Policy
		if caller.Kind == "run" {
			c := caller.Run
			rc.tenant, rc.jobID, rc.runID, rc.role, rc.rbID, rc.actingFor = c.TenantID, c.JobID, c.RunID, c.Role, c.ReleaseBundleID, c.ActingFor
			rc.purpose = "agent"
			if c.Role == "qa" {
				rc.purpose = "qa_agent"
			}
			for h, want := range map[string]string{"X-Replicant-Tenant-Id": c.TenantID, "X-Replicant-Job-Id": c.JobID, "X-Replicant-Run-Id": c.RunID, "X-Replicant-Role": c.Role} {
				if v := r.Header.Get(h); v != "" && v != want {
					writeError(w, protocol, 401, "unauthenticated", h+" does not match the run token", nil)
					s.logReq(rc, 401, "auth_denied")
					return
				}
			}
			if s.State.IsRevoked(c.RunID, c.JobID) {
				writeError(w, protocol, 401, "run_closed", "run is closed or revoked", nil)
				s.emitDenied(rc, 401, "auth_denied")
				return
			}
			p, ok := s.State.GetPolicy(c.RunID)
			if !ok {
				w.Header().Set("retry-after", "5")
				writeError(w, protocol, 503, "run_policy_missing", "no run policy registered for "+c.RunID, nil)
				s.emitDenied(rc, 503, "auth_denied")
				return
			}
			tokBudget := s.Cfg.DevRunMicros
			if c.BudgetUSD != nil {
				tokBudget = USDToMicros(*c.BudgetUSD)
			}
			if USDToMicros(p.BudgetUSD) != tokBudget || p.TenantID != c.TenantID || p.JobID != c.JobID || p.Role != c.Role {
				writeError(w, protocol, 403, "run_policy_mismatch", "run policy does not match the run token", nil)
				s.emitDenied(rc, 403, "auth_denied")
				return
			}
			policy = &p
			if p.BudgetAccount != "" {
				rc.account = p.BudgetAccount
			}
		} else {
			if !s.serviceDataPlane(w, r, rc) {
				return
			}
		}
		if rc.account == "" && rc.jobID != "" {
			if a, ok := s.State.JobAccountOf(rc.jobID); ok {
				rc.account = a.Account
			}
		}
		if rc.account != "" {
			rc.evalMode = "eval"
			if strings.HasPrefix(rc.account, "fgw:pipeline:") {
				rc.evalMode = "shadow"
			}
			if a, ok := s.State.JobAccountOf(rc.jobID); ok && a.EvaluationMode != "" {
				rc.evalMode = a.EvaluationMode
			}
		}

		body, err := io.ReadAll(io.LimitReader(r.Body, 32<<20+1))
		if err != nil || len(body) > 32<<20 {
			writeError(w, protocol, 400, "invalid_request", "request body unreadable or larger than 32 MiB", nil)
			return
		}
		var bi bodyInfo
		if err := json.Unmarshal(body, &bi); err != nil {
			writeError(w, protocol, 400, "invalid_request", "request body is not JSON", nil)
			return
		}
		rc.model, rc.stream = bi.Model, bi.Stream
		maxTok := bi.MaxTokens
		if maxTok == 0 {
			maxTok = bi.MaxOutputTokens
		}
		if caller.Kind == "service" && (rc.purpose == "worker_llm" || rc.purpose == "eval_judge") &&
			(nonEmptyJSON(bi.Tools) || nonEmptyJSON(bi.ToolChoice)) {
			writeError(w, protocol, 400, "tools_not_allowed", "in-worker calls are tool-less (G-24)", nil)
			s.emitDenied(rc, 400, "model_denied")
			return
		}
		// model allowlist (run policy) and price book
		if policy != nil && !contains(policy.Models, bi.Model) {
			writeError(w, protocol, 403, "model_not_allowed", "model "+bi.Model+" is not in the run policy", nil)
			s.emitDenied(rc, 403, "model_denied")
			return
		}
		if _, ok := s.Cfg.Prices[bi.Model]; !ok {
			writeError(w, protocol, 403, "model_not_allowed", "model "+bi.Model+" is not priced", func(fe *ReplicantError) { fe.Detail = "unpriced_model" })
			s.emitDenied(rc, 403, "model_denied")
			return
		}
		if ce, ok := s.Cfg.File.Catalog[bi.Model]; ok && ce.RetiresAt != "" {
			if t, err := time.Parse(time.RFC3339, ce.RetiresAt); err == nil && s.now().After(t) {
				writeError(w, protocol, 403, "model_not_allowed", "model "+bi.Model+" is retired", func(fe *ReplicantError) { fe.Detail = "model_retired" })
				s.emitDenied(rc, 403, "model_denied")
				return
			}
		}
		if s.Usage != nil && !s.Usage.Healthy() {
			writeError(w, protocol, 503, "gateway_state_unavailable", "usage log unavailable (fail closed)", nil)
			return
		}

		// tenant quota (skipped for budget-account jobs)
		var quota *TenantQuota
		if rc.account == "" {
			q, err := s.Quotas.Get(r.Context(), rc.tenant)
			if err != nil {
				w.Header().Set("retry-after", "5")
				writeError(w, protocol, 503, "gateway_state_unavailable", "tenant quota unavailable", nil)
				return
			}
			if q == nil {
				writeError(w, protocol, 402, "tenant_quota_unset", "tenant "+rc.tenant+" has no gateway quota", nil)
				s.emitDenied(rc, 402, "budget_denied")
				return
			}
			quota = q
		}

		inflightKey := ""
		if policy != nil {
			inflightKey = "run:" + rc.runID
			if !s.State.AcquireInflight(inflightKey, policy.MaxConcurrency) {
				w.Header().Set("retry-after", "2")
				w.Header().Set("X-Replicant-Limit", "run_concurrency")
				writeError(w, protocol, 429, "rate_limited", "run concurrency limit reached", nil)
				s.emitDenied(rc, 429, "rate_limited")
				return
			}
			defer s.State.ReleaseInflight(inflightKey)
		}

		levels := s.levels(rc, caller, quota)
		charged := protocol != "anthropic_count_tokens"
		estimate := s.Cfg.Estimate(bi.Model, len(body), maxTok)
		if charged {
			ok, lvl := s.State.Admit(levels, estimate, rc.requestID)
			if !ok {
				s.setBudgetHeaders(w.Header(), rc)
				w.Header().Set("X-Replicant-Budget-Exceeded", lvl)
				bs := s.budgetState(rc)
				writeError(w, protocol, 402, "budget_exceeded", lvl+" budget exhausted", func(fe *ReplicantError) { fe.Budget = &bs })
				s.emitDenied(rc, 402, "budget_denied")
				return
			}
		}

		d := &DataRequest{Protocol: protocol, Route: route, UpstreamPath: strings.TrimPrefix(r.URL.Path, prefix), Body: body,
			Model: bi.Model, Stream: bi.Stream, MaxTokens: maxTok, Role: rc.role, Policy: policy, RequestID: rc.requestID, Header: r.Header}
		if r.URL.RawQuery != "" {
			d.UpstreamPath += "?" + r.URL.RawQuery
		}
		d.RunKey = rc.runID
		if d.Role == "" {
			d.Role = rc.purpose // service callers select scripts by purpose (worker_llm, eval_judge)
		}
		if d.RunKey == "" {
			d.RunKey = caller.Service + ":" + rc.jobID
		}

		var res Result
		var out http.ResponseWriter = w
		var bw *bufWriter
		if !bi.Stream {
			bw = &bufWriter{h: http.Header{}}
			out = bw
		} else {
			s.setBudgetHeaders(w.Header(), rc) // streams: pre-request figures
		}
		if fault != nil {
			writeFault(out, protocol, fault)
			res = Result{Status: fault.Status, Faulted: true}
		} else {
			res = s.Backend.Serve(out, r, d)
		}

		// settlement
		var cost int64
		src := "parsed"
		if res.UsageParsed {
			cost, _ = s.Cfg.Cost(bi.Model, res.Usage)
		} else if !res.Faulted && (res.Status == 200 || res.ClientAborted) {
			cost, src = estimate, "estimated" // R2-058: unparsed usage settles at the estimate
		}
		if charged {
			s.State.Settle(rc.requestID, cost)
		}
		if bw != nil {
			s.setBudgetHeaders(bw.h, rc)
			if res.ServedModel != "" {
				bw.h.Set("X-Replicant-Served-Model", res.ServedModel)
			}
			bw.flushTo(w)
		}
		status := statusOf(res)
		s.emit(rc, res, cost, src, status)
	}
}

func nonEmptyJSON(r json.RawMessage) bool {
	t := strings.TrimSpace(string(r))
	return t != "" && t != "null" && t != "[]" && t != "{}"
}

func statusOf(res Result) string {
	switch {
	case res.ClientAborted:
		return "client_aborted"
	case res.Status == 200:
		return "ok"
	case res.Status == 429:
		return "rate_limited"
	case res.Status == 529:
		return "overloaded"
	case res.Status == 402:
		return "budget_denied"
	case res.Status == 504:
		return "timeout"
	default:
		return "upstream_error"
	}
}

// serviceDataPlane validates a service caller (gateway.md §5.4) and fills attribution. Returns false if it answered.
func (s *Server) serviceDataPlane(w http.ResponseWriter, r *http.Request, rc *reqCtx) bool {
	c := rc.caller
	purpose := r.Header.Get("X-Replicant-Purpose")
	routes := map[string][]string{
		"worker_llm": {"anthropic_messages", "anthropic_count_tokens", "openai_responses"},
		"embedding":  {"embeddings", "anthropic_count_tokens"},
		"eval_judge": {"anthropic_messages", "openai_responses"},
	}
	if purpose == "" || !c.Has(purpose) || !contains(routes[purpose], rc.protocol) {
		writeError(w, rc.protocol, 403, "scope_missing", "service caller not bound to purpose "+strconv.Quote(purpose)+" on this route", nil)
		return false
	}
	rc.purpose = purpose
	rc.tenant = r.Header.Get("X-Replicant-Tenant-Id")
	rc.jobID = r.Header.Get("X-Replicant-Job-Id")
	rc.pipeline = r.Header.Get("X-Replicant-Pipeline-Id")
	if !reTenant.MatchString(rc.tenant) {
		writeError(w, rc.protocol, 403, "scope_missing", "X-Replicant-Tenant-Id required for service callers", nil)
		return false
	}
	if purpose != "embedding" && !reJob.MatchString(rc.jobID) {
		writeError(w, rc.protocol, 403, "scope_missing", "X-Replicant-Job-Id required for service callers", nil)
		return false
	}
	if purpose == "eval_judge" {
		if r.Header.Get("X-Replicant-Role") != "eval-judge" {
			writeError(w, rc.protocol, 403, "scope_missing", "eval_judge requires X-Replicant-Role: eval-judge", nil)
			return false
		}
		rc.role = "eval-judge"
		if _, ok := s.State.JobAccountOf(rc.jobID); !ok {
			writeError(w, rc.protocol, 402, "account_unset", "judge call for a job without a budget account", nil)
			return false
		}
	}
	return true
}

// levels builds the admission levels in gateway.md §6.2 order.
func (s *Server) levels(rc *reqCtx, c *Caller, q *TenantQuota) []Level {
	now := s.now().UTC()
	month, day := now.Format("200601"), now.Format("20060102")
	var lv []Level
	if c.Kind == "run" {
		var lim *int64
		if c.Run.BudgetUSD != nil {
			v := USDToMicros(*c.Run.BudgetUSD)
			lim = &v
		} else if s.Cfg.DevRunMicros > 0 {
			v := s.Cfg.DevRunMicros
			lim = &v
		}
		lv = append(lv, Level{Name: "run", Key: "fgw:run:" + rc.runID, Limit: lim})
	}
	switch {
	case c.Kind == "service" && rc.purpose == "embedding":
		var lim *int64
		if v := USDToMicros(s.Cfg.File.Budgets.Embeddings.PerTenantDayUSD); v > 0 {
			lim = &v
		}
		lv = append(lv, Level{Name: "tenant_embeddings_day", Key: "fgw:tenant:" + rc.tenant + ":emb:" + day, Limit: lim})
		if rc.pipeline != "" {
			var pl *int64
			if v := USDToMicros(s.Cfg.File.Budgets.Embeddings.ReembedBackfillUSDMax); v > 0 {
				pl = &v
			}
			lv = append(lv, Level{Name: "pipeline", Key: "fgw:pipeline:" + rc.pipeline, Limit: pl})
		}
	case c.Kind == "service":
		lv = append(lv, Level{Name: "job", Key: "fgw:job:" + rc.jobID + ":worker_spent"})
	default:
		jl := Level{Name: "job", Key: "fgw:job:" + rc.jobID}
		if _, ok := s.State.JobLimit(rc.jobID); !ok && s.Cfg.Backend != "enterprise" && s.Cfg.DevJobMicros > 0 {
			v := s.Cfg.DevJobMicros
			jl.Limit = &v
		}
		lv = append(lv, jl)
	}
	if rc.account != "" {
		lv = append(lv, Level{Name: "account", Key: "fgw:acct:" + rc.account})
	} else if q != nil {
		lv = append(lv, Level{Name: "tenant_period", Key: "fgw:tenant:" + rc.tenant + ":" + month, Limit: limitPtr(q.MonthlyBudgetUSD)},
			Level{Name: "tenant_period", Key: "fgw:tenant:" + rc.tenant + ":" + day, Limit: limitPtr(q.DailyBudgetUSD)})
	}
	var pd, pm *int64
	if q != nil {
		pd, pm = limitPtr(q.PlatformDailyUSDMax), limitPtr(q.PlatformMonthlyUSDMax)
	}
	lv = append(lv, Level{Name: "platform", Key: "fgw:platform:" + day, Limit: pd}, Level{Name: "platform", Key: "fgw:platform:" + month, Limit: pm})
	return lv
}

func limitPtr(usd float64) *int64 {
	if usd <= 0 {
		return nil
	}
	v := USDToMicros(usd)
	return &v
}

func (s *Server) setBudgetHeaders(h http.Header, rc *reqCtx) {
	if rc.runID != "" {
		lim, spent, _ := s.State.CounterState("fgw:run:" + rc.runID)
		if lim != nil {
			h.Set("X-Replicant-Budget-Run-Limit-Usd", usd6(*lim))
		}
		h.Set("X-Replicant-Budget-Run-Spent-Usd", usd6(spent))
	}
	if rc.jobID != "" {
		_, spent, _ := s.State.CounterState("fgw:job:" + rc.jobID)
		h.Set("X-Replicant-Budget-Job-Spent-Usd", usd6(spent))
	}
}

func microsToUSD(m int64) float64 { return float64(m) / 1e6 }

func (s *Server) counter(key string) BudgetCounter {
	lim, spent, resv := s.State.CounterState(key)
	bc := BudgetCounter{SpentUSD: microsToUSD(spent), ReservedUSD: microsToUSD(resv)}
	if lim != nil {
		v := microsToUSD(*lim)
		bc.LimitUSD = &v
	}
	return bc
}

func (s *Server) budgetState(rc *reqCtx) BudgetState {
	month := s.now().UTC().Format("200601")
	tp := s.counter("fgw:tenant:" + rc.tenant + ":" + month)
	tp.Period = s.now().UTC().Format("2006-01")
	return BudgetState{Run: s.counter("fgw:run:" + rc.runID), Job: s.counter("fgw:job:" + rc.jobID), TenantPeriod: tp}
}

func (s *Server) logReq(rc *reqCtx, status int, outcome string) {
	if s.Log == nil {
		return
	}
	s.Log.Info("gateway request", "request_id", rc.requestID, "protocol", rc.protocol, "tenant_id", rc.tenant, "job_id", rc.jobID,
		"run_id", rc.runID, "role", rc.role, "model", rc.model, "stream", rc.stream, "status", status, "outcome", outcome,
		"trace_id", rc.trace, "latency_ms", time.Since(rc.start).Milliseconds())
}

func (s *Server) emitDenied(rc *reqCtx, httpStatus int, status string) {
	s.emit(rc, Result{Status: httpStatus}, 0, "parsed", status)
}

// emit writes the usage event (GW-7) and the request log line (never bodies, GW-12).
func (s *Server) emit(rc *reqCtx, res Result, cost int64, src, status string) {
	s.logReq(rc, res.Status, status)
	// WS-08 M2: one span per model request in the job's trace, under the caller's span (traceparent) or, without
	// one, under the run's sandbox.run span (T-5b).
	if e := otel("replicant-gateway-shim"); e != nil && rc.jobID != "" {
		parent := rc.span
		if parent == "" && rc.runID != "" {
			parent = otelRunSpanID(rc.runID)
		}
		e.emitJobSpan(rc.jobID, parent, "gateway "+rc.protocol, 2, rc.start, time.Now(), map[string]string{
			"replicant.request_id": rc.requestID, "replicant.run_id": rc.runID, "replicant.tenant_id": rc.tenant,
			"replicant.role": rc.role, "gen_ai.request.model": rc.model, "replicant.gateway_status": status,
			"http.response.status_code": strconv.Itoa(res.Status), "replicant.cost_micros_usd": strconv.FormatInt(cost, 10),
			"gen_ai.usage.input_tokens":  strconv.Itoa(int(res.Usage.InputTokens)),
			"gen_ai.usage.output_tokens": strconv.Itoa(int(res.Usage.OutputTokens)),
		}, res.Status >= 500)
	}
	if s.Usage == nil || rc.tenant == "" {
		return
	}
	model := rc.model
	if model == "" {
		model = "unknown/unknown"
	}
	proto := rc.protocol
	ev := usage.Event{
		Schema: "replicant.gateway-usage/v1", EventID: rc.requestID, OccurredAt: rc.start.UTC().Format(time.RFC3339Nano),
		TenantID: rc.tenant, JobID: rc.jobID, RunID: rc.runID, Role: rc.role, ReleaseBundleID: rc.rbID,
		GatewayModelID: model, Protocol: proto, Streaming: rc.stream, Status: status, HTTPStatus: clampStatus(res.Status),
		UpstreamReqID: trunc(res.UpstreamRequestID, 128),
		Usage: usage.Tokens{InputTokens: res.Usage.InputTokens, OutputTokens: res.Usage.OutputTokens, CacheReadTokens: res.Usage.CacheReadTokens,
			CacheWriteTokens: res.Usage.CacheWriteTokens, ReasoningTokens: res.Usage.ReasoningTokens},
		Cost:      usage.Cost{MicrosUSD: cost, Source: "price_book", PriceBookVersion: 1},
		LatencyMs: time.Since(rc.start).Milliseconds(), RetryAttempt: rc.retry, TraceID: rc.trace, SpanID: rc.span,
		ActingFor: rc.actingFor, Purpose: rc.purpose, UsageSource: src, ServedModel: trunc(res.ServedModel, 200),
		PipelineID: rc.pipeline, EvaluationMode: rc.evalMode,
	}
	if rc.evalMode != "none" {
		ev.BudgetAccount = rc.account
	}
	if res.TTFB > 0 {
		t := res.TTFB.Milliseconds()
		ev.TTFBMs = &t
	}
	if rc.caller != nil {
		if rc.caller.Kind == "run" {
			ev.Caller = usage.Caller{Kind: "run", TokenJti: rc.caller.Run.Jti, GrantJti: rc.caller.Run.GrantJti}
		} else {
			ev.Caller = usage.Caller{Kind: "service", ServicePrincipal: rc.caller.Service}
			if localAllServices[rc.caller.Service] { // the event schema allows only the three data-plane principals
				switch rc.purpose {
				case "embedding":
					ev.Caller.ServicePrincipal = "svc_memory-service"
				case "eval_judge":
					ev.Caller.ServicePrincipal = "svc_eval-worker"
				default:
					ev.Caller.ServicePrincipal = "svc_replicant-worker"
				}
			}
		}
	}
	if !strings.HasPrefix(ev.Caller.TokenJti, "rtk_") {
		ev.Caller.TokenJti = ""
	}
	ba := &usage.BudgetAfter{}
	if rc.runID != "" {
		_, sp, _ := s.State.CounterState("fgw:run:" + rc.runID)
		ba.RunSpentMicros = &sp
	}
	if rc.jobID != "" {
		_, sp, _ := s.State.CounterState("fgw:job:" + rc.jobID)
		ba.JobSpentMicros = &sp
	}
	ev.BudgetAfter = ba
	if err := s.Usage.Append(ev); err != nil && s.Log != nil {
		s.Log.Error("usage event not queued", "request_id", rc.requestID, "err", err.Error())
	}
}

func clampStatus(s int) int {
	if s < 100 {
		return 499
	}
	if s > 599 {
		return 599
	}
	return s
}

func trunc(s string, n int) string {
	if len(s) > n {
		return s[:n]
	}
	return s
}

// --- control endpoints --------------------------------------------------------------------------------

func (s *Server) runCaller(w http.ResponseWriter, r *http.Request) (*RunClaims, bool) {
	c, err := s.Auth.Authenticate(r.Header.Get("Authorization"))
	if err != nil {
		s.authErr(w, "", err)
		return nil, false
	}
	if c.Kind != "run" {
		writeError(w, "", 403, "scope_missing", "a run token is required", nil)
		return nil, false
	}
	return c.Run, true
}

func (s *Server) serviceCaller(w http.ResponseWriter, r *http.Request, right string) (*Caller, bool) {
	c, err := s.Auth.Authenticate(r.Header.Get("Authorization"))
	if err != nil {
		s.authErr(w, "", err)
		return nil, false
	}
	if !c.Has(right) {
		writeError(w, "", 403, "scope_missing", "caller may not use this endpoint", nil)
		return nil, false
	}
	return c, true
}

func (s *Server) getBudget(w http.ResponseWriter, r *http.Request) {
	c, ok := s.runCaller(w, r)
	if !ok {
		return
	}
	if s.State.IsRevoked(c.RunID, c.JobID) {
		writeError(w, "", 401, "run_closed", "run is closed or revoked", nil)
		return
	}
	rc := &reqCtx{tenant: c.TenantID, jobID: c.JobID, runID: c.RunID}
	bs := s.budgetState(rc)
	if bs.Run.LimitUSD == nil && c.BudgetUSD != nil {
		v := *c.BudgetUSD
		bs.Run.LimitUSD = &v
	}
	jsonOut(w, 200, bs)
}

func (s *Server) selfClose(w http.ResponseWriter, r *http.Request) {
	c, ok := s.runCaller(w, r)
	if !ok {
		return
	}
	s.State.Revoke([]string{c.RunID}, nil, revocationTTL)
	w.WriteHeader(204)
}

func decodeStrict(r *http.Request, v any) error {
	dec := json.NewDecoder(io.LimitReader(r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return err
	}
	if dec.More() {
		return errors.New("trailing data")
	}
	return nil
}

func badRequest(w http.ResponseWriter, msg string) {
	writeError(w, "", 400, "invalid_request", msg, nil)
}

func (s *Server) revocations(w http.ResponseWriter, r *http.Request) {
	if _, ok := s.serviceCaller(w, r, "revoke"); !ok {
		return
	}
	var body struct {
		RunIDs     []string `json:"run_ids"`
		JobIDs     []string `json:"job_ids"`
		TenantID   string   `json:"tenant_id"`
		Reason     string   `json:"reason"`
		TTLSeconds int      `json:"ttl_seconds"`
	}
	if err := decodeStrict(r, &body); err != nil {
		badRequest(w, "invalid body: "+err.Error())
		return
	}
	switch body.Reason {
	case "job_cancelled", "takeover", "budget_exhausted", "security_incident", "tenant_suspended":
	default:
		badRequest(w, "reason is required (job_cancelled|takeover|budget_exhausted|security_incident|tenant_suspended)")
		return
	}
	if len(body.RunIDs) > 500 || len(body.JobIDs) > 500 {
		badRequest(w, "at most 500 ids per list")
		return
	}
	for _, id := range body.RunIDs {
		if !reRun.MatchString(id) {
			badRequest(w, "bad run_id "+strconv.Quote(id))
			return
		}
	}
	for _, id := range body.JobIDs {
		if !reJob.MatchString(id) {
			badRequest(w, "bad job_id "+strconv.Quote(id))
			return
		}
	}
	ttl := revocationTTL
	if body.TTLSeconds != 0 {
		if body.TTLSeconds < 3600 || body.TTLSeconds > 3888000 {
			badRequest(w, "ttl_seconds must be within [3600, 3888000]")
			return
		}
		ttl = time.Duration(body.TTLSeconds) * time.Second
	}
	s.State.Revoke(body.RunIDs, body.JobIDs, ttl)
	w.WriteHeader(204)
}

var validRoles = []string{"planner", "implementer", "qa", "reviewer", "repair", "takeover", "eval"}

func (s *Server) validatePolicy(runID string, p *Policy) (int, string, string) {
	switch {
	case !reRun.MatchString(runID):
		return 400, "invalid_request", "bad run_id"
	case !reTenant.MatchString(p.TenantID) || len(p.TenantID) < 3 || len(p.TenantID) > 32:
		return 400, "invalid_request", "bad tenant_id"
	case !reJob.MatchString(p.JobID):
		return 400, "invalid_request", "bad job_id"
	case !contains(validRoles, p.Role):
		return 400, "invalid_request", "bad role"
	case !reRB.MatchString(p.ReleaseBundleID):
		return 400, "invalid_request", "bad release_bundle_id"
	case len(p.Models) < 1 || len(p.Models) > 8:
		return 400, "invalid_request", "models must have 1..8 entries"
	case p.BudgetUSD <= 0 || p.BudgetUSD > 5000:
		return 400, "invalid_request", "budget_usd must be in (0, 5000]"
	case p.MaxConcurrency < 1 || p.MaxConcurrency > 16:
		return 400, "invalid_request", "max_concurrency must be in [1, 16]"
	}
	for _, m := range p.Models {
		if m == "" || len(m) > 200 {
			return 400, "invalid_request", "bad model id"
		}
	}
	if _, err := time.Parse(time.RFC3339, p.ExpiresAt); err != nil {
		return 400, "invalid_request", "expires_at must be RFC 3339"
	}
	if len(p.Experiment) > 8 {
		return 400, "invalid_request", "experiment has more than 8 properties"
	}
	if p.BudgetAccount != "" {
		if !reAcct.MatchString(p.BudgetAccount) {
			return 400, "invalid_request", "bad budget_account"
		}
		if a, ok := s.State.JobAccountOf(p.JobID); !ok || a.Account != p.BudgetAccount {
			return 409, "account_cap_conflict", "budget_account differs from the account bound to the job"
		}
	}
	if p.Stub != nil {
		if s.Cfg.Backend == "enterprise" {
			return 400, "stub_not_allowed", "this shim does not honor stub selection"
		}
		if !contains([]string{"tests", "evals", "scripts"}, p.Stub.Root) || len(p.Stub.Set) < 1 || len(p.Stub.Set) > 100 || len(p.Stub.Key) < 1 || len(p.Stub.Key) > 100 {
			return 400, "invalid_request", "bad stub"
		}
	}
	return 0, "", ""
}

func (s *Server) putPolicy(w http.ResponseWriter, r *http.Request) {
	if _, ok := s.serviceCaller(w, r, "policy"); !ok {
		return
	}
	runID := r.PathValue("run_id")
	var p Policy
	if err := decodeStrict(r, &p); err != nil {
		badRequest(w, "invalid RunPolicy: "+err.Error())
		return
	}
	if st, code, msg := s.validatePolicy(runID, &p); st != 0 {
		writeError(w, "", st, code, msg, nil)
		return
	}
	stored, conflict := s.State.PutPolicy(runID, p)
	if conflict {
		writeError(w, "", 409, "run_policy_mismatch", "a different policy is already registered for "+runID, nil)
		return
	}
	if s.Log != nil {
		s.Log.Info("run policy registered", "run_id", runID, "job_id", p.JobID, "tenant_id", p.TenantID, "role", p.Role, "models", strings.Join(p.Models, ","), "stub", p.Stub != nil)
	}
	jsonOut(w, 200, stored)
}

func (s *Server) getPolicy(w http.ResponseWriter, r *http.Request) {
	if _, ok := s.serviceCaller(w, r, "policy"); !ok {
		return
	}
	p, ok := s.State.GetPolicy(r.PathValue("run_id"))
	if !ok {
		writeError(w, "", 404, "run_policy_missing", "no policy", nil)
		return
	}
	jsonOut(w, 200, p)
}

func (s *Server) putJobBudget(w http.ResponseWriter, r *http.Request) {
	if _, ok := s.serviceCaller(w, r, "job_budget"); !ok {
		return
	}
	jobID := r.PathValue("job_id")
	if !reJob.MatchString(jobID) {
		badRequest(w, "bad job_id")
		return
	}
	if r.Header.Get("Idempotency-Key") == "" {
		badRequest(w, "Idempotency-Key header is required")
		return
	}
	var body struct {
		TenantID     string  `json:"tenant_id"`
		JobBudgetUSD float64 `json:"job_budget_usd"`
	}
	if err := decodeStrict(r, &body); err != nil {
		badRequest(w, "invalid body: "+err.Error())
		return
	}
	if !reTenant.MatchString(body.TenantID) || body.JobBudgetUSD <= 0 || body.JobBudgetUSD > 10000 {
		badRequest(w, "tenant_id and job_budget_usd in (0, 10000] are required")
		return
	}
	s.State.SetJobLimit(jobID, USDToMicros(body.JobBudgetUSD))
	jsonOut(w, 200, s.budgetState(&reqCtx{tenant: body.TenantID, jobID: jobID}))
}

func (s *Server) putJobAccount(w http.ResponseWriter, r *http.Request) {
	if _, ok := s.serviceCaller(w, r, "account"); !ok {
		return
	}
	jobID := r.PathValue("job_id")
	var a JobAccount
	if err := decodeStrict(r, &a); err != nil {
		badRequest(w, "invalid body: "+err.Error())
		return
	}
	if !reJob.MatchString(jobID) || !reAcct.MatchString(a.Account) || a.CapUSD <= 0 || a.CapUSD > 100000 ||
		(a.EvaluationMode != "" && a.EvaluationMode != "eval" && a.EvaluationMode != "shadow") {
		badRequest(w, "invalid JobBudgetAccount")
		return
	}
	switch s.State.BindJobAccount(jobID, a) {
	case "":
		jsonOut(w, 200, a)
	case "account_cap_conflict":
		writeError(w, "", 409, "account_cap_conflict", "account exists with a different cap", nil)
	default:
		writeError(w, "", 409, "account_cap_conflict", "job is bound to another account", nil)
	}
}

func (s *Server) models(w http.ResponseWriter, r *http.Request) {
	if _, ok := s.serviceCaller(w, r, "models"); !ok {
		return
	}
	type entry struct {
		GatewayModelID   string  `json:"gateway_model_id"`
		Protocol         string  `json:"protocol"`
		UpstreamModel    string  `json:"upstream_model"`
		PriceBookVersion *int    `json:"price_book_version"`
		RetiresAt        *string `json:"retires_at"`
		RetiresAtSource  *string `json:"retires_at_source"`
		ZDR              bool    `json:"zdr"`
	}
	var out []entry
	names := make([]string, 0, len(s.Cfg.File.ModelMap))
	for k := range s.Cfg.File.ModelMap {
		names = append(names, k)
	}
	sort.Strings(names)
	one := 1
	for _, k := range names {
		e := entry{GatewayModelID: k, UpstreamModel: s.Cfg.File.ModelMap[k], Protocol: protocolOfModel(k)}
		if _, ok := s.Cfg.Prices[k]; ok {
			e.PriceBookVersion = &one
		}
		if ce, ok := s.Cfg.File.Catalog[k]; ok {
			if ce.RetiresAt != "" {
				e.RetiresAt = &ce.RetiresAt
				src := ce.RetiresAtSource
				e.RetiresAtSource = &src
			}
			e.ZDR = ce.ZDR
		}
		out = append(out, e)
	}
	jsonOut(w, 200, map[string]any{"models": out, "as_of": s.now().UTC().Format(time.RFC3339)})
}

func protocolOfModel(m string) string {
	switch {
	case strings.HasPrefix(m, "anthropic/"):
		return "anthropic_messages"
	case strings.HasPrefix(m, "voyage/"), strings.Contains(m, "embedding"):
		return "embeddings"
	default:
		return "openai_responses"
	}
}

// RunBackground starts the janitor (reservations older than request timeout + 60 s).
func (s *Server) RunBackground(ctx context.Context) {
	go func() {
		t := time.NewTicker(60 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				if n := s.State.Janitor(11 * time.Minute); n > 0 && s.Log != nil {
					s.Log.Warn("janitor charged stale reservations", "count", n)
				}
			}
		}
	}()
}

// String is for debugging.
func (l Level) String() string { return fmt.Sprintf("%s(%s)", l.Name, l.Key) }
