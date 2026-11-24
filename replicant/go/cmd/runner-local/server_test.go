package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/authz"
	"example.com/replicant/internal/runner/jose"
)

func TestParseIdemKey(t *testing.T) {
	if k, err := parseIdemKey(`"ik1:job_01m5p7x7r0bt69j0h0fbxepw90:implementer.c0.n1.run"`); err != nil || k != "ik1:job_01m5p7x7r0bt69j0h0fbxepw90:implementer.c0.n1.run" {
		t.Fatalf("%q %v", k, err)
	}
	for _, bad := range []string{`ik1:job_x:a.c0.n1.run`, `"ik2:x"`, `"ik1:a b"`, `"`} {
		if _, err := parseIdemKey(bad); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestSignSetPerAudience(t *testing.T) {
	s := jose.GenerateSigner("k")
	now := time.Now()
	a := &api.RunAuthz{Exp: now.Add(30 * time.Minute).Unix(), RunID: "run_x", JobID: "job_x", TenantID: "t", Role: "qa",
		Audiences: []string{api.AudSandbox, api.AudGateway, "repo-mirror"}, Scopes: []string{"repo:fetch", "credentials:aws", "gateway:invoke"},
		BudgetUSD: 2, ActingFor: "svc_x", RepoIDs: []string{"R_1"}}
	set, err := signSet(s, "http://runner-local:8480", a, false, now, nil)
	if err != nil || len(set) != 2 {
		t.Fatalf("%v %d", err, len(set))
	}
	if _, ok := set["repo-mirror"]; ok { // retired audience of in-flight plans (SIMPLIFY-D40): no token minted
		t.Fatal("repo-mirror token minted")
	}
	v := &authz.TokenVerifier{Audience: api.AudGateway, Issuers: []string{"http://runner-local:8480"},
		JWKS: &jose.RemoteJWKS{Static: []jose.JWK{s.PublicJWK()}}}
	for aud, e := range set {
		var c api.RunTokenClaims
		_ = jose.UnsafeClaims(e.Token, &c)
		if c.Aud != aud || c.Exp != a.Exp || c.ActingFor != "svc_x" || c.BudgetUSD == nil || *c.BudgetUSD != 2 {
			t.Errorf("%s: %+v", aud, c)
		}
		if aud == api.AudSandbox && (len(c.Scopes) != 1 || c.Scopes[0] != "credentials:aws") {
			t.Errorf("sandbox scopes %v (inbox:read must need steering)", c.Scopes)
		}
	}
	if _, err := v.Verify(context.Background(), set[api.AudGateway].Token); err != nil {
		t.Fatalf("gateway token does not verify against the runner key: %v", err)
	}
	only, _ := signSet(s, "i", a, false, now, []string{api.AudSandbox})
	if len(only) != 1 {
		t.Fatal("cancel refresh must mint only runner-sandbox")
	}
	a.Exp = now.Unix() - 1
	if _, err := signSet(s, "i", a, false, now, nil); err == nil {
		t.Fatal("run past its deadline minted")
	}
}

func scopeSpec() *api.RunSpec {
	return &api.RunSpec{TenantID: "payments", JobID: "job_a", Role: "implementer", Repo: api.Repo{RepoID: "R_1"},
		RunScope: api.RunScope{RepoIDs: []string{"R_1"}, Audiences: []string{api.AudGateway}, Scopes: []string{"gateway:invoke"}, ActingFor: "usr_a"}}
}

func TestCheckScopeAgainstCore(t *testing.T) {
	core := &JobScope{JobID: "job_a", TenantID: "payments", RepoIDs: []string{"R_1", "R_2"}, ActingFor: "usr_a"}
	if err := checkScope(core, scopeSpec()); err != nil {
		t.Fatal(err)
	}
	for name, mut := range map[string]func(*api.RunSpec, *JobScope){
		"other tenant's job":        func(rs *api.RunSpec, _ *JobScope) { rs.TenantID = "other" },
		"repo not a job target":     func(rs *api.RunSpec, _ *JobScope) { rs.RunScope.RepoIDs = []string{"R_1", "R_9"} },
		"repo not in scope":         func(rs *api.RunSpec, _ *JobScope) { rs.Repo.RepoID = "R_2" },
		"additional repo uncovered": func(rs *api.RunSpec, _ *JobScope) { rs.AdditionalRepos = []api.Repo{{RepoID: "R_2"}} },
		"acting for someone else":   func(rs *api.RunSpec, _ *JobScope) { rs.RunScope.ActingFor = "usr_b" },
		"empty scope":               func(rs *api.RunSpec, _ *JobScope) { rs.RunScope.RepoIDs = nil },
		"revoked job":               func(_ *api.RunSpec, js *JobScope) { js.Revoked = true },
	} {
		rs, js := scopeSpec(), *core
		mut(rs, &js)
		if checkScope(&js, rs) == nil {
			t.Errorf("%s accepted", name)
		}
	}
}

func TestCoreScopeClient(t *testing.T) {
	tokFile := filepath.Join(t.TempDir(), "tok")
	_ = os.WriteFile(tokFile, []byte("runner-cp\n"), 0o600)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer runner-cp" {
			w.WriteHeader(401)
			return
		}
		if r.URL.Path != "/internal/v1/jobs/job_a/run-scope" || r.URL.Query().Get("tenant_id") != "payments" {
			w.WriteHeader(404)
			return
		}
		_, _ = w.Write([]byte(`{"job_id":"job_a","tenant_id":"payments","repo_ids":["R_1"],"acting_for":"usr_a","revoked":false}`))
	}))
	defer srv.Close()
	c := &CoreScope{URL: srv.URL, TokenFile: tokFile}
	js, err := c.Get(context.Background(), "payments", "job_a")
	if err != nil || js.ActingFor != "usr_a" || len(js.RepoIDs) != 1 {
		t.Fatalf("%+v %v", js, err)
	}
	if _, err := c.Get(context.Background(), "other", "job_a"); !errors.Is(err, errJobUnknown) {
		t.Fatalf("another tenant's job must look unknown: %v", err)
	}
	if _, err := (&CoreScope{URL: srv.URL}).Get(context.Background(), "payments", "job_a"); err == nil || errors.Is(err, errJobUnknown) {
		t.Fatalf("unauthenticated must be an outage, not a denial: %v", err)
	}
}

