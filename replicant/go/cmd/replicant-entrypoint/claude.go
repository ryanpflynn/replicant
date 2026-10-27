package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strings"

	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/sandbox"
	"example.com/replicant/internal/runner/schemas"
)

// The CLI adapter for Claude Code (cli-invocation.md §2), package adapter/claudecode in the spec.

var alwaysDenied = []string{"WebFetch", "WebSearch", "NotebookEdit", "EnterWorktree", "Artifact", "PushNotification",
	"RemoteTrigger", "SendUserFile", "SendMessage", "Workflow", "Skill"}

var taskTools = []string{"TaskCreate", "TaskGet", "TaskList", "TaskUpdate"}

// toolLists renders --tools and --disallowedTools (§2.3 "Rendering of tools").
func toolLists(p *sandbox.RunPlan) (tools, disallowed []string) {
	t := p.Agent.Tools
	if t.Read {
		tools = append(tools, "Read")
		if t.Shell.Mode == "none" {
			tools = append(tools, "Glob", "Grep")
		}
	}
	if t.Edit {
		tools = append(tools, "Edit", "Write")
	}
	if t.Shell.Mode == "any" || t.Shell.Mode == "allowlist" {
		tools = append([]string{"Bash"}, tools...)
	}
	if t.Subagents.Enabled {
		tools = append(tools, "Agent")
	}
	tools = append(tools, taskTools...)
	disallowed = append([]string{}, alwaysDenied...)
	if !t.Edit {
		disallowed = append(disallowed, "Edit", "Write")
	}
	for _, d := range t.DenyTools {
		if !slices.Contains(disallowed, d) {
			disallowed = append(disallowed, d)
		}
	}
	return tools, disallowed
}

