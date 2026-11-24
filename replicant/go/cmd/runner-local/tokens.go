package main

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/authz"
	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/jose"
)

// audienceScopes is the controller.md §4.3 table: the scopes each audience's token may carry.
var audienceScopes = map[string][]string{
	api.AudGateway: {"gateway:invoke"},
	api.AudMemory:  {"memory:read", "memory:search"},
	api.AudContext: {"context:read", "code_search:read"},
	api.AudSandbox: {"credentials:aws", "inbox:read"},
}

// signSet mints one ES256 token per audience in the admitted run scope (only those in `only` when non-empty),
// each claiming a subset of it, exp = min(now + 60 min, the run's overall deadline) (C-RUNNER 4.0.0, CCR-09-01).
// The runner's own key signs; verifiers check the runner JWKS and the runner revocation feed only (SIMPLIFY-D50).
func signSet(s *jose.Signer, issuer string, a *api.RunAuthz, steeringLive bool, now time.Time, only []string) (map[string]api.TokenEntry, error) {
	exp := now.Add(time.Hour).Unix()
	if a.Exp < exp {
		exp = a.Exp
	}
	if exp <= now.Unix() {
		return nil, fmt.Errorf("run past its overall deadline")
	}
	out := map[string]api.TokenEntry{}
	for _, aud := range a.Audiences {
		if len(only) > 0 && !slices.Contains(only, aud) {
			continue
		}
		if _, known := audienceScopes[aud]; !known { // e.g. repo-mirror in plans of in-flight jobs (SIMPLIFY-D40)
			continue
		}
		scopes := []string{}
		for _, sc := range audienceScopes[aud] {
			if sc == "inbox:read" && !steeringLive {
				continue
			}
			if slices.Contains(a.Scopes, sc) {
				scopes = append(scopes, sc)
			}
		}
		c := api.RunTokenClaims{
			Iss: issuer, Sub: "run:" + a.RunID, Aud: aud, Exp: exp, Iat: now.Unix(), Jti: ids.ULID("rtk"),
			TenantID: a.TenantID, JobID: a.JobID, RunID: a.RunID, Role: a.Role, RepoIDs: a.RepoIDs,
			Scopes: scopes, ActingFor: a.ActingFor,
		}
		if a.BudgetUSD > 0 {
			budget := a.BudgetUSD
			c.BudgetUSD = &budget
		}
		if err := authz.Within(&c, a); err != nil { // defence in depth: never sign outside the admitted run
			return nil, err
		}
		tok, err := s.Sign(api.TokenTyp, c)
		if err != nil {
			return nil, err
		}
		out[aud] = api.TokenEntry{Token: tok, ExpiresAt: time.Unix(exp, 0).UTC()}
	}
	return out, nil
}

// writeTokenFiles writes $STATE/runs/<run_id>/tokens/<audience> (mode 0400, uid 10000).
func writeTokenFiles(stateDir, runID string, set map[string]api.TokenEntry) error {
	dir := filepath.Join(stateDir, "runs", runID, "tokens")
	_ = os.Chmod(dir, 0o700)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	for aud, e := range set {
		p := filepath.Join(dir, aud)
		_ = os.Remove(p)
		if err := os.WriteFile(p, []byte(e.Token), 0o400); err != nil {
			return err
		}
		_ = os.Chown(p, 10000, 10000)
	}
	_ = os.Chown(dir, 10000, 10000)
	return os.Chmod(dir, 0o500)
}

func removeTokenFiles(stateDir, runID string) {
	dir := filepath.Join(stateDir, "runs", runID, "tokens")
	_ = os.Chmod(dir, 0o700)
	_ = os.RemoveAll(dir)
}
