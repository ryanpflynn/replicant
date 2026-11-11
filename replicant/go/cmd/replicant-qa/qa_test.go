package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"example.com/replicant/internal/runner/schemas"
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

type qaOut struct {
	Verdict   string         `json:"verdict"`
	ExitClass string         `json:"exit_class"`
	RR        map[string]any `json:"role_result"`
}

// runWithCommands runs the harness on a repo with no go.mod, the given environment.commands and stub binaries on PATH.
func runWithCommands(t *testing.T, commands map[string]any, stubs map[string]string) qaOut {
	t.Helper()
	repo, bin := t.TempDir(), t.TempDir()
	_ = os.WriteFile(filepath.Join(repo, "pyproject.toml"), []byte("[project]\nname='x'\n"), 0o644)
	for name, script := range stubs {
		if err := os.WriteFile(filepath.Join(bin, name), []byte("#!/bin/sh\n"+script), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", bin+":/usr/bin:/bin")
	planPath := filepath.Join(t.TempDir(), "run-plan.json")
	env := map[string]any{"codebase_type": "python_service"}
	if commands != nil {
		env["commands"] = commands
	}
	b, _ := json.Marshal(map[string]any{"environment": env, "workspace": map[string]any{"base_sha": strings.Repeat("a", 40)}})
	_ = os.WriteFile(planPath, b, 0o644)
	out := filepath.Join(t.TempDir(), "qa")
	if rc := run(t.TempDir(), out, planPath, repo); rc != 0 {
		t.Fatalf("rc %d", rc)
	}
	rb, err := os.ReadFile(filepath.Join(out, "result.json"))
	if err != nil {
		t.Fatal(err)
	}
	var r qaOut
	if err := json.Unmarshal(rb, &r); err != nil {
		t.Fatal(err)
	}
	if err := schemas.Validate(schemas.RoleQA, r.RR); err != nil {
		t.Fatalf("role_result does not validate: %v\n%s", err, rb)
	}
	return r
}

func stageOf(r qaOut, name string) map[string]any {
	for _, s := range r.RR["stages"].([]any) {
		if m := s.(map[string]any); m["stage"] == name {
			return m
		}
	}
	return nil
}

func TestCommandsPythonAndNode(t *testing.T) {
	uv := `case "$*" in
"sync --frozen") echo "Resolved 3 packages"; exit 0;;
"run pytest -q") printf 'F..\nFAILED tests/test_a.py::test_x - assert 1 == 2\n1 failed, 2 passed in 0.03s\n'; exit 1;;
"run ruff check .") echo "All checks passed!"; exit 0;;
esac
exit 3
`
	npm := `case "$1" in
ci) exit 0;;
run) echo "built"; exit 0;;
test) printf '\n Test Files  1 passed (1)\n      Tests  3 passed (3)\n'; exit 0;;
esac
exit 3
`
	for _, c := range []struct {
		name               string
		commands           map[string]any
		stubs              map[string]string
		verdict, reason    string
		stage, status, cs  string
		detail             string
		passed, failed, nT int
	}{
		{"python test failure", map[string]any{"install": "uv sync --frozen", "build": nil, "test": "uv run pytest -q", "lint": "uv run ruff check ."},
			map[string]string{"uv": uv}, "fail", "regression", "unit", "failed", "test_failure", "", 2, 1, 3},
		{"node pass", map[string]any{"install": "npm ci", "build": "npm run build", "test": "npm test", "lint": nil},
			map[string]string{"npm": npm}, "pass", "all_passed", "unit", "passed", "", "", 3, 0, 3},
		{"missing tool", map[string]any{"install": "pnpm install --frozen-lockfile", "test": "pnpm test"},
			nil, "inconclusive", "environment_failure", "install", "failed", "missing_tool", "pnpm", 0, 1, 1},
		{"missing test runner", map[string]any{"install": "uv sync --frozen", "test": "uv run pytest -q"},
			map[string]string{"uv": `[ "$1" = sync ] && exit 0; echo 'error: Failed to spawn: ` + "`pytest`" + `' >&2; exit 2`},
			"inconclusive", "environment_failure", "unit", "failed", "missing_tool", "pytest", 0, 1, 1},
		{"lint warning", map[string]any{"test": "npm test", "lint": "npm run lint-fails"},
			map[string]string{"npm": `[ "$1" = test ] && { echo "Tests:       2 passed, 2 total"; exit 0; }; echo "1 problem"; exit 1`},
			"pass_with_warnings", "non_blocking_findings", "lint", "warning", "lint_failure", "", 2, 0, 2},
	} {
		t.Run(c.name, func(t *testing.T) {
			r := runWithCommands(t, c.commands, c.stubs)
			if r.Verdict != c.verdict || r.RR["reason_code"] != c.reason {
				t.Fatalf("verdict %s/%v, want %s/%s: %v", r.Verdict, r.RR["reason_code"], c.verdict, c.reason, r.RR)
			}
			st := stageOf(r, c.stage)
			if st == nil || st["status"] != c.status || (c.cs != "" && st["cause"] != c.cs) || (c.detail != "" && st["detail"] != c.detail) {
				t.Fatalf("stage %s: %v", c.stage, st)
			}
			counts := r.RR["counts"].(map[string]any)
			if int(counts["passed"].(float64)) != c.passed || int(counts["failed"].(float64)) != c.failed || int(counts["total"].(float64)) != c.nT {
				t.Fatalf("counts %v", counts)
			}
		})
	}
}

func TestCommandsSkipLaterStagesAndUnconfigured(t *testing.T) {
	r := runWithCommands(t, map[string]any{"install": "false", "build": "true", "test": "true"}, nil)
	if st := stageOf(r, "build"); st["status"] != "not_run" || st["command"] != "true" {
		t.Fatalf("build after failed install: %v", st)
	}
	if stageOf(r, "lint") != nil {
		t.Fatal("an unconfigured stage must not be listed")
	}
	if st := stageOf(r, "install"); st["cause"] != "install_failure" {
		t.Fatalf("install: %v", st)
	}
	// no commands and no go.mod: QA stays disabled, as before
	if r := runWithCommands(t, nil, nil); r.Verdict != "not_applicable" || r.RR["reason_code"] != "qa_disabled" {
		t.Fatalf("%v", r)
	}
}