// managedSettings renders /etc/claude-code/managed-settings.json (§2.3).
func managedSettings(p *sandbox.RunPlan, mcp []mcpEntry) map[string]any {
	t := p.Agent.Tools
	allow := []string{}
	deny := append([]string{}, alwaysDenied...)
	if t.Read {
		allow = append(allow, "Read")
		if t.Shell.Mode == "none" {
			allow = append(allow, "Glob", "Grep")
		}
	}
	if t.Edit {
		if len(t.EditPaths) > 0 {
			for _, g := range t.EditPaths {
				allow = append(allow, "Edit(//"+strings.TrimPrefix(g, "/")+")", "Write(//"+strings.TrimPrefix(g, "/")+")")
			}
		} else {
			allow = append(allow, "Edit", "Write")
		}
	} else {
		deny = append(deny, "Edit", "Write", "NotebookEdit")
	}
	switch t.Shell.Mode {
	case "any":
		allow = append(allow, "Bash")
		for _, d := range t.Shell.Deny {
			deny = append(deny, "Bash("+d+")")
		}
	case "allowlist":
		for _, a := range t.Shell.Allow {
			allow = append(allow, "Bash("+a+")")
		}
	default:
		deny = append(deny, "Bash")
	}
	if t.Subagents.Enabled {
		allow = append(allow, "Agent")
	} else {
		deny = append(deny, "Agent")
	}
	allow = append(allow, taskTools...)
	for _, m := range mcp {
		if len(m.allow) == 0 {
			allow = append(allow, "mcp__"+m.name+"__*")
		}
		for _, a := range m.allow {
			allow = append(allow, "mcp__"+m.name+"__"+a)
		}
		for _, d := range m.deny {
			deny = append(deny, "mcp__"+m.name+"__"+d)
		}
	}
	for _, d := range t.DenyTools {
		if !slices.Contains(deny, d) {
			deny = append(deny, d)
		}
	}
	deny = append(deny, "Read(//run/replicant/**)", "Read(//proc/*/environ)", "Edit(//etc/**)", "Edit(//opt/replicant/**)", "Edit(//replicant/inputs/**)")
	hook := func(name string) []map[string]any {
		return []map[string]any{{"type": "command", "command": "/opt/replicant/bin/replicant-entrypoint hook " + name, "timeout": 10}}
	}
	return map[string]any{
		"permissions": map[string]any{
			"defaultMode": "dontAsk", "disableBypassPermissionsMode": "disable", "disableAutoMode": "disable",
			"allow": allow, "deny": deny,
		},
		"allowManagedHooksOnly":           true,
		"allowManagedPermissionRulesOnly": true,
		"allowManagedMcpServersOnly":      true,
		"allowedMcpServers":               []map[string]string{{"serverUrl": "http://127.0.0.1:7801/*"}},
		"hooks": map[string]any{
			"InstructionsLoaded": []map[string]any{{"hooks": hook("instructions-loaded")}},
			"PreToolUse":         []map[string]any{{"matcher": "Edit|Write|NotebookEdit", "hooks": hook("guard-paths")}},
			"SessionEnd":         []map[string]any{{"hooks": hook("session-end")}},
		},
		"pluginConfigs":          map[string]any{"cc-plugin-agents-md@builtin": map[string]any{"options": map[string]any{"instructionFiles": "managed-only"}}},
		"claudeMdExcludes":       []string{"/workspace/**", "/home/agent/**"},
		"disabledMcpjsonServers": []string{"*"},
		"autoMemoryEnabled":      false,
		"cleanupPeriodDays":      30,
		"env": map[string]string{"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_WEB_FETCH": "1",
			"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
	}
}

type mcpEntry struct {
	name, url   string
	allow, deny []string
	required    bool
}

// mcpServers lists the plan's MCP servers whose upstream exists (bootstrap endpoint set) (§2.5, §5.1 item 1a).
func mcpServers(p *sandbox.RunPlan, bs *sandbox.Bootstrap) []mcpEntry {
	var out []mcpEntry
	for _, s := range p.Agent.MCPServers {
		ep := ""
		switch s.Service {
		case "memory":
			ep = bs.Endpoints.MemoryMCP
		case "context":
			ep = bs.Endpoints.ContextMCP
		case "code_search":
			ep = bs.Endpoints.CodeSearchMCP
		}
		if ep == "" {
			continue
		}
		out = append(out, mcpEntry{name: s.Name, url: "http://127.0.0.1:7801/mcp/" + s.Service, allow: s.Tools.Allow, deny: s.Tools.Deny, required: s.Required})
	}
	return out
}

func renderManaged(dir string, p *sandbox.RunPlan, bs *sandbox.Bootstrap) error {
	cc := filepath.Join(dir, "claude-code")
	fd := filepath.Join(dir, "replicant")
	for _, d := range []string{cc, fd} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return err
		}
	}
	mcp := mcpServers(p, bs)
	servers := map[string]any{}
	for _, m := range mcp {
		servers[m.name] = map[string]string{"type": "http", "url": m.url}
	}
	if err := writeJSONFile(filepath.Join(cc, "managed-settings.json"), managedSettings(p, mcp), 0o444); err != nil {
		return err
	}
	if err := writeJSONFile(filepath.Join(cc, "managed-mcp.json"), map[string]any{"mcpServers": servers}, 0o444); err != nil {
		return err
	}
	b, err := os.ReadFile(sandbox.BootstrapPath)
	if err == nil {
		_ = os.WriteFile(filepath.Join(fd, "bootstrap.json"), b, 0o444)
	}
	return nil
}

func writeJSONFile(p string, v any, mode os.FileMode) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	_ = os.Remove(p)
	return os.WriteFile(p, b, mode)
}

var protectedEnv = regexp.MustCompile(`^(REPLICANT_.*|TRACEPARENT|TRACESTATE|OTEL_.*|ANTHROPIC_.*|CLAUDE_.*|OPENAI_.*|CODEX_.*|AWS_.*|GOOGLE_.*|CLOUD_ML_REGION|HOME|USER|PATH|SHELL|HTTP_PROXY|HTTPS_PROXY|NO_PROXY|http_proxy|https_proxy|no_proxy|NODE_EXTRA_CA_CERTS|SSL_CERT_FILE|SSL_CERT_DIR|GIT_.*|LD_PRELOAD|LD_LIBRARY_PATH|DISABLE_.*|IS_SANDBOX|MCP_.*|MAX_MCP_OUTPUT_TOKENS|API_TIMEOUT_MS|BASH_.*)$`)

