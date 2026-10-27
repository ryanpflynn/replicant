package main

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/jose"
)

// audienceScopes is the controller.md §4.3 table: the scopes each audience's token may carry.
var audienceScopes = map[string][]string{
	api.AudGateway: {"gateway:invoke"},
	api.AudMemory:  {"memory:read", "memory:search"},
	api.AudContext: {"context:read", "code_search:read"},
	api.AudMirror:  {"repo:fetch"},
	api.AudSandbox: {"credentials:aws", "inbox:read"},
}

// signSet mints one ES256 token per audience in the grant (only those in `only` when non-empty), each
// embedding the grant and claiming a subset of it, exp = min(now + 60 min, grant.exp) (C-RUNNER, CCR-09-01/02).
func signSet(s *jose.Signer, issuer, grant string, g *api.RunGrantClaims, steeringLive bool, now time.Time, only []string) (map[string]api.TokenEntry, error) {
	exp := now.Add(time.Hour).Unix()
	if g.Exp < exp {
		exp = g.Exp
	}
	if exp <= now.Unix() {
		return nil, fmt.Errorf("grant expired")
	}
	out := map[string]api.TokenEntry{}
	for _, aud := range g.Audiences {
		if len(only) > 0 && !slices.Contains(only, aud) {
			continue
		}
		scopes := []string{}
		for _, sc := range audienceScopes[aud] {
			if sc == "inbox:read" && !steeringLive {
				continue
			}
			if slices.Contains(g.Scopes, sc) {
				scopes = append(scopes, sc)
			}
		}
		budget := g.BudgetUSD
		c := api.RunTokenClaims{
			Iss: issuer, Sub: "run:" + g.RunID, Aud: aud, Exp: exp, Iat: now.Unix(), Jti: ids.ULID("rtk"),
			TenantID: g.TenantID, JobID: g.JobID, RunID: g.RunID, Role: g.Role, RepoIDs: g.RepoIDs,
			Scopes: scopes, BudgetUSD: &budget, ActingFor: g.ActingFor, Grant: grant, GrantJti: g.Jti,
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
