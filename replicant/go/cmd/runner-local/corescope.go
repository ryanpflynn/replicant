package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"slices"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/httpx"
)

// CoreScope reads a job's intake record from core: GET /internal/v1/jobs/{job_id}/run-scope?tenant_id=T with the
// runner's caller token (service:runner-local). It replaces the core-signed run grant (SIMPLIFY-D50): instead of
// trusting a signature over what the worker asked for, the runner compares the request with what core accepted.
type CoreScope struct {
	URL       string
	TokenFile string
	Client    *http.Client
}

// JobScope is core's answer (control-plane-api routes_runner.py).
type JobScope struct {
	JobID     string   `json:"job_id"`
	TenantID  string   `json:"tenant_id"`
	RepoIDs   []string `json:"repo_ids"`
	ActingFor string   `json:"acting_for"`
	Revoked   bool     `json:"revoked"`
}

// errJobUnknown: core has no such job for that tenant (404). Another tenant's job is indistinguishable.
var errJobUnknown = errors.New("job unknown to core for this tenant")

// Get returns the job's scope, errJobUnknown, or a transport/5xx error (the caller answers 503).
func (c *CoreScope) Get(ctx context.Context, tenantID, jobID string) (*JobScope, error) {
	cl := c.Client
	if cl == nil {
		cl = &http.Client{Timeout: 5 * time.Second}
	}
	u := fmt.Sprintf("%s/internal/v1/jobs/%s/run-scope?tenant_id=%s", c.URL, url.PathEscape(jobID), url.QueryEscape(tenantID))
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if tok, err := httpx.ReadToken(c.TokenFile); err == nil && tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	resp, err := cl.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	switch {
	case resp.StatusCode == http.StatusNotFound:
		return nil, errJobUnknown
	case resp.StatusCode != http.StatusOK:
		return nil, fmt.Errorf("core run-scope: HTTP %d", resp.StatusCode)
	}
	var js JobScope
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&js); err != nil {
		return nil, fmt.Errorf("core run-scope: %w", err)
	}
	return &js, nil
}

// checkScope is the admission rule of C-RUNNER 4.0.0: the run's tenant, repos and acting_for must be what core
// accepted for the job, and the job must not be revoked.
func checkScope(js *JobScope, rs *api.RunSpec) error {
	sc := rs.RunScope
	switch {
	case js.JobID != rs.JobID || js.TenantID != rs.TenantID:
		return fmt.Errorf("job %s is not a job of tenant %s", rs.JobID, rs.TenantID)
	case js.Revoked:
		return fmt.Errorf("job %s is revoked (cancelled, failed or taken over)", rs.JobID)
	case sc.ActingFor != js.ActingFor:
		return fmt.Errorf("run_scope.acting_for is not the job's requester")
	case len(sc.RepoIDs) == 0:
		return fmt.Errorf("run_scope.repo_ids is empty")
	}
	for _, id := range sc.RepoIDs {
		if !slices.Contains(js.RepoIDs, id) {
			return fmt.Errorf("repo %s is not a target of job %s", id, rs.JobID)
		}
	}
	need := []string{rs.Repo.RepoID}
	for _, a := range rs.AdditionalRepos {
		need = append(need, a.RepoID)
	}
	for _, id := range need {
		if !slices.Contains(sc.RepoIDs, id) {
			return fmt.Errorf("run_scope.repo_ids does not cover %s", id)
		}
	}
	return nil
}

// runAuthz binds the admitted scope to the run's identity, budget and overall deadline (the old grant's exp:
// startup + active deadline + finalize + 300 s).
func runAuthz(rs *api.RunSpec, runID string, now time.Time) api.RunAuthz {
	a := api.RunAuthz{TenantID: rs.TenantID, JobID: rs.JobID, RunID: runID, Role: rs.Role,
		RepoIDs: rs.RunScope.RepoIDs, Audiences: rs.RunScope.Audiences, Scopes: rs.RunScope.Scopes, ActingFor: rs.RunScope.ActingFor,
		Exp: now.Unix() + int64(rs.Timeouts.StartupSeconds+rs.Timeouts.ActiveDeadlineSeconds+rs.Timeouts.FinalizeSeconds) + 300}
	if rs.Limits != nil && rs.Limits.MaxCostUSD != nil {
		a.BudgetUSD = *rs.Limits.MaxCostUSD
	}
	return a
}
