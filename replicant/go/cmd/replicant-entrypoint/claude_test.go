package main

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"example.com/replicant/internal/runner/sandbox"
)

func plan(t *testing.T) *sandbox.RunPlan {
	var p sandbox.RunPlan
	p.Role = "implementer"
	p.Agent.Tools.Read, p.Agent.Tools.Edit = true, true
	p.Agent.Tools.Shell.Mode = "any"
	p.Agent.Tools.Shell.Deny = []string{"git push:*"}
	p.Agent.Model.Primary = "anthropic/claude-sonnet-5-5"
	p.Effective.MaxTurns, p.Effective.BudgetUSD = 50, 3
	return &p
}

func TestArgvAndManagedSettings(t *testing.T) {
	p := plan(t)
	argv := cliArgv(p, "run_gj3h3ep3phjfs9e23v768zrca5", "{}", "/run/replicant/prompt/system-append.md")
	s := strings.Join(argv, " ")
	for _, want := range []string{"-p --input-format stream-json --output-format stream-json --verbose", "--permission-mode dontAsk",
		"--setting-sources user", "--max-budget-usd 3.00", "--session-id 119cf1fa-b8ea-54df-89b4-1c44020bac16", "--append-system-prompt-file"} {
		if !strings.Contains(s, want) {
			t.Errorf("argv missing %q: %s", want, s)
		}
	}
	if strings.Contains(s, "--mcp-config") || strings.Contains(s, "dangerously") {
		t.Error("forbidden flag rendered")
	}
	ms := managedSettings(p, nil)
	perm := ms["permissions"].(map[string]any)
	allow, deny := perm["allow"].([]string), perm["deny"].([]string)
	for _, a := range []string{"Read", "Edit", "Write", "Bash"} {
		if !slices.Contains(allow, a) {
			t.Errorf("allow missing %s", a)
		}
	}
	for _, d := range []string{"WebFetch", "WebSearch", "Bash(git push:*)", "Read(//run/replicant/**)"} {
		if !slices.Contains(deny, d) {
			t.Errorf("deny missing %s", d)
		}
	}
	b, _ := json.Marshal(ms)
	if !strings.Contains(string(b), `"defaultMode":"dontAsk"`) {
		t.Error("defaultMode")
	}
	p.Agent.Tools.Edit = false
	_, dis := toolLists(p)
	if !slices.Contains(dis, "Edit") {
		t.Error("edit:false must disallow Edit")
	}
}

func TestGuardPathsAndGlob(t *testing.T) {
	if !globMatch(".github/workflows/**", ".github/workflows/ci.yml") || !globMatch("**/*.pem", "a/b/key.pem") || globMatch("**/*.pem", "a/key.go") {
		t.Fatal("glob")
	}
	if guardPaths([]byte(`{"tool_input":{"file_path":"/etc/passwd"}}`)) != 2 {
		t.Error("outside /workspace allowed")
	}
	if guardPaths([]byte(`{"tool_input":{"file_path":"/workspace/repo/CLAUDE.md"}}`)) != 2 {
		t.Error("CLAUDE.md allowed")
	}
	if guardPaths([]byte(`{"tool_input":{"file_path":"/workspace/repo/.claude/settings.json"}}`)) != 2 {
		t.Error(".claude allowed")
	}
	if guardPaths([]byte(`{"tool_input":{"file_path":"/workspace/repo/internal/x.go"}}`)) != 0 {
		t.Error("normal edit blocked")
	}
}

func TestAgentReportSchemaInlined(t *testing.T) {
	s, _, err := agentReportSchema(plan(t))
	if err != nil || strings.Contains(s, "$ref") || !strings.Contains(s, "tests_run") {
		t.Fatalf("%v %s", err, s)
	}
}
