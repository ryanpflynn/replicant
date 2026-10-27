package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// TestHarnessOnGoRepo runs install/unit/build on a throw-away module with one failing test.
func TestHarnessOnGoRepo(t *testing.T) {
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("go not on PATH")
	}
	repo := t.TempDir()
	w := func(p, s string) {
		_ = os.MkdirAll(filepath.Dir(filepath.Join(repo, p)), 0o755)
		_ = os.WriteFile(filepath.Join(repo, p), []byte(s), 0o644)
	}
	w("go.mod", "module example.com/q\n\ngo 1.26\n")
	w("a.go", "package q\n\nfunc Add(a, b int) int { return a - b }\n")
	w("a_test.go", "package q\n\nimport \"testing\"\n\nfunc TestAdd(t *testing.T) {\n\tt.Run(\"one\", func(t *testing.T) { if Add(1, 1) != 2 { t.Fatal(\"bad\") } })\n\tt.Run(\"zero\", func(t *testing.T) {})\n}\n")
	for _, a := range [][]string{{"init", "-q"}, {"add", "-A"}, {"-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"}} {
		c := exec.Command("git", a...)
		c.Dir = repo
		if out, err := c.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %s", a, out)
		}
	}
	out := filepath.Join(t.TempDir(), "qa")
	if rc := run(t.TempDir(), out, filepath.Join(t.TempDir(), "none.json"), repo); rc != 0 {
		t.Fatalf("rc %d", rc)
	}
	b, err := os.ReadFile(filepath.Join(out, "result.json"))
	if err != nil {
		t.Fatal(err)
	}
	var r struct {
		Verdict, ExitClass string `json:"-"`
		V                  string `json:"verdict"`
		E                  string `json:"exit_class"`
		RR                 struct {
			Counts map[string]int `json:"counts"`
		} `json:"role_result"`
	}
	_ = json.Unmarshal(b, &r)
	if r.V != "fail" || r.E != "fail" || r.RR.Counts["failed"] != 1 || r.RR.Counts["passed"] != 1 {
		t.Fatalf("%s", b)
	}
	if _, err := os.Stat(filepath.Join(out, "outputs.entries.json")); err != nil {
		t.Fatal(err)
	}
}
