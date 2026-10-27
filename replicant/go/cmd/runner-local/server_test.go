package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"example.com/replicant/internal/runner/api"
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
	g := &api.RunGrantClaims{Jti: "rgr_x", Exp: now.Add(30 * time.Minute).Unix(), RunID: "run_x", JobID: "job_x", TenantID: "t", Role: "qa",
		Audiences: []string{api.AudSandbox, api.AudMirror}, Scopes: []string{"repo:fetch", "credentials:aws"}, BudgetUSD: 2, ActingFor: "svc_x"}
	set, err := signSet(s, "http://runner-local:8480", "a.b.c", g, false, now, nil)
	if err != nil || len(set) != 2 {
		t.Fatalf("%v %d", err, len(set))
	}
	for aud, e := range set {
		var c api.RunTokenClaims
		_ = jose.UnsafeClaims(e.Token, &c)
		if c.Aud != aud || c.Exp != g.Exp || c.GrantJti != "rgr_x" || c.Grant != "a.b.c" {
			t.Errorf("%s: %+v", aud, c)
		}
		if aud == api.AudSandbox && (len(c.Scopes) != 1 || c.Scopes[0] != "credentials:aws") {
			t.Errorf("sandbox scopes %v (inbox:read must need steering)", c.Scopes)
		}
	}
	only, _ := signSet(s, "i", "a.b.c", g, false, now, []string{api.AudSandbox})
	if len(only) != 1 {
		t.Fatal("cancel refresh must mint only runner-sandbox")
	}
	g.Exp = now.Unix() - 1
	if _, err := signSet(s, "i", "a.b.c", g, false, now, nil); err == nil {
		t.Fatal("expired grant minted")
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
