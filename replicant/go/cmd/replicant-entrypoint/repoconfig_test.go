package main

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"example.com/replicant/internal/runner/sandbox"
	"example.com/replicant/internal/runner/schemas"
)

const cliExecutionSchema = "https://schemas.replicant.example.com/kinds/common/v1.json#/$defs/cli_execution"

func TestRepoConfigUseVsIgnore(t *testing.T) {
	for _, mode := range []string{"", "ignore", "use"} {
		t.Run("mode="+mode, func(t *testing.T) {
			use := mode == "use"
			p := plan(t)
			p.RepoConfig = mode
			p.MCPAutoAllow = []sandbox.MCPAutoAllow{{Name: "schema", Command: "npx", Args: []string{"-y", "@acme/schema-mcp@1.2.0"}}}
			argv := strings.Join(cliArgv(p, "run_gj3h3ep3phjfs9e23v768zrca5", "{}", ""), " ")
			if strings.Contains(argv, "--setting-sources user,project --include-hook-events") != use ||
				strings.Contains(argv, "--setting-sources user ") == use {
				t.Errorf("argv: %s", argv)
			}
			if slices.Contains(cliEnv(p, ""), "CLAUDE_CODE_DISABLE_CLAUDE_MDS=1") == use {
				t.Error("CLAUDE_CODE_DISABLE_CLAUDE_MDS")
			}
			ms := managedSettings(p, nil)
			if ms["allowManagedHooksOnly"] != !use || ms["allowManagedPermissionRulesOnly"] != true || ms["allowManagedMcpServersOnly"] != true {
				t.Errorf("managed-only switches: %v %v %v", ms["allowManagedHooksOnly"], ms["allowManagedPermissionRulesOnly"], ms["allowManagedMcpServersOnly"])
			}
			if !slices.Equal(ms["disabledMcpjsonServers"].([]string), []string{"*"}) {
				t.Error("disabledMcpjsonServers must stay [*]")
			}
			if slices.Contains(ms["claudeMdExcludes"].([]string), "/workspace/**") == use || !slices.Contains(ms["claudeMdExcludes"].([]string), "/home/agent/**") {
				t.Errorf("claudeMdExcludes %v", ms["claudeMdExcludes"])
			}
			hooks, _ := json.Marshal(ms["hooks"])
			for _, h := range []string{"instructions-loaded", "guard-paths", "session-end"} {
				if !strings.Contains(string(hooks), "replicant-entrypoint hook "+h) {
					t.Errorf("managed hook %s missing", h)
				}
			}
			allowed, _ := json.Marshal(ms["allowedMcpServers"])
			wantAllowed := `[{"serverUrl":"http://127.0.0.1:7801/*"}]`
			if use {
				wantAllowed = `[{"serverUrl":"http://127.0.0.1:7801/*"},{"serverCommand":["npx","-y","@acme/schema-mcp@1.2.0"]}]`
			}
			if string(allowed) != wantAllowed {
				t.Errorf("allowedMcpServers %s", allowed)
			}
			perm := ms["permissions"].(map[string]any)
			if slices.Contains(perm["allow"].([]string), "mcp__schema__*") != use {
				t.Error("auto-allowed server tools")
			}
			env := ms["env"].(map[string]string)
			if (env["ANTHROPIC_BASE_URL"] == gatewayBaseURL) != use || (env["MAX_THINKING_TOKENS"] == "8000") != use {
				t.Errorf("managed env pins the gateway and models only in use mode: %v", env)
			}
			dir := t.TempDir()
			if err := renderManaged(dir, p, &sandbox.Bootstrap{}); err != nil {
				t.Fatal(err)
			}
			b, _ := os.ReadFile(filepath.Join(dir, "claude-code", "managed-mcp.json"))
			if strings.Contains(string(b), `"command": "npx"`) != use {
				t.Errorf("managed-mcp.json: %s", b)
			}
		})
	}
}

