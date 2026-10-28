package ids

import (
	"strings"
	"testing"
)

func TestVectors(t *testing.T) {
	cases := []struct{ got, want string }{
		{Derived("run", "payments", "ik1:job_01m5p7x7r0bt69j0h0fbxepw90:implementer.c0.n1.run"), "run_gj3h3ep3phjfs9e23v768zrca5"},
		{Derived("run", "payments", "ik1:job_01m5p7x7r0bt69j0h0fbxepw90:qa.c0.n1.run"), "run_chxtsxqzcpds1fc1c4dvw9e4em"},
		{Derived("gate", "job_01m5p7x7r0bt69j0h0fbxepw90", "plan_approval", "c0", "n1"), "gate_r1a2avb51zgbsjy0x4ghyktt2e"},
		{JobTraceID("job_01m5p7x7r0bt69j0h0fbxepw90"), "fc0575170c8597ce9805552723c08d6e"},
	}
	for _, c := range cases {
		if c.got != c.want {
			t.Errorf("got %s want %s", c.got, c.want)
		}
	}
}

func TestULID(t *testing.T) {
	a, b := ULID("rtk"), ULID("rtk")
	if !strings.HasPrefix(a, "rtk_") || len(a) != 30 || a >= b {
		t.Fatalf("bad ulids %s %s", a, b)
	}
	if !regexpMatch(a) {
		t.Fatalf("alphabet %s", a)
	}
}

func regexpMatch(s string) bool {
	for _, c := range s[4:] {
		if !strings.ContainsRune(Alphabet, c) {
			return false
		}
	}
	return true
}

func TestUUID5(t *testing.T) {
	// Python: uuid.uuid5(uuid.NAMESPACE_URL, "replicant:run_gj3h3ep3phjfs9e23v768zrca5")
	got := SessionID("run_gj3h3ep3phjfs9e23v768zrca5")
	if got != "119cf1fa-b8ea-54df-89b4-1c44020bac16" {
		t.Fatalf("bad uuid5 %s", got)
	}
	if UUID5(NamespaceURL, "http://python.org/") != "4c565f0d-3f5a-5890-b41b-20cf47701c5e" {
		t.Fatalf("uuid5 vector mismatch: %s", UUID5(NamespaceURL, "http://python.org/"))
	}
}

// Golden vectors captured from the gateway shim's former ids.Derived(seed), which ULIDFromSeed replaces.
func TestULIDFromSeedGolden(t *testing.T) {
	for seed, want := range map[string]string{
		"":                                     "73p324567w3ga9nyzms2cpze94",
		"a":                                    "6ajy0h5jgvqq5fnghhped27q2d",
		"seed|0":                               "4y75j0wzv04skykfraz7py5fby",
		"seed|msg":                             "092beqxrgvn8nxbvd20w601hmn",
		"req|so":                               "2ee692wy93b7j0ke73afxgw8pb",
		"x|msg":                                "0x3ga4bfhrg9jbfr96py6pp19h",
		"abcdefghijklmnopqrstuvwxyz0123456789": "013z19jkhst98h85a0z1x6j29b",
	} {
		if got := ULIDFromSeed(seed); got != want {
			t.Errorf("ULIDFromSeed(%q) = %s, want %s", seed, got, want)
		}
	}
}

// The ID shape the gateway shim relies on (gwu_, run_, ...): prefix_ + 26 lower Crockford chars, unique.
func TestULIDShape(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 1000; i++ {
		id := ULID("gwu")
		if len(id) != 30 || id[:4] != "gwu_" || strings.ContainsAny(id[4:], "ilou") || !regexpMatch(id) || seen[id] {
			t.Fatal(id)
		}
		seen[id] = true
	}
}