// cliEnv is §2.2: inherited controller/runner variables (minus agent-invisible ones) + the adapter's set +
// definition env through the protected-name filter.
func cliEnv(p *sandbox.RunPlan, traceparent string) []string {
	env := map[string]string{}
	for _, kv := range os.Environ() {
		k, v, _ := strings.Cut(kv, "=")
		if k == "REPLICANT_RUN_TOKEN_DIR" || k == "REPLICANT_RUN_TOKEN_SEED_DIR" || k == "REPLICANT_RUN_TOKEN_FILE" || k == "REPLICANT_UPLOADER_SOCKET" {
			continue
		}
		env[k] = v
	}
	m := p.Agent.Model
	small := m.Small
	if small == "" {
		small = m.Primary
	}
	sub := m.Subagent
	if sub == "" {
		sub = m.Primary
	}
	maxOut := "32000"
	if m.Parameters.MaxOutputTokens > 0 {
		maxOut = fmt.Sprint(m.Parameters.MaxOutputTokens)
	}
	think := "8000"
	if m.Parameters.ThinkingTokens > 0 {
		think = fmt.Sprint(m.Parameters.ThinkingTokens)
	}
	timeout := "600000"
	if p.Agent.Gateway.RequestTimeoutSeconds > 0 {
		timeout = fmt.Sprint(p.Agent.Gateway.RequestTimeoutSeconds * 1000)
	}
	bashDef, bashMax, bashOut := "300000", "1800000", "30000"
	if s := p.Agent.Tools.Shell; s.DefaultTimeoutSeconds > 0 {
		bashDef = fmt.Sprint(s.DefaultTimeoutSeconds * 1000)
	}
	if s := p.Agent.Tools.Shell; s.MaxTimeoutSeconds > 0 {
		bashMax = fmt.Sprint(s.MaxTimeoutSeconds * 1000)
	}
	if s := p.Agent.Tools.Shell; s.MaxOutputChars > 0 {
		bashOut = fmt.Sprint(s.MaxOutputChars)
	}
	set := map[string]string{
		"HOME": sandbox.HomeDir, "CLAUDE_CONFIG_DIR": sandbox.HomeDir + "/.claude", "CLAUDE_CODE_PROJECT_DIR_NAME": "replicant-workspace",
		"CLAUDE_CODE_SHELL": "/bin/bash", "ANTHROPIC_BASE_URL": "http://127.0.0.1:7800/anthropic",
		"ANTHROPIC_AUTH_TOKEN": "replicant-loopback-placeholder", "ANTHROPIC_DEFAULT_HAIKU_MODEL": small,
		"CLAUDE_CODE_SUBAGENT_MODEL": sub, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": maxOut, "MAX_THINKING_TOKENS": think,
		"API_TIMEOUT_MS": timeout, "CLAUDE_CODE_MAX_RETRIES": "4", "CLAUDE_CODE_RETRY_WATCHDOG": "0",
		"CLAUDE_CODE_PROPAGATE_TRACEPARENT": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
		"DISABLE_UPDATES": "1", "DISABLE_ERROR_REPORTING": "1", "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
		"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_WEB_FETCH": "1",
		"CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "60000",
		"BASH_DEFAULT_TIMEOUT_MS": bashDef, "BASH_MAX_TIMEOUT_MS": bashMax, "BASH_MAX_OUTPUT_LENGTH": bashOut,
		"MCP_TIMEOUT": "30000", "MCP_TOOL_TIMEOUT": "60000", "MAX_MCP_OUTPUT_TOKENS": "25000",
		"OTEL_LOGS_EXPORTER": "none", "OTEL_LOG_USER_PROMPTS": "0", "OTEL_LOG_TOOL_DETAILS": "0",
		"OTEL_METRICS_INCLUDE_SESSION_ID": "false", "OTEL_METRICS_INCLUDE_ACCOUNT_UUID": "false",
		"TRACEPARENT": traceparent,
	}
	// Telemetry only when an OTLP endpoint exists (M1 has no observability profile).
	if env["OTEL_EXPORTER_OTLP_ENDPOINT"] != "" {
		for k, v := range map[string]string{"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
			"OTEL_TRACES_EXPORTER": "otlp", "OTEL_METRICS_EXPORTER": "otlp", "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
			"OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative", "OTEL_METRIC_EXPORT_INTERVAL": "30000"} {
			set[k] = v
		}
	}
	for k, v := range set {
		env[k] = v
	}
	if p.Effective.Deterministic && p.Effective.GitIdentityDate != "" {
		env["GIT_AUTHOR_DATE"], env["GIT_COMMITTER_DATE"], env["TZ"] = p.Effective.GitIdentityDate, p.Effective.GitIdentityDate, "UTC"
	}
	for _, src := range []map[string]string{p.Agent.Env, p.Environment.Env} {
		for k, v := range src {
			if protectedEnv.MatchString(k) {
				log.Warn("protected env name ignored", "name", k)
				continue
			}
			env[k] = v
		}
	}
	for _, c := range p.Environment.Caches {
		if c.Env != "" && !protectedEnv.MatchString(c.Env) {
			env[c.Env] = c.Path
			_ = os.MkdirAll(c.Path, 0o755)
		}
	}
	np := env["NO_PROXY"]
	if !strings.Contains(np, "127.0.0.1") {
		env["NO_PROXY"] = strings.Trim(np+",127.0.0.1,localhost", ",")
	}
	out := make([]string, 0, len(env))
	for k, v := range env {
		out = append(out, k+"="+v)
	}
	sort.Strings(out)
	return out
}

