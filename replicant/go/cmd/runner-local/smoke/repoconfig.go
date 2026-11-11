package main

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"time"

	"example.com/replicant/internal/runner/s3x"
)

// repoConfigFiles is the Claude Code configuration RC-REPO-CFG commits on top of the sample repo (design §8c).
var repoConfigFiles = map[string]string{
	// the hook leaves a file in the diff; the allow rule must lose to the managed deny of Bash(curl:*)
	".claude/settings.json": `{"hooks":{"PostToolUse":[{"matcher":"Edit|Write","hooks":[{"type":"command","command":"echo ran >> \"$CLAUDE_PROJECT_DIR/hook-marker.txt\""}]}]},` +
		`"permissions":{"allow":["Bash(curl:*)"]}}` + "\n",
	".claude/rules/ledger-style.md": "Amounts are integer minor units; never use floats.\n",
	"internal/ledger/CLAUDE.md":     "Ledger entries are append-only.\n",
	".mcp.json": `{"mcpServers":{"schema":{"command":"npx","args":["-y","@acme/schema-mcp@1.2.0"]},` +
		`"jira":{"type":"http","url":"https://jira.acme.internal/mcp"}}}` + "\n",
}

// caseRepoConfig is the §8c runner-contract case: with repo_config use and config_base_sha, the repo's PostToolUse hook
// runs, .claude/rules and a nested CLAUDE.md load natively, the .mcp.json servers are skipped (the URL one by
// identity), and a managed deny still wins over a project allow rule.
func caseRepoConfig(ctx context.Context, e *env) (string, error) {
	sha, err := e.gitea.commitBranch("repo-config", "add Claude Code configuration", repoConfigFiles)
	if err != nil {
		return "", err
	}
	st, err := e.stage(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "repo_config", baseSHA: sha, repoConfig: "use", configBaseSHA: sha})
	if err != nil {
		return "", err
	}
	if c, out := e.create(st); c != 201 {
		return "", fmt.Errorf("create %d %v", c, out)
	}
	final, _, err := e.wait(st, 10*time.Minute)
	if err != nil {
		return "", err
	}
	if final["phase"] != "Succeeded" {
		e.dumpRun(ctx, st)
		return "", fmt.Errorf("run %v %v", final["phase"], final["failure"])
	}
	ab, ak, _ := s3x.ParseURI(st.prefixes.Artifacts)
	rb, _, err := e.s3.Get(ctx, ab, ak+"run_result/result.json")
	if err != nil {
		return "", err
	}
	var rr struct {
		Outputs    map[string]string `json:"outputs"`
		RoleResult struct {
			CLI struct {
				PermissionDenials int      `json:"permission_denials"`
				Instructions      []string `json:"instruction_files_loaded"`
				RepoConfig        *struct {
					Mode  string `json:"mode"`
					Hooks []struct {
						Event    string `json:"event"`
						Runs     int    `json:"runs"`
						Failures int    `json:"failures"`
					} `json:"hooks"`
					Rules      []string            `json:"rules"`
					ClaudeMD   []string            `json:"claude_md"`
					MCPSkipped []map[string]string `json:"mcp_skipped"`
					Available  bool                `json:"hook_events_available"`
				} `json:"repo_config"`
			} `json:"cli"`
		} `json:"role_result"`
	}
	if err := json.Unmarshal(rb, &rr); err != nil {
		return "", err
	}
	cli := rr.RoleResult.CLI
	rcfg := cli.RepoConfig
	if rcfg == nil || rcfg.Mode != "use" || !rcfg.Available {
		return "", fmt.Errorf("no cli.repo_config section: %s", rb)
	}
	db, _, _ := e.s3.Get(ctx, ab, rr.Outputs["diff_bundle"])
	patch := patchOf(db)
	hookRan := len(rcfg.Hooks) == 1 && rcfg.Hooks[0].Runs >= 1 && rcfg.Hooks[0].Failures == 0 && strings.Contains(patch, "hook-marker.txt")
	e.record("RC-REPO-CFG-01", "repo PostToolUse hook ran (stream-json hook events + marker file in the diff)", hookRan, fmt.Sprintf("hooks %+v", rcfg.Hooks))
	loaded := slices.Contains(cli.Instructions, "/workspace/repo/.claude/rules/ledger-style.md") &&
		slices.Contains(cli.Instructions, "/workspace/repo/internal/ledger/CLAUDE.md") &&
		slices.Contains(rcfg.Rules, "ledger-style.md") && slices.Contains(rcfg.ClaudeMD, "internal/ledger/CLAUDE.md")
	e.record("RC-REPO-CFG-02", ".claude/rules and nested CLAUDE.md loaded (InstructionsLoaded)", loaded, fmt.Sprintf("loaded %v", cli.Instructions))
	urlSkipped := false
	for _, m := range rcfg.MCPSkipped {
		urlSkipped = urlSkipped || (m["name"] == "jira" && m["identity"] == "url:https://jira.acme.internal" && m["reason"] == "not_allowed")
	}
	e.record("RC-REPO-CFG-03", ".mcp.json URL server skipped", urlSkipped && len(rcfg.MCPSkipped) == 2, fmt.Sprintf("mcp_skipped %v", rcfg.MCPSkipped))
	e.record("RC-REPO-CFG-04", "managed deny Bash(curl:*) wins over the project allow", cli.PermissionDenials >= 1, fmt.Sprintf("permission_denials %d", cli.PermissionDenials))
	clean := !strings.Contains(patch, ".claude/") && !strings.Contains(patch, ".mcp.json") && !strings.Contains(patch, "CLAUDE.md")
	e.record("RC-REPO-CFG-05", "configuration paths not in the diff", clean, "")
	if !hookRan || !loaded || !urlSkipped || cli.PermissionDenials < 1 || !clean {
		return "", fmt.Errorf("repo_config assertions failed: %s", rb)
	}
	return fmt.Sprintf("hook runs %d, %d instruction files, %d mcp skipped, %d denials", rcfg.Hooks[0].Runs, len(cli.Instructions),
		len(rcfg.MCPSkipped), cli.PermissionDenials), nil
}
