// Package authz implements run-grant verification (Runner admission, RS-10) and the C-RUNNER 3.0.0
// run-token verifier obligations 1–5 shared by the sandbox listener and repo-mirror.
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

// GrantVerifier verifies core-signed run grants.
type GrantVerifier struct {
	Issuer  string
	JWKS    *jose.RemoteJWKS
	Revoked revoke.Checker
	Now     func() time.Time
	Skew    time.Duration
}

func (g *GrantVerifier) now() time.Time {
	if g.Now != nil {
		return g.Now()
	}
	return time.Now()
}

// Verify checks signature, typ, iss, exp and revocation and returns the claims.
func (g *GrantVerifier) Verify(ctx context.Context, grant string) (*api.RunGrantClaims, error) {
	p, err := g.JWKS.VerifyToken(ctx, grant)
	if err != nil {
		return nil, fmt.Errorf("grant signature: %w", err)
	}
	if p.Header.Typ != api.GrantTyp {
		return nil, fmt.Errorf("grant typ %q", p.Header.Typ)
	}
	var c api.RunGrantClaims
	if err := json.Unmarshal(p.Payload, &c); err != nil {
		return nil, fmt.Errorf("grant claims: %w", err)
	}
	if c.Typ != api.GrantTyp {
		return nil, fmt.Errorf("grant claim typ %q", c.Typ)
	}
	if g.Issuer != "" && c.Iss != g.Issuer {
		return nil, fmt.Errorf("grant iss %q", c.Iss)
	}
	if g.now().Unix() > c.Exp+int64(g.Skew.Seconds()) {
		return nil, fmt.Errorf("grant expired")
	}
	if g.Revoked != nil {
		if ok, why := g.Revoked.Revoked(c.JobID, c.RunID); ok {
			return nil, fmt.Errorf("grant revoked (%s)", why)
		}
	}
	return &c, nil
}

// TokenVerifier verifies per-audience run tokens.
type TokenVerifier struct {
	Audience string // the verifier's own name
	Issuers  []string
	JWKS     *jose.RemoteJWKS
	Grants   *GrantVerifier
	Revoked  revoke.Checker // core list and exec feed
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
	Grant  api.RunGrantClaims
}

// Verify applies obligations 1–3 and 5; obligation 4 (authorization) is the caller's.
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
	if c.Sub != "run:"+c.RunID {
		return nil, fmt.Errorf("sub mismatch")
	}
	g, err := v.Grants.Verify(ctx, c.Grant)
	if err != nil {
		return nil, err
	}
	if err := Subset(&c, g); err != nil {
		return nil, err
	}
	if v.Revoked != nil {
		if ok, why := v.Revoked.Revoked(c.JobID, c.RunID); ok {
			return nil, fmt.Errorf("revoked (%s)", why)
		}
	}
	return &Verified{Claims: c, Grant: *g}, nil
}

// Subset checks the token-within-grant relation (CCR-09-02).
func Subset(c *api.RunTokenClaims, g *api.RunGrantClaims) error {
	switch {
	case c.GrantJti != g.Jti:
		return fmt.Errorf("grant_jti mismatch")
	case c.RunID != g.RunID || c.JobID != g.JobID || c.TenantID != g.TenantID:
		return fmt.Errorf("token identity outside grant")
	case c.Role != g.Role:
		return fmt.Errorf("role outside grant")
	case !slices.Contains(g.Audiences, c.Aud):
		return fmt.Errorf("aud outside grant")
	case c.Exp > g.Exp:
		return fmt.Errorf("exp beyond grant")
	case c.ActingFor != g.ActingFor:
		return fmt.Errorf("acting_for mismatch")
	}
	for _, s := range c.Scopes {
		if !slices.Contains(g.Scopes, s) {
			return fmt.Errorf("scope %s outside grant", s)
		}
	}
	for _, r := range c.RepoIDs {
		if !slices.Contains(g.RepoIDs, r) {
			return fmt.Errorf("repo %s outside grant", r)
		}
	}
	if c.BudgetUSD != nil && *c.BudgetUSD > g.BudgetUSD+1e-9 {
		return fmt.Errorf("budget outside grant")
	}
	return nil
}

// HasScope reports whether the token carries scope s.
func (v *Verified) HasScope(s string) bool { return slices.Contains(v.Claims.Scopes, s) }
