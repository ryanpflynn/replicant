package shim

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"
)

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