// cliArgv renders §2.1 (exec form, never a shell).
func cliArgv(p *sandbox.RunPlan, runID, schemaJSON, promptFile string) []string {
	bin := "/opt/replicant/cli/claude-code/" + cliVersion() + "/claude"
	maxTurns := p.Effective.MaxTurns
	if maxTurns <= 0 {
		maxTurns = p.Agent.Limits.MaxTurns
	}
	argv := []string{bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
		"--model", p.Agent.Model.Primary}
	if len(p.Agent.Model.Fallbacks) > 0 {
		argv = append(argv, "--fallback-model", strings.Join(p.Agent.Model.Fallbacks, ","))
	}
	if maxTurns > 0 {
		argv = append(argv, "--max-turns", fmt.Sprint(maxTurns))
	}
	if p.Effective.BudgetUSD > 0 {
		argv = append(argv, "--max-budget-usd", fmt.Sprintf("%.2f", p.Effective.BudgetUSD))
	}
	argv = append(argv, "--permission-mode", "dontAsk", "--setting-sources", "user")
	if promptFile != "" {
		if (p.Role == "planner" || p.Role == "reviewer") && promptMode(p) == "replace" {
			argv = append(argv, "--system-prompt-file", promptFile)
		} else {
			argv = append(argv, "--append-system-prompt-file", promptFile)
		}
	}
	tools, dis := toolLists(p)
	argv = append(argv, "--tools", strings.Join(tools, ","), "--disallowedTools", strings.Join(dis, ","))
	if schemaJSON != "" {
		argv = append(argv, "--json-schema", schemaJSON)
	}
	if !resumable(p) {
		argv = append(argv, "--no-session-persistence")
	}
	argv = append(argv, "--session-id", ids.SessionID(runID))
	return argv
}

func promptMode(p *sandbox.RunPlan) string {
	if p.Agent.Prompts.Role.Mode == "replace" {
		return "replace"
	}
	return "append"
}

