package shim

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"example.com/replicant/internal/runner/ids"
)

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
		rc := &reqCtx{start: s.now(), requestID: ids.ULID("gwu"), protocol: protocol, evalMode: "none"}
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
