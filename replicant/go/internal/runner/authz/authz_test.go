package authz

import (
	"context"
	"testing"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/jose"
	"example.com/replicant/internal/runner/revoke"
)

type fixedRevoked map[string]bool

func (f fixedRevoked) Revoked(job, run string) (bool, string) { return f[job] || f[run], "test" }

func setup(t *testing.T) (*jose.Signer, *jose.Signer, *TokenVerifier, api.RunGrantClaims, string) {
	core := jose.GenerateSigner("rg-local-1")
	runner := jose.GenerateSigner("rt-1")
	now := time.Now().Unix()
	g := api.RunGrantClaims{Iss: "replicant-local-core", Typ: api.GrantTyp, Jti: "rgr_01m5p7x7s0kd3m8q2v6x9z4b7c", Iat: now, Exp: now + 3600,
		TenantID: "payments", JobID: "job_01m5p7x7r0bt69j0h0fbxepw90", RunID: "run_gj3h3ep3phjfs9e23v768zrca5", Role: "implementer",
		RepoIDs: []string{"1"}, Audiences: []string{api.AudGateway, api.AudMirror}, Scopes: []string{"gateway:invoke", "repo:fetch"},
		BudgetUSD: 5, ActingFor: "svc_tester", SpecDigest: "sha256:x"}
	gs, err := core.Sign(api.GrantTyp, g)
	if err != nil {
		t.Fatal(err)
	}
	v := &TokenVerifier{Audience: api.AudMirror, Issuers: []string{"http://runner-local:8480"},
		JWKS:   &jose.RemoteJWKS{Static: []jose.JWK{runner.PublicJWK()}},
		Grants: &GrantVerifier{Issuer: "replicant-local-core", JWKS: &jose.RemoteJWKS{Static: []jose.JWK{core.PublicJWK()}}}}
	return core, runner, v, g, gs
}

func token(t *testing.T, s *jose.Signer, g api.RunGrantClaims, gs, aud string, mut func(*api.RunTokenClaims)) string {
	b := g.BudgetUSD
	c := api.RunTokenClaims{Iss: "http://runner-local:8480", Sub: "run:" + g.RunID, Aud: aud, Exp: g.Exp - 10, Iat: g.Iat, Jti: "rtk_x",
		TenantID: g.TenantID, JobID: g.JobID, RunID: g.RunID, Role: g.Role, RepoIDs: g.RepoIDs, Scopes: []string{"repo:fetch"},
		BudgetUSD: &b, ActingFor: g.ActingFor, Grant: gs, GrantJti: g.Jti}
	if mut != nil {
		mut(&c)
	}
	tok, err := s.Sign("JWT", c)
	if err != nil {
		t.Fatal(err)
	}
	return tok
}

func TestTokenVerifier(t *testing.T) {
	core, runner, v, g, gs := setup(t)
	ctx := context.Background()
	if _, err := v.Verify(ctx, token(t, runner, g, gs, api.AudMirror, nil)); err != nil {
		t.Fatalf("valid token rejected: %v", err)
	}
	cases := map[string]string{
		"wrong audience":   token(t, runner, g, gs, api.AudGateway, nil),
		"scope outside":    token(t, runner, g, gs, api.AudMirror, func(c *api.RunTokenClaims) { c.Scopes = []string{"memory:read"} }),
		"repo outside":     token(t, runner, g, gs, api.AudMirror, func(c *api.RunTokenClaims) { c.RepoIDs = []string{"2"} }),
		"exp beyond grant": token(t, runner, g, gs, api.AudMirror, func(c *api.RunTokenClaims) { c.Exp = g.Exp + 100 }),
		"untrusted issuer": token(t, runner, g, gs, api.AudMirror, func(c *api.RunTokenClaims) { c.Iss = "http://evil" }),
		"forged signature": token(t, core, g, gs, api.AudMirror, nil),
	}
	for name, tok := range cases {
		if _, err := v.Verify(ctx, tok); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
	v.Revoked = revoke.Multi{fixedRevoked{g.RunID: true}}
	if _, err := v.Verify(ctx, token(t, runner, g, gs, api.AudMirror, nil)); err == nil {
		t.Error("revoked run accepted")
	}
}