// resumable: planner, reviewer and QA agent tasks pass --no-session-persistence (§2.1).
func resumable(p *sandbox.RunPlan) bool {
	switch p.Role {
	case "planner", "reviewer", "qa":
		return false
	}
	return true
}

// agentReportSchema returns the role schema's $defs/agent_report with $refs inlined (§2.1 --json-schema).
func agentReportSchema(p *sandbox.RunPlan) (string, string, error) {
	id := p.Agent.OutputContract.RoleResultSchema
	if id == "" {
		id = "replicant.role-result." + p.Role + "/v1"
	}
	loc := schemas.RoleSchemaFor(id)
	if loc == "" {
		return "", "", fmt.Errorf("unknown role result schema %s", id)
	}
	s, err := schemas.Inlined(loc + "#/$defs/agent_report")
	if err != nil {
		return "", "", err
	}
	b, err := json.Marshal(s)
	return string(b), loc, err
}

// renderHome is §4.4 step 6: delete pre-existing CLI configuration, then write the user layer.
func renderHome(p *sandbox.RunPlan, schemaJSON string) error {
	h := sandbox.HomeDir
	for _, rm := range []string{".claude/settings.json", ".claude/CLAUDE.md", ".claude/agents", ".claude/commands", ".claude/skills",
		".claude/plugins", ".claude/rules", ".codex/config.toml", ".agents"} {
		_ = os.RemoveAll(filepath.Join(h, rm))
	}
	_ = os.MkdirAll(filepath.Join(h, ".claude"), 0o755)
	_ = os.MkdirAll(filepath.Join(h, ".replicant"), 0o755)
	if err := writeJSONFile(filepath.Join(h, ".claude/settings.json"), map[string]any{"cleanupPeriodDays": 30, "autoMemoryEnabled": false}, 0o644); err != nil {
		return err
	}
	if schemaJSON != "" {
		p := filepath.Join(h, ".replicant/agent-report.schema.json")
		_ = os.Remove(p)
		if err := os.WriteFile(p, []byte(schemaJSON), 0o444); err != nil {
			return err
		}
	}
	gitcfg := "[user]\n\tname = replicant-agent\n\temail = replicant-agent@replicant.invalid\n[safe]\n\tdirectory = /workspace/repo\n" +
		"[core]\n\thooksPath = /dev/null\n[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n[credential]\n\thelper =\n"
	return os.WriteFile(filepath.Join(h, ".gitconfig"), []byte(gitcfg), 0o644)
}

// joinPrompt writes /run/replicant/prompt/system-append.md (§1.4): system-append.md + "\n\n" + memory .rendered.
func joinPrompt(m *sandbox.InputsManifest) (string, error) {
	sys, err := os.ReadFile(filepath.Join(sandbox.InputsDir, "system-append.md"))
	if err != nil {
		return "", nil // no system append staged
	}
	for _, o := range m.Objects {
		if o.Place == "system_append" && sha(sys) != o.SHA256 {
			return "", fmt.Errorf("system-append.md digest changed")
		}
	}
	out := sys
	if mb, err := os.ReadFile(filepath.Join(sandbox.InputsDir, "memory-injection.json")); err == nil {
		for _, o := range m.Objects {
			if o.Place == "system_append_memory" && sha(mb) != o.SHA256 {
				return "", fmt.Errorf("memory-injection.json digest changed")
			}
		}
		var mi struct {
			Rendered string `json:"rendered"`
		}
		if json.Unmarshal(mb, &mi) == nil && strings.TrimSpace(mi.Rendered) != "" {
			out = append(append(out, []byte("\n\n")...), []byte(mi.Rendered)...)
		}
	}
	_ = os.MkdirAll(sandbox.PromptDir, 0o755)
	p := filepath.Join(sandbox.PromptDir, "system-append.md")
	_ = os.Remove(p)
	if err := os.WriteFile(p, out, 0o444); err != nil {
		return "", err
	}
	return p, nil
}