func TestCoreRevocationsReachTheFeed(t *testing.T) {
	dir := t.TempDir()
	st, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range [][2]string{{"run_1", "job_a"}, {"run_2", "job_a"}, {"run_3", "job_b"}, {"run_4", "job_c"}} {
		rec := &Record{}
		rec.AgentRun.Spec.RunID, rec.AgentRun.Spec.JobID = r[0], r[1]
		if err := st.Create(rec); err != nil {
			t.Fatal(err)
		}
	}
	feed := NewFeed(dir, nil, "", "", time.Hour, nil)
	propagateCoreRevocations(context.Background(), st, feed, []string{"job_a"}, []string{"run_3"})
	for run, want := range map[string]bool{"run_1": true, "run_2": true, "run_3": true, "run_4": false} {
		if feed.IsRevoked(run) != want {
			t.Errorf("%s revoked=%v", run, !want)
		}
	}
	if l := feed.Since(""); len(l.Revoked) != 3 || l.Revoked[0].Reason != "job_revoked" {
		t.Fatalf("%+v", l)
	}
}

func TestLegacyRecordMigrates(t *testing.T) {
	old := []byte(`{"agentrun":{"spec":{"runId":"run_x","runGrant":"a.b.c"}},"local":{"grant":{"run_id":"run_x","acting_for":"usr_a","exp":5,"audiences":["ai-gateway"]}}}`)
	var r Record
	if err := json.Unmarshal(migrateLegacy(old), &r); err != nil {
		t.Fatal(err)
	}
	if r.Local.Scope.ActingFor != "usr_a" || r.Local.Scope.Exp != 5 || r.AgentRun.Spec.RunID != "run_x" {
		t.Fatalf("%+v", r.Local.Scope)
	}
}

func TestKillSwitchFile(t *testing.T) {
	p := filepath.Join(t.TempDir(), "ks.json")
	_ = os.WriteFile(p, []byte(`{"switches":[{"scope":"repo","scope_value":"git.x/acme/r","switch":"starts","engaged":true,"issued_at":"2026-10-24T00:00:00Z"},
	 {"scope":"repo","scope_value":"git.x/acme/r","switch":"starts","engaged":false,"issued_at":"2026-10-24T01:00:00Z"},
	 {"scope":"bundle","scope_value":"rb_x","switch":"starts","engaged":true,"issued_at":"2026-10-24T00:00:00Z"}]}`), 0o600)
	k := &KillSwitches{Source: "file://" + p}
	if f, _ := k.Check(context.Background(), "t", []string{"git.x/acme/r"}, "rb_y"); f != "" {
		t.Fatalf("latest issued_at must win, got %s", f)
	}
	if f, _ := k.Check(context.Background(), "t", nil, "rb_x"); f != "release_bundle_id" {
		t.Fatalf("bundle switch: %q", f)
	}
	missing := &KillSwitches{Source: "file://" + p + ".absent"}
	if f, err := missing.Check(context.Background(), "t", nil, "rb_x"); f != "" || err != nil {
		t.Fatalf("absent mirror = no switches: %q %v", f, err)
	}
}

func TestFeedCursor(t *testing.T) {
	f := NewFeed(t.TempDir(), nil, "", "", time.Hour, nil)
	f.Add(context.Background(), "run_a", "job_a", "Succeeded")
	f.Add(context.Background(), "run_a", "job_a", "Succeeded")
	l := f.Since("")
	if len(l.Revoked) != 1 || l.NextCursor != "1" {
		t.Fatalf("%+v", l)
	}
	f.Add(context.Background(), "run_b", "job_b", "Failed")
	if l2 := f.Since(l.NextCursor); len(l2.Revoked) != 1 || l2.Revoked[0].RunID != "run_b" {
		t.Fatalf("%+v", l2)
	}
}