func TestAutoAllowedMCPFiltering(t *testing.T) {
	p := plan(t)
	p.RepoConfig = "use"
	p.MCPAutoAllow = []sandbox.MCPAutoAllow{{Name: "memory", Command: "x"}, {Name: "bad name", Command: "x"}, {Name: "fmt", Command: ""},
		{Name: "ok", Command: "uvx", Args: []string{"fmt-mcp==1.0"}}, {Name: "ok", Command: "other"}}
	got := autoAllowedMCP(p, []mcpEntry{{name: "memory"}})
	if len(got) != 1 || got[0].Name != "ok" || got[0].Command != "uvx" {
		t.Fatalf("%+v", got)
	}
	p.RepoConfig = "ignore"
	if autoAllowedMCP(p, nil) != nil {
		t.Fatal("ignore mode must not allow repo servers")
	}
}

func TestEffortFlag(t *testing.T) {
	for effort, want := range map[string]string{"": "", "high": "--effort high", "xhigh": "--effort xhigh", "minimal": ""} {
		p := plan(t)
		p.Agent.Model.Parameters.ReasoningEffort = effort
		s := strings.Join(cliArgv(p, "run_gj3h3ep3phjfs9e23v768zrca5", "", ""), " ")
		if want == "" && strings.Contains(s, "--effort") || want != "" && !strings.Contains(s, want) {
			t.Errorf("effort %q: %s", effort, s)
		}
	}
}

