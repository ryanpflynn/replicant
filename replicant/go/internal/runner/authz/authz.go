// Package authz implements the C-RUNNER 4.0.0 run-token verifier obligations shared by the sandbox listener and
// the other run-token verifiers. There is no run grant any more (SIMPLIFY-D50): the Runner is the only issuer, it
// checks each run against core's intake record before it mints, and it is the only revocation source.
package authz

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/jose"
	"example.com/replicant/internal/runner/revoke"
)

// TokenVerifier verifies per-audience run tokens.
type TokenVerifier struct {
	Audience string // the verifier's own name
	Issuers  []string
	JWKS     *jose.RemoteJWKS // the Runner's JWKS
	Revoked  revoke.Checker   // the Runner's revocation feed (it carries core's job revocations)
	Now      func() time.Time
}

func (v *TokenVerifier) now() time.Time {
	if v.Now != nil {
		return v.Now()
	}
	return time.Now()
}

// Verified is the result of a successful verification.
type Verified struct {
	Claims api.RunTokenClaims
}

// Verify applies obligations 1 (signature, aud, iss, exp, sub) and 3 (revocation); obligation 2
// (authorization) is the caller's.
func (v *TokenVerifier) Verify(ctx context.Context, token string) (*Verified, error) {
	p, err := v.JWKS.VerifyToken(ctx, token)
	if err != nil {
		return nil, fmt.Errorf("token signature: %w", err)
	}
	var c api.RunTokenClaims
	if err := json.Unmarshal(p.Payload, &c); err != nil {
		// legacy array aud is not accepted outside the N/N+1 window
		return nil, fmt.Errorf("token claims: %w", err)
	}
	if c.Aud != v.Audience {
		return nil, fmt.Errorf("aud %q != %q", c.Aud, v.Audience)
	}
	if len(v.Issuers) > 0 && !slices.Contains(v.Issuers, c.Iss) {
		return nil, fmt.Errorf("iss %q not trusted", c.Iss)
	}
	if v.now().Unix() >= c.Exp {
		return nil, fmt.Errorf("token expired")
	}
	if c.RunID == "" || c.JobID == "" || c.TenantID == "" || c.ActingFor == "" {
		return nil, fmt.Errorf("token identity incomplete")
	}
	if c.Sub != "run:"+c.RunID {
		return nil, fmt.Errorf("sub mismatch")
	}
	if v.Revoked != nil {
		if ok, why := v.Revoked.Revoked(c.JobID, c.RunID); ok {
			return nil, fmt.Errorf("revoked (%s)", why)
		}
	}
	return &Verified{Claims: c}, nil
}

// Within checks that a token's claims stay inside the run's admitted authorization (used by the Runner before
// it signs; verifiers trust the Runner's signature instead).
func Within(c *api.RunTokenClaims, a *api.RunAuthz) error {
	switch {
	case c.RunID != a.RunID || c.JobID != a.JobID || c.TenantID != a.TenantID:
		return fmt.Errorf("token identity outside the run")
	case c.Role != a.Role:
		return fmt.Errorf("role outside the run")
	case !slices.Contains(a.Audiences, c.Aud):
		return fmt.Errorf("aud outside the run scope")
	case c.Exp > a.Exp:
		return fmt.Errorf("exp beyond the run")
	case c.ActingFor != a.ActingFor:
		return fmt.Errorf("acting_for mismatch")
	}
	for _, s := range c.Scopes {
		if !slices.Contains(a.Scopes, s) {
			return fmt.Errorf("scope %s outside the run scope", s)
		}
	}
	for _, r := range c.RepoIDs {
		if !slices.Contains(a.RepoIDs, r) {
			return fmt.Errorf("repo %s outside the run scope", r)
		}
	}
	if c.BudgetUSD != nil && *c.BudgetUSD > a.BudgetUSD+1e-9 {
		return fmt.Errorf("budget outside the run")
	}
	return nil
}

// HasScope reports whether the token carries scope s.
func (v *Verified) HasScope(s string) bool { return slices.Contains(v.Claims.Scopes, s) }
