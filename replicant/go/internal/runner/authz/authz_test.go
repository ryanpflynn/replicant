package authz

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/jose"
	"example.com/replicant/internal/runner/revoke"
)

type fixedRevoked map[string]bool

func (f fixedRevoked) Revoked(job, run string) (bool, string) { return f[job] || f[run], "test" }

func runAuthz() api.RunAuthz {
	return api.RunAuthz{TenantID: "payments", JobID: "job_01m5p7x7r0bt69j0h0fbxepw90", RunID: "run_gj3h3ep3phjfs9e23v768zrca5",
		Role: "implementer", RepoIDs: []string{"1"}, Audiences: []string{api.AudGateway, api.AudMemory},
		Scopes: []string{"gateway:invoke", "memory:read"}, BudgetUSD: 5, ActingFor: "svc_tester", Exp: time.Now().Unix() + 3600}
}

func token(t *testing.T, s *jose.Signer, a api.RunAuthz, aud string, mut func(*api.RunTokenClaims)) string {
	b := a.BudgetUSD
	c := api.RunTokenClaims{Iss: "http://runner-local:8480", Sub: "run:" + a.RunID, Aud: aud, Exp: a.Exp - 10, Iat: time.Now().Unix(), Jti: "rtk_x",
		TenantID: a.TenantID, JobID: a.JobID, RunID: a.RunID, Role: a.Role, RepoIDs: a.RepoIDs, Scopes: []string{"memory:read"},
		BudgetUSD: &b, ActingFor: a.ActingFor}
	if mut != nil {
		mut(&c)
	}
	tok, err := s.Sign("JWT", c)
	if err != nil {
		t.Fatal(err)
	}
	return tok
}

// The verifier fetches the Runner's JWKS (its only key source; there is no grant JWKS any more).
func TestTokenVerifiedAgainstRunnerJWKS(t *testing.T) {
	runner := jose.GenerateSigner("rt-1")
	other := jose.GenerateSigner("rt-1") // same kid, different key: a forged token
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/.well-known/jwks.json" {
			w.WriteHeader(404)
			return
		}
		_ = json.NewEncoder(w).Encode(jose.JWKS{Keys: []jose.JWK{runner.PublicJWK()}})
	}))
	defer srv.Close()
	a := runAuthz()
	v := &TokenVerifier{Audience: api.AudMemory, Issuers: []string{"http://runner-local:8480"},
		JWKS: &jose.RemoteJWKS{URLs: []string{srv.URL + "/.well-known/jwks.json"}}}
	ctx := context.Background()
	got, err := v.Verify(ctx, token(t, runner, a, api.AudMemory, nil))
	if err != nil {
		t.Fatalf("valid token rejected: %v", err)
	}
	if got.Claims.RunID != a.RunID || !got.HasScope("memory:read") {
		t.Fatalf("claims %+v", got.Claims)
	}
	cases := map[string]string{
		"wrong audience":   token(t, runner, a, api.AudGateway, nil),
		"untrusted issuer": token(t, runner, a, api.AudMemory, func(c *api.RunTokenClaims) { c.Iss = "http://evil" }),
		"forged signature": token(t, other, a, api.AudMemory, nil),
		"expired":          token(t, runner, a, api.AudMemory, func(c *api.RunTokenClaims) { c.Exp = time.Now().Unix() - 1 }),
		"sub mismatch":     token(t, runner, a, api.AudMemory, func(c *api.RunTokenClaims) { c.Sub = "run:run_other" }),
		"no acting_for":    token(t, runner, a, api.AudMemory, func(c *api.RunTokenClaims) { c.ActingFor = "" }),
		"identity missing": token(t, runner, a, api.AudMemory, func(c *api.RunTokenClaims) { c.TenantID = "" }),
	}
	for name, tok := range cases {
		if _, err := v.Verify(ctx, tok); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
}

func TestRevokedRunAndJobRefused(t *testing.T) {
	runner := jose.GenerateSigner("rt-1")
	a := runAuthz()
	v := &TokenVerifier{Audience: api.AudMemory, JWKS: &jose.RemoteJWKS{Static: []jose.JWK{runner.PublicJWK()}}}
	ctx := context.Background()
	for _, key := range []string{a.RunID, a.JobID} {
		v.Revoked = revoke.Multi{fixedRevoked{key: true}}
		if _, err := v.Verify(ctx, token(t, runner, a, api.AudMemory, nil)); err == nil {
			t.Errorf("revoked %s accepted", key)
		}
	}
	// the runner's feed as served at GET /v1/revocations, polled by a verifier
	feed := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(api.RevocationList{Revoked: []api.Revocation{{RunID: a.RunID, JobID: a.JobID, Reason: "job_revoked"}}, NextCursor: "1"})
	}))
	defer feed.Close()
	ex := &revoke.Exec{URLs: []string{feed.URL + "/v1/revocations"}, Token: "reader"}
	if err := ex.Poll(ctx); err != nil {
		t.Fatal(err)
	}
	v.Revoked = ex
	if _, err := v.Verify(ctx, token(t, runner, a, api.AudMemory, nil)); err == nil {
		t.Error("run on the runner revocation feed accepted")
	}
}

func TestWithin(t *testing.T) {
	a := runAuthz()
	b := a.BudgetUSD
	ok := api.RunTokenClaims{TenantID: a.TenantID, JobID: a.JobID, RunID: a.RunID, Role: a.Role, Aud: api.AudMemory, Exp: a.Exp,
		ActingFor: a.ActingFor, Scopes: []string{"memory:read"}, RepoIDs: []string{"1"}, BudgetUSD: &b}
	if err := Within(&ok, &a); err != nil {
		t.Fatal(err)
	}
	over := b + 1
	for name, mut := range map[string]func(*api.RunTokenClaims){
		"scope":  func(c *api.RunTokenClaims) { c.Scopes = []string{"context:read"} },
		"repo":   func(c *api.RunTokenClaims) { c.RepoIDs = []string{"2"} },
		"exp":    func(c *api.RunTokenClaims) { c.Exp = a.Exp + 1 },
		"aud":    func(c *api.RunTokenClaims) { c.Aud = api.AudContext },
		"budget": func(c *api.RunTokenClaims) { c.BudgetUSD = &over },
		"tenant": func(c *api.RunTokenClaims) { c.TenantID = "other" },
	} {
		c := ok
		mut(&c)
		if Within(&c, &a) == nil {
			t.Errorf("%s outside the run accepted", name)
		}
	}
}