func gitT(t *testing.T, dir string, args ...string) string {
	t.Helper()
	c := exec.Command("git", append([]string{"-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"}, args...)...)
	c.Dir = dir
	out, err := c.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v %s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

func writeFiles(t *testing.T, dir string, files map[string]string) {
	t.Helper()
	for p, s := range files {
		full := filepath.Join(dir, p)
		_ = os.MkdirAll(filepath.Dir(full), 0o755)
		if s == "" {
			_ = os.Remove(full)
			continue
		}
		if err := os.WriteFile(full, []byte(s), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func TestRestoreBaseConfig(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not on PATH")
	}
	origin := t.TempDir()
	gitT(t, origin, "init", "-q")
	gitT(t, origin, "config", "uploadpack.allowAnySHA1InWant", "true")
	gitT(t, origin, "config", "uploadpack.allowFilter", "true")
	writeFiles(t, origin, map[string]string{"CLAUDE.md": "base root\n", "src/CLAUDE.md": "base nested\n", ".claude/settings.json": `{"hooks":{}}`,
		".claude/rules/style.md": "tabs\n", ".mcp.json": `{"mcpServers":{}}`, "main.go": "package main\n"})
	gitT(t, origin, "add", "-A")
	gitT(t, origin, "commit", "-qm", "base")
	base := gitT(t, origin, "rev-parse", "HEAD")
	// a PR branch with human commits: config edited, added and deleted, plus a normal change
	writeFiles(t, origin, map[string]string{"CLAUDE.md": "branch root\n", "lib/CLAUDE.md": "branch new nested\n",
		".claude/settings.json": `{"hooks":{"PostToolUse":[]}}`, ".claude/agents/evil.md": "x\n", ".claude/rules/style.md": "",
		"AGENTS.md": "branch agents\n", "main.go": "package main // changed\n"})
	gitT(t, origin, "add", "-A")
	gitT(t, origin, "commit", "-qm", "branch")
	head := gitT(t, origin, "rev-parse", "HEAD")

	// the sandbox checkout: only the branch head, shallow, so the base commit must be fetched
	repo := t.TempDir()
	gitT(t, repo, "init", "-q")
	gitT(t, repo, "remote", "add", "origin", "file://"+origin)
	gitT(t, repo, "fetch", "-q", "--no-tags", "--depth", "1", "origin", head)
	gitT(t, repo, "checkout", "-q", "--detach", head)
	if err := restoreBaseConfig(context.Background(), repo, base); err != nil {
		t.Fatal(err)
	}
	for p, want := range map[string]string{"CLAUDE.md": "base root\n", "src/CLAUDE.md": "base nested\n", ".claude/settings.json": `{"hooks":{}}`,
		".claude/rules/style.md": "tabs\n", ".mcp.json": `{"mcpServers":{}}`, "main.go": "package main // changed\n"} {
		b, err := os.ReadFile(filepath.Join(repo, p))
		if err != nil || string(b) != want {
			t.Errorf("%s = %q (%v), want %q", p, b, err, want)
		}
	}
	for _, p := range []string{"lib/CLAUDE.md", ".claude/agents/evil.md", "AGENTS.md"} {
		if _, err := os.Stat(filepath.Join(repo, p)); err == nil {
			t.Errorf("%s exists only on the branch and must be removed", p)
		}
	}
	for _, p := range []string{"CLAUDE.md", ".claude/settings.json", ".claude/rules/style.md"} {
		if fi, _ := os.Stat(filepath.Join(repo, p)); fi == nil || fi.Mode().Perm()&0o222 != 0 {
			t.Errorf("%s must be read-only", p)
		}
	}
	if fi, _ := os.Stat(filepath.Join(repo, "main.go")); fi.Mode().Perm()&0o200 == 0 {
		t.Error("non-config files stay writable")
	}
}

func TestHookMatches(t *testing.T) {
	for _, c := range []struct {
		m, q string
		want bool
	}{{"", "Write", true}, {"*", "Bash", true}, {"Edit|Write", "Write", true}, {"Edit|Write", "MultiEdit", false},
		{"Notebook.*", "NotebookEdit", true}, {"mcp__.*__write", "mcp__fs__write", true}, {"Bash", "", true}, {"(", "x", false}} {
		if hookMatches(c.m, c.q) != c.want {
			t.Errorf("%q ~ %q", c.m, c.q)
		}
	}
}

// hookEvents replays stream-json hook events shaped like the pinned CLI's (2.1.310 with --include-hook-events).
func hookEvents(tr *hookTracker, lines ...string) {
	for _, l := range lines {
		var ev map[string]any
		_ = json.Unmarshal([]byte(l), &ev)
		typ, _ := ev["type"].(string)
		sub, _ := ev["subtype"].(string)
		tr.observe(typ, sub, ev)
	}
}

func started(id, name string) string {
	return `{"type":"system","subtype":"hook_started","hook_id":"` + id + `","hook_name":"` + name + `","hook_event":"` + strings.Split(name, ":")[0] + `"}`
}

func response(id, name, stdout, stderr string, code int, outcome string) string {
	b, _ := json.Marshal(map[string]any{"type": "system", "subtype": "hook_response", "hook_id": id, "hook_name": name,
		"hook_event": strings.Split(name, ":")[0], "stdout": stdout, "stderr": stderr, "exit_code": code, "outcome": outcome})
	return string(b)
}

func TestHookTrackerAttribution(t *testing.T) {
	hooks := []repoHook{
		{"PostToolUse", "Write|Edit", "touch /tmp/marker"},
		{"PostToolUse", "Write|Edit", "uv run ruff format"},
		{"PostToolUse", "Write|Edit", "curl -sS https://x.invalid"},
		{"PreToolUse", "Write", "check-paths"},
		{"Stop", "", "sleep 30"},
	}
	tr := newHookTracker()
	assistant := `{"type":"assistant","message":{"content":[]}}`
	hookEvents(tr,
		assistant,
		// managed guard-paths burst (marker) then the repo's PreToolUse burst: same hook_name, same size
		started("m1", "PreToolUse:Write"), response("m1", "PreToolUse:Write", managedHookMarker+"\n", "", 0, "success"),
		started("p1", "PreToolUse:Write"), response("p1", "PreToolUse:Write", "", "nope\n", 2, "error"),
		// repo PostToolUse burst (config order), then the managed one
		started("a", "PostToolUse:Write"), started("b", "PostToolUse:Write"), started("c", "PostToolUse:Write"),
		response("b", "PostToolUse:Write", "", "/bin/sh: 1: uv: not found\n", 127, "error"),
		response("a", "PostToolUse:Write", "", "", 0, "success"),
		response("c", "PostToolUse:Write", "", "curl: (6) Could not resolve host: x.invalid\n", 6, "error"),
		started("m2", "PostToolUse:Write"), response("m2", "PostToolUse:Write", managedHookMarker, "", 0, "success"),
		`{"type":"user","message":{"content":[]}}`,
		assistant,
		// a second edit: the same three hooks, all fine
		started("a2", "PostToolUse:Edit"), started("b2", "PostToolUse:Edit"), started("c2", "PostToolUse:Edit"),
		response("a2", "PostToolUse:Edit", "", "", 0, "success"), response("b2", "PostToolUse:Edit", "", "", 0, "success"),
		response("c2", "PostToolUse:Edit", "", "", 0, "success"),
		// a Stop hook that timed out, and a burst that cannot be attributed (2 runs for 3 candidates)
		started("s", "Stop"), response("s", "Stop", "", "", 1, "cancelled"),
		assistant,
		started("x1", "PostToolUse:Write"), started("x2", "PostToolUse:Write"),
		response("x1", "PostToolUse:Write", "", "", 0, "success"), response("x2", "PostToolUse:Write", "", "", 1, "error"),
	)
	stats := tr.hookStats(hooks)
	type want struct {
		runs, failures, blocks int
		cause, detail          string
	}
	for i, w := range []want{{2, 0, 0, "", ""}, {2, 1, 0, "missing_tool", "uv"}, {2, 1, 0, "network", "x.invalid"}, {1, 0, 1, "", ""}, {1, 1, 0, "timeout", ""}} {
		s := stats[i]
		cause := ""
		if s.Cause != nil {
			cause = *s.Cause
		}
		if s.Runs != w.runs || s.Failures != w.failures || s.Blocks != w.blocks || cause != w.cause || s.Detail != w.detail {
			t.Errorf("hook %d (%s): %+v cause=%q, want %+v", i, hooks[i].Command, s, cause, w)
		}
		if *s.CommandHash != commandHash(hooks[i].Command) {
			t.Errorf("hook %d command hash", i)
		}
	}
	if len(stats) != 6 || stats[5].CommandHash != nil || stats[5].Event != "PostToolUse" || stats[5].Runs != 2 || stats[5].Failures != 1 ||
		*stats[5].Cause != "non_zero_exit" {
		t.Fatalf("unattributed: %+v", stats[len(stats)-1])
	}
}

func TestRepoConfigReportSection(t *testing.T) {
	repo := t.TempDir()
	writeFiles(t, repo, map[string]string{
		".claude/settings.json":              `{"hooks":{"PostToolUse":[{"matcher":"Write|Edit","hooks":[{"type":"command","command":"touch /tmp/marker"},{"type":"prompt","prompt":"x"}]}]},"permissions":{"allow":["Bash(curl:*)"],"deny":["Read(.env)"],"ask":["Bash"]}}`,
		".claude/rules/python-style.md":      "style\n",
		".claude/skills/migrations/SKILL.md": "---\nname: migrations\n---\n",
		".claude/skills/notloaded/SKILL.md":  "x\n",
		".claude/skills/README.md":           "not a skill\n",
		"src/billing/CLAUDE.md":              "nested\n",
		".mcp.json": `{"mcpServers":{"schema":{"command":"npx","args":["-y","@acme/schema-mcp@1.2.0"]},` +
			`"jira":{"type":"http","url":"https://jira.acme.internal/mcp"},"db":{"command":"npx","args":["-y","@acme/db-mcp@2.0.0"],"env":{"DB_PASSWORD":"x"}}}}`,
	})
	p := plan(t)
	p.Agent.CLIImage.Version = "2.1.310"
	p.RepoConfig, p.ConfigBaseSHA = "use", strings.Repeat("b", 40)
	p.MCPAutoAllow = []sandbox.MCPAutoAllow{{Name: "schema", Command: "npx", Args: []string{"-y", "@acme/schema-mcp@1.2.0"}}}
	rc := &runCtx{plan: p, bs: &sandbox.Bootstrap{}, hooks: newHookTracker(), repoDir: repo}
	rc.initSkills = []string{"migrations", "deep-research"}
	rc.instructions = []string{repo + "/CLAUDE.md", repo + "/.claude/rules/python-style.md", repo + "/src/billing/CLAUDE.md"}
	hookEvents(rc.hooks, started("a", "PostToolUse:Write"), response("a", "PostToolUse:Write", "", "", 0, "success"))
	c := rc.cliExecution(0)
	if err := schemas.Validate(cliExecutionSchema, c); err != nil {
		t.Fatalf("cli section does not validate: %v", err)
	}
	b, _ := json.Marshal(c["repo_config"])
	var got struct {
		Mode, ConfigBaseSHA, SettingsHash string
		Hooks                             []hookStat
		Rules, Skills, ClaudeMD           []string
		MCPSkipped                        []map[string]string
		MCPAllowed                        []string
		PermIgnored                       int
		Avail                             bool
	}
	var raw map[string]json.RawMessage
	_ = json.Unmarshal(b, &raw)
	for k, dst := range map[string]any{"mode": &got.Mode, "config_base_sha": &got.ConfigBaseSHA, "settings_hash": &got.SettingsHash,
		"hooks": &got.Hooks, "rules": &got.Rules, "skills": &got.Skills, "claude_md": &got.ClaudeMD, "mcp_skipped": &got.MCPSkipped,
		"mcp_allowed": &got.MCPAllowed, "permission_rules_ignored": &got.PermIgnored, "hook_events_available": &got.Avail} {
		if err := json.Unmarshal(raw[k], dst); err != nil {
			t.Fatalf("%s: %v (%s)", k, err, b)
		}
	}
	if got.Mode != "use" || got.ConfigBaseSHA != p.ConfigBaseSHA || !strings.HasPrefix(got.SettingsHash, "sha256:") || !got.Avail {
		t.Errorf("header: %s", b)
	}
	if len(got.Hooks) != 1 || got.Hooks[0].Runs != 1 || got.Hooks[0].Matcher != "Write|Edit" || *got.Hooks[0].CommandHash != commandHash("touch /tmp/marker") {
		t.Errorf("hooks: %+v", got.Hooks)
	}
	if !slices.Equal(got.Rules, []string{"python-style.md"}) || !slices.Equal(got.Skills, []string{"migrations"}) ||
		!slices.Equal(got.ClaudeMD, []string{"CLAUDE.md", "src/billing/CLAUDE.md"}) {
		t.Errorf("rules %v skills %v claude_md %v", got.Rules, got.Skills, got.ClaudeMD)
	}
	if len(got.MCPSkipped) != 2 || got.MCPSkipped[0]["name"] != "db" || got.MCPSkipped[0]["identity"] != "stdio:npx:@acme/db-mcp@2.0.0" ||
		got.MCPSkipped[1]["identity"] != "url:https://jira.acme.internal" || got.MCPSkipped[1]["reason"] != "not_allowed" {
		t.Errorf("mcp_skipped %v", got.MCPSkipped)
	}
	if !slices.Equal(got.MCPAllowed, []string{"schema"}) || got.PermIgnored != 3 {
		t.Errorf("mcp_allowed %v permission_rules_ignored %d", got.MCPAllowed, got.PermIgnored)
	}
	// the hash covers the project settings
	rc2 := &runCtx{plan: p, bs: &sandbox.Bootstrap{}, repoDir: repo}
	writeFiles(t, repo, map[string]string{".claude/settings.json": `{}`})
	if h2, _ := rc2.repoConfigSection()["settings_hash"].(string); h2 == got.SettingsHash {
		t.Error("settings_hash ignores the project settings")
	}
	// repo_config ignore: no section
	p.RepoConfig = "ignore"
	if _, ok := rc.cliExecution(0)["repo_config"]; ok {
		t.Error("repo_config section in ignore mode")
	}
}
