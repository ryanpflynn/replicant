package main

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/klauspost/compress/zstd"

	"example.com/replicant/internal/runner/ipc"
	"example.com/replicant/internal/runner/s3x"
	"example.com/replicant/internal/runner/sandbox"
	"example.com/replicant/internal/runner/schemas"
)

// D-10 agent-config paths, always excluded from the diff (§4.8).
var agentConfigPaths = []string{"CLAUDE.md", "**/CLAUDE.md", "CLAUDE.local.md", "**/CLAUDE.local.md", "AGENTS.md", "**/AGENTS.md",
	"AGENTS.override.md", "**/AGENTS.override.md", ".claude/**", "**/.claude/**", ".codex/**", "**/.codex/**", ".mcp.json", "**/.mcp.json",
	".cursor/**", "**/.cursor/**"}

type collected struct {
	entries  []sandbox.OutputEntry
	outputs  map[string]string
	changes  map[string]any
	diffKey  string
	empty    bool
	headTree string
	integ    map[string]any
	files    []map[string]any
}

func (rc *runCtx) artifactsKey(typ, name string) string {
	if rc.bs.WS02 == nil {
		return typ + "/" + name
	}
	_, k, _ := s3x.ParseURI(rc.bs.WS02.ArtifactPrefixes.Artifacts)
	return k + typ + "/" + name
}

// killAll is finalize step 1: from PID 1, kill every other process (§4.5).
func killAll(rc *runCtx) {
	if os.Getpid() == 1 {
		_ = syscall.Kill(-1, syscall.SIGKILL)
		time.Sleep(200 * time.Millisecond)
		var ws syscall.WaitStatus
		for {
			pid, err := syscall.Wait4(-1, &ws, syscall.WNOHANG, nil)
			if pid <= 0 || err != nil {
				break
			}
		}
		return
	}
	rc.mu.Lock()
	defer rc.mu.Unlock()
	if rc.cmd != nil && rc.cmd.Process != nil {
		_ = syscall.Kill(-rc.cmd.Process.Pid, syscall.SIGKILL)
	}
}

func synthReport(role, reason string) map[string]any {
	r := map[string]any{"outcome": "failed", "summary": "[replicant] " + reason + "; see handoff"}
	switch role {
	case "reviewer":
		r["verdict"] = "comment"
		r["findings"] = []any{}
	case "planner":
		r["steps"] = []any{}
		r["acceptance_criteria"] = []any{}
	default:
		r["tests_run"] = []any{}
	}
	return r
}

// finalize implements §4.5 finalize steps 1–8 and returns the exit code.
func (rc *runCtx) finalize(ctx context.Context, forced int, cliExit string) int {
	killAll(rc)
	p := rc.plan
	role := p.Role
	code := forced
	rc.mu.Lock()
	cause := rc.stopCause
	res := rc.lastResult
	cfgErr := rc.configErr
	rc.mu.Unlock()
	if code == 0 {
		switch cause {
		case "budget":
			code = exitBudget
		case "gateway":
			code = exitGatewayDown
		case "disk":
			code = exitDisk
		case "cancel", "heartbeat":
			code = exitTerminated
		case "version", "protocol", "hang":
			code = exitAgentNonZero
		case "config":
			code = exitResultInvalid
		}
	}
	if code == exitResultInvalid && cfgErr == "" {
		cfgErr = "config regression"
	}
	var report map[string]any
	limitReason := ""
	subtype, _ := res["subtype"].(string)
	switch {
	case code == exitBudget:
		limitReason = "budget exhausted"
	case code == exitGatewayDown:
		limitReason = "gateway unavailable"
	case code == exitDisk:
		limitReason = "workspace disk exceeded"
	case code == exitOOM:
		limitReason = "out of memory (a process of the agent container was OOM-killed)"
	case code == exitTerminated:
		limitReason = "run stopped (" + rc.cancelReason + ")"
	case code == exitEnvSetup:
		limitReason = "environment setup failed"
	case subtype == "error_max_turns" || cause == "turns" || rc.turnBudgetExhausted() && res == nil:
		limitReason = "turn budget exhausted"
	case subtype == "error_max_budget_usd":
		code, limitReason = exitBudget, "budget exhausted"
	}
	if so, ok := res["structured_output"].(map[string]any); ok && limitReason == "" {
		report = so
	} else if limitReason == "" && res != nil {
		// fallback: result text is the JSON object
		if txt, ok := res["result"].(string); ok {
			var m map[string]any
			if json.Unmarshal([]byte(strings.TrimSpace(txt)), &m) == nil && m["outcome"] != nil {
				report = m
			}
		}
	}
	if code == 0 && report == nil && limitReason == "" {
		// No structured output: a crash (no result event and a non-zero exit) or an errored result (API errors,
		// error_during_execution: is_error) is AgentExitedNonZero; a clean result without one is AgentResultMissing.
		isErr, _ := res["is_error"].(bool)
		switch {
		case res == nil && cliExit != "0":
			code = exitAgentNonZero
		case res != nil && isErr:
			code = exitAgentNonZero
			if t, ok := res["result"].(string); ok {
				cfgErr = truncate(t, 500)
			}
		default:
			code = exitResultMissing
		}
	}
	if report != nil && code == 0 {
		schemaJSON, loc, _ := agentReportSchema(p)
		_ = schemaJSON
		if err := schemas.Validate(loc+"#/$defs/agent_report", report); err != nil {
			code, cfgErr = exitResultInvalid, "agent_report does not validate: "+err.Error()
		}
	}
	if report == nil && limitReason != "" {
		report = synthReport(role, limitReason)
	}
	// After kill(-1) the workspace cannot change: collect outputs.
	security := rc.cancelReason == "security" || rc.cancelReason == "unknown"
	col := &collected{outputs: map[string]string{}}
	if code != exitAgentNonZero && code != exitResultMissing {
		rc.collectOutputs(ctx, col, report, code, security)
	}
	if col.integ != nil && col.integ["tracked_files_modified"] == true && p.Agent.OutputContract.WorkspaceIntegrity == "enforce" {
		code, cfgErr = exitResultInvalid, "workspace integrity violated"
	}
	if code == exitResultInvalid {
		writeTermination(code, "", truncate(cfgErr, 900))
		return rc.finish(code, col)
	}
	if code == exitAgentNonZero || code == exitResultMissing {
		msg := "CLI ended without a structured result"
		if code == exitAgentNonZero {
			msg = "CLI exited " + cliExit
			if cause == "hang" {
				msg = "cli_hang"
			} else if cfgErr != "" {
				msg = cfgErr
			}
		}
		writeTermination(code, "", msg)
		return rc.finish(code, col)
	}
	// compose run_result
	var usage ipc.Usage
	_ = rc.agentd.Do(ctx, http.MethodGet, "/v1/usage", nil, nil, &usage)
	var consumed ipc.Consumed
	_ = rc.agentd.Do(ctx, http.MethodGet, "/v1/steering/consumed", nil, nil, &consumed)
	outcome, _ := report["outcome"].(string)
	if limitReason != "" {
		outcome = "failed"
	} else if role == "implementer" || role == "repair" {
		if col.empty && p.Agent.OutputContract.Outputs["diff_bundle"] != "forbidden" && outcome == "completed" {
			outcome = "no_changes"
		}
	}
	if col.integ != nil && col.integ["tracked_files_modified"] == true {
		outcome = "failed"
	}
	summary, _ := report["summary"].(string)
	rr := map[string]any{
		"schema": "replicant.run-result/v1", "run_id": runIDOf(rc), "job_id": rc.manifest.JobID, "role": role,
		"outcome": outcome, "summary": truncate(summary, 8000), "outputs": col.outputs,
		"usage": map[string]any{"wall_seconds": round1(time.Since(rc.start).Seconds()), "model_calls": usage.ModelCalls,
			"input_tokens": usage.InputTokens, "output_tokens": usage.OutputTokens, "cache_read_tokens": usage.CacheReadTokens,
			"cache_write_tokens": usage.CacheWriteTokens, "cost_usd_reported": usage.CostUSDReported},
		"memory_entries_used": nonNil(rc.manifest.MemoryEntries),
		"steering_consumed":   nonNil(consumed.MessageIDs),
		"role_result":         rc.roleResult(report, col, usage, len(consumed.MessageIDs), code),
	}
	if peak := readInt(sandbox.DiskPeak); peak > 0 {
		rr["usage"].(map[string]any)["peak_workspace_bytes"] = peak
	}
	if mp := readInt("/sys/fs/cgroup/memory.peak"); mp > 0 {
		rr["usage"].(map[string]any)["peak_memory_bytes"] = mp
	}
	b, _ := json.MarshalIndent(rr, "", "  ")
	if err := schemas.Validate(schemas.RunResult, b); err != nil {
		writeTermination(exitResultInvalid, "", "run_result does not validate: "+truncate(err.Error(), 800))
		return rc.finish(exitResultInvalid, col)
	}
	if rs := rr["role_result"].(map[string]any); rs != nil {
		if loc := schemas.RoleSchemaFor(fmt.Sprint(rs["schema"])); loc != "" {
			if err := schemas.Validate(loc, rs); err != nil {
				writeTermination(exitResultInvalid, "", "role_result does not validate: "+truncate(err.Error(), 800))
				return rc.finish(exitResultInvalid, col)
			}
		}
	}
	_ = os.MkdirAll(filepath.Dir(sandbox.ResultPath), 0o755)
	if err := os.WriteFile(sandbox.ResultPath, b, 0o644); err != nil {
		writeTermination(exitAgentNonZero, "", "write run_result: "+err.Error())
		return rc.finish(exitAgentNonZero, col)
	}
	col.entries = append([]sandbox.OutputEntry{{Type: "run_result", BucketClass: "artifacts", Path: sandbox.ResultPath, Name: "result.json",
		ContentType: "application/json", Required: true, Redaction: "required", Producer: "entrypoint"}}, col.entries...)
	switch code {
	case exitBudget:
		scope := "run"
		if usage.BudgetExceededScope != "" {
			scope = usage.BudgetExceededScope
		}
		writeTermination(code, "", fmt.Sprintf("%s: budget %.2f USD exhausted at %.2f USD", scopePrefix(scope), usage.BudgetLimitUSD, usage.CostUSDReported))
	case exitOK:
	default:
		writeTermination(code, "", limitReason)
	}
	return rc.finishWithResult(code, col)
}

func scopePrefix(s string) string {
	switch s {
	case "job", "account":
		return "job"
	case "tenant_period", "platform":
		return "tenant_period"
	}
	return "run"
}

func runIDOf(rc *runCtx) string {
	if v := os.Getenv("REPLICANT_RUN_ID"); v != "" {
		return v
	}
	return rc.manifest.RunID
}

func nonNil(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

func readInt(p string) int64 {
	b, err := os.ReadFile(p)
	if err != nil {
		return 0
	}
	v, _ := strconv.ParseInt(strings.TrimSpace(string(b)), 10, 64)
	return v
}

func (rc *runCtx) cliExecution(code int) map[string]any {
	rc.mu.Lock()
	defer rc.mu.Unlock()
	tb := rc.terminatedBy
	if tb == "" {
		tb = "none"
	}
	c := map[string]any{"cli": "claude-code", "version": firstNonEmpty(rc.cliVer, cliVersion()), "exit_code": code, "terminated_by": tb}
	if rc.sessionID != "" {
		c["session_id"] = rc.sessionID
	}
	if r := rc.lastResult; r != nil {
		if s, ok := r["subtype"].(string); ok {
			c["result_subtype"] = s
		}
		if s, ok := r["stop_reason"].(string); ok {
			c["stop_reason"] = s
		}
		for k, f := range map[string]string{"num_turns": "num_turns", "duration_ms": "duration_ms", "duration_api_ms": "duration_api_ms"} {
			if v, ok := r[f].(float64); ok {
				c[k] = int64(v)
			}
		}
		if pd, ok := r["permission_denials"].([]any); ok {
			c["permission_denials"] = len(pd)
		}
	}
	if len(rc.mcpStatus) > 0 {
		c["mcp_servers"] = rc.mcpStatus
	}
	if len(rc.instructions) > 0 {
		c["instruction_files_loaded"] = rc.instructions
	}
	return c
}

func firstNonEmpty(a ...string) string {
	for _, s := range a {
		if s != "" {
			return s
		}
	}
	return ""
}

func (rc *runCtx) roleResult(report map[string]any, col *collected, u ipc.Usage, steered, code int) map[string]any {
	budget := map[string]any{"effective_usd": firstPos(u.BudgetLimitUSD, rc.plan.Effective.BudgetUSD), "spent_usd": u.CostUSDReported,
		"soft_limit_reached": u.SoftLimitReached, "hard_limit_reached": u.HardLimitReached, "rate_limited_seconds": u.RateLimitedSeconds,
		"turns_used": rc.turnsUsed()}
	role := rc.plan.Role
	id := rc.plan.Agent.OutputContract.RoleResultSchema
	if id == "" {
		id = "replicant.role-result." + role + "/v1"
	}
	out := map[string]any{"schema": id, "agent_report": report, "cli": rc.cliExecution(code), "budget": budget}
	switch role {
	case "implementer", "repair":
		ch := col.changes
		if ch == nil {
			ch = map[string]any{"files_changed": 0, "insertions": 0, "deletions": 0, "diff_sha256": sha(nil)}
		}
		out["changes"] = ch
		if rc.bootstrapRes != nil {
			out["bootstrap"] = rc.bootstrapRes
		}
		out["session_resumed"] = false
		out["steering_delivered"] = steered
	case "reviewer":
		integ := col.integ
		if integ == nil {
			integ = map[string]any{"mode": "off", "tracked_files_modified": false}
		}
		out["integrity"] = integ
		counts := map[string]any{"blocker": 0, "major": 0, "minor": 0, "nit": 0}
		if fs, ok := report["findings"].([]any); ok {
			for _, f := range fs {
				if m, ok := f.(map[string]any); ok {
					if s, ok := m["severity"].(string); ok {
						if n, ok := counts[s].(int); ok {
							counts[s] = n + 1
						}
					}
				}
			}
		}
		out["counts"] = counts
	}
	return out
}

func firstPos(a ...float64) float64 {
	for _, f := range a {
		if f > 0 {
			return f
		}
	}
	return 0
}

// collectOutputs is §4.8 for the roles v0 runs.
func (rc *runCtx) collectOutputs(ctx context.Context, col *collected, report map[string]any, code int, security bool) {
	p := rc.plan
	role := p.Role
	_ = os.MkdirAll(sandbox.OutDir, 0o775)
	if role == "implementer" || role == "repair" || role == "takeover" {
		if err := rc.collectDiff(ctx, col); err != nil {
			log.Error("diff collection failed", "err", err)
		}
	}
	if mode := p.Agent.OutputContract.WorkspaceIntegrity; (mode == "detect" || mode == "enforce") && rc.integrityPre != "" {
		after, _ := trackedTree(ctx)
		mod := after != rc.integrityPre
		integ := map[string]any{"mode": mode, "tracked_files_modified": mod, "tree_before": rc.integrityPre, "tree_after": after}
		if mod {
			out, _ := git(ctx, sandbox.RepoDir, gitEnv(), "diff", "--name-only", rc.integrityPre, after)
			integ["modified_paths"] = firstN(strings.Fields(out), 200)
		}
		col.integ = integ
	}
	if role == "reviewer" && report != nil {
		rv := map[string]any{"schema": "replicant.review-report/v1", "agent_report": report, "cli": rc.cliExecution(code)}
		if col.integ != nil {
			rv["integrity"] = col.integ
		}
		path := filepath.Join(sandbox.OutDir, "review_report", "review.json")
		if writeOut(path, rv) == nil {
			col.entries = append(col.entries, sandbox.OutputEntry{Type: "review_report", BucketClass: "artifacts", Path: path, Name: "review.json",
				ContentType: "application/json", Required: true, Redaction: "required", Producer: "entrypoint"})
			col.outputs["review_report"] = rc.artifactsKey("review_report", "review.json")
		}
	}
	if role == "planner" && report != nil {
		path := filepath.Join(sandbox.OutDir, "plan", "plan.json")
		if writeOut(path, report) == nil {
			col.entries = append(col.entries, sandbox.OutputEntry{Type: "plan", BucketClass: "artifacts", Path: path, Name: "plan.json",
				ContentType: "application/json", Required: true, Redaction: "required", Producer: "entrypoint"})
			col.outputs["plan"] = rc.artifactsKey("plan", "plan.json")
		}
		// plan.md: the agent's /replicant/out/plan.md, else rendered from the structured plan
		md, err := os.ReadFile(filepath.Join(sandbox.OutDir, "plan.md"))
		if err != nil || len(bytes.TrimSpace(md)) == 0 {
			md = renderPlanMD(report)
		}
		mdPath := filepath.Join(sandbox.OutDir, "plan", "plan.md")
		if os.WriteFile(mdPath, md, 0o644) == nil {
			col.entries = append(col.entries, sandbox.OutputEntry{Type: "plan", BucketClass: "artifacts", Path: mdPath, Name: "plan.md",
				ContentType: "text/markdown", Required: false, Redaction: "required", Producer: "entrypoint"})
		}
	}
	if role != "qa" && role != "eval" {
		rc.writeHandoff(col, report, code)
	}
	if b, err := os.ReadFile(filepath.Join(sandbox.OutDir, "memory_candidates.json")); err == nil && json.Valid(b) && !security {
		path := filepath.Join(sandbox.OutDir, "memory_candidates", "candidates.json")
		_ = os.MkdirAll(filepath.Dir(path), 0o755)
		if os.WriteFile(path, b, 0o644) == nil {
			col.entries = append(col.entries, sandbox.OutputEntry{Type: "memory_candidates", BucketClass: "artifacts", Path: path, Name: "candidates.json",
				ContentType: "application/json", Required: false, Redaction: "required", Producer: "entrypoint"})
			col.outputs["memory_candidates"] = rc.artifactsKey("memory_candidates", "candidates.json")
		}
	}
}

func firstN(s []string, n int) []string {
	if len(s) > n {
		return s[:n]
	}
	if s == nil {
		return []string{}
	}
	return s
}

func writeOut(path string, v any) error {
	_ = os.MkdirAll(filepath.Dir(path), 0o755)
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, b, 0o644)
}

// trackedTree computes `git write-tree` over a temporary index of the tracked files (§6 step 3).
func trackedTree(ctx context.Context) (string, error) {
	idx := filepath.Join(os.TempDir(), fmt.Sprintf("integ-%d.index", time.Now().UnixNano()))
	defer os.Remove(idx)
	env := gitEnv("GIT_INDEX_FILE=" + idx)
	if _, err := git(ctx, sandbox.RepoDir, env, "read-tree", "HEAD"); err != nil {
		return "", err
	}
	if _, err := git(ctx, sandbox.RepoDir, env, "add", "-u", "--", "."); err != nil {
		return "", err
	}
	out, err := git(ctx, sandbox.RepoDir, env, "write-tree")
	return strings.TrimSpace(out), err
}

// collectDiff builds diff_bundle/changes.tar.zst against base_sha with a temporary index (§4.8).
func (rc *runCtx) collectDiff(ctx context.Context, col *collected) error {
	base := rc.plan.Workspace.BaseSHA
	if base == "" {
		base = rc.bs.Repo.BaseSHA
	}
	work := filepath.Join(sandbox.OutDir, ".cli")
	_ = os.MkdirAll(work, 0o755)
	idx := filepath.Join(work, "index")
	_ = os.Remove(idx)
	env := gitEnv("GIT_INDEX_FILE=" + idx)
	repo := sandbox.RepoDir
	if _, err := git(ctx, repo, env, "read-tree", base); err != nil {
		return err
	}
	// neutralize repo-declared filters
	var filterArgs []string
	if out, err := git(ctx, repo, gitEnv(), "config", "--file", ".git/config", "--get-regexp", `^filter\.`); err == nil {
		seen := map[string]bool{}
		for _, l := range strings.Split(out, "\n") {
			k := strings.Fields(l)
			if len(k) == 0 {
				continue
			}
			parts := strings.Split(k[0], ".")
			if len(parts) >= 3 && !seen[parts[1]] {
				seen[parts[1]] = true
				filterArgs = append(filterArgs, "-c", "filter."+parts[1]+".clean=", "-c", "filter."+parts[1]+".smudge=", "-c", "filter."+parts[1]+".process=")
			}
		}
	}
	// all changes (for the stripped list), then the excluded set
	addAll := append(append([]string{}, filterArgs...), "add", "-A", "--", ".")
	if _, err := git(ctx, repo, env, addAll...); err != nil {
		return err
	}
	allNames, _ := git(ctx, repo, env, "diff", "--cached", "--name-only", base)
	if _, err := git(ctx, repo, env, "read-tree", base); err != nil {
		return err
	}
	forbidden := append(append([]string{}, agentConfigPaths...), rc.plan.Agent.OutputContract.DiffPolicy.ForbiddenPaths...)
	args := append(append([]string{}, filterArgs...), "add", "-A", "--", ".")
	for _, f := range forbidden {
		args = append(args, ":(exclude,glob)"+f)
	}
	if _, err := git(ctx, repo, env, args...); err != nil {
		return err
	}
	keptNames, _ := git(ctx, repo, env, "diff", "--cached", "--name-only", base)
	kept := map[string]bool{}
	for _, n := range strings.Fields(keptNames) {
		kept[n] = true
	}
	stripped := []string{}
	for _, n := range strings.Fields(allNames) {
		if !kept[n] {
			stripped = append(stripped, n)
		}
	}
	patch, err := git(ctx, repo, env, "-c", "diff.external=", "-c", "core.pager=cat", "diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", base)
	if err != nil {
		return err
	}
	numstat, _ := git(ctx, repo, env, "diff", "--cached", "--numstat", base)
	ns, _ := git(ctx, repo, env, "diff", "--cached", "--name-status", "-M", base)
	ins, del, files, bin := 0, 0, 0, 0
	numByPath := map[string][2]int{}
	var paths []string
	for _, l := range strings.Split(strings.TrimSpace(numstat), "\n") {
		f := strings.Split(l, "\t")
		if len(f) < 3 {
			continue
		}
		files++
		if f[0] == "-" {
			bin++
			paths = append(paths, f[2])
			continue
		}
		a, _ := strconv.Atoi(f[0])
		d, _ := strconv.Atoi(f[1])
		ins, del = ins+a, del+d
		numByPath[f[2]] = [2]int{a, d}
		paths = append(paths, f[2])
	}
	for _, l := range strings.Split(strings.TrimSpace(ns), "\n") {
		f := strings.Split(l, "\t")
		if len(f) < 2 {
			continue
		}
		ft := map[string]any{"path": f[len(f)-1], "change": "modified"}
		switch f[0][0] {
		case 'A':
			ft["change"] = "added"
		case 'D':
			ft["change"] = "deleted"
		case 'R':
			ft["change"] = "renamed"
			ft["renamed_from"] = f[1]
		}
		if n, ok := numByPath[f[len(f)-1]]; ok {
			ft["insertions"], ft["deletions"] = n[0], n[1]
		}
		col.files = append(col.files, ft)
	}
	head, _ := git(ctx, repo, env, "write-tree")
	col.headTree = strings.TrimSpace(head)
	commits := 0
	var mbox string
	if out, err := git(ctx, repo, gitEnv(), "rev-list", "--count", base+"..HEAD"); err == nil {
		commits, _ = strconv.Atoi(strings.TrimSpace(out))
		if commits > 0 {
			mbox, _ = git(ctx, repo, gitEnv(), "format-patch", base+"..HEAD", "--stdout")
		}
	}
	meta := map[string]any{"base_sha": base, "files_changed": files, "insertions": ins, "deletions": del, "paths": firstN(paths, 500),
		"forbidden_paths_stripped": stripped, "created_by_run": runIDOf(rc)}
	var buf bytes.Buffer
	zw, _ := zstd.NewWriter(&buf)
	tw := tar.NewWriter(zw)
	add := func(name string, b []byte) {
		_ = tw.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(b)), ModTime: time.Unix(0, 0), Format: tar.FormatPAX})
		_, _ = tw.Write(b)
	}
	metaB, _ := json.MarshalIndent(meta, "", "  ")
	add("changes.patch", []byte(patch))
	add("metadata.json", metaB)
	if mbox != "" {
		add("commits.mbox", []byte(mbox))
	}
	_ = tw.Close()
	_ = zw.Close()
	path := filepath.Join(sandbox.OutDir, "diff_bundle", "changes.tar.zst")
	_ = os.MkdirAll(filepath.Dir(path), 0o755)
	if err := os.WriteFile(path, buf.Bytes(), 0o644); err != nil {
		return err
	}
	dsha := sha(buf.Bytes())
	col.empty = files == 0
	col.diffKey = rc.artifactsKey("diff_bundle", "changes.tar.zst")
	col.changes = map[string]any{"files_changed": files, "insertions": ins, "deletions": del, "binary_files": bin,
		"diff_sha256": dsha, "base_sha": base, "commits_by_agent": commits, "forbidden_paths_stripped": stripped, "paths": firstN(paths, 500)}
	col.entries = append(col.entries, sandbox.OutputEntry{Type: "diff_bundle", BucketClass: "artifacts", Path: path, Name: "changes.tar.zst",
		ContentType: "application/vnd.replicant.diff-bundle.v1+tar+zstd", SHA256: dsha, Required: true, Redaction: "scan-block", Producer: "entrypoint",
		Attributes: map[string]any{"files_changed": files, "insertions": ins, "deletions": del}})
	col.outputs["diff_bundle"] = col.diffKey
	return nil
}

// writeHandoff writes handoff/handoff.json (replicant.handoff/v1, WS-04); mechanical facts never come from model text.
func (rc *runCtx) writeHandoff(col *collected, report map[string]any, code int) {
	lai := os.Getenv("REPLICANT_LOGICAL_ACTIVITY_ID")
	node, cycle, ordinal := "node", 0, 1
	if parts := strings.Split(lai, "."); len(parts) >= 3 {
		node = parts[0]
		cycle, _ = strconv.Atoi(strings.TrimPrefix(parts[1], "c"))
		ordinal, _ = strconv.Atoi(strings.TrimPrefix(parts[2], "n"))
	}
	outcome, _ := report["outcome"].(string)
	if code == exitTerminated {
		outcome = "cancelled"
	}
	producer := map[string]any{"job_id": rc.manifest.JobID, "run_id": runIDOf(rc), "role": rc.plan.Role, "node_key": node, "cycle": cycle,
		"ordinal": max(ordinal, 1), "release_bundle_id": rc.manifest.ReleaseBundleID, "cli": "claude-code", "cli_version": cliVersion(),
		"model": rc.plan.Agent.Model.Primary, "produced_at": nowRFC()}
	if outcome != "" {
		producer["run_outcome"] = outcome
	}
	if rc.manifest.MemorySnapshotID != "" {
		producer["memory_snapshot_id"] = rc.manifest.MemorySnapshotID
	}
	goal := map[string]any{"summary": "See the task in the first message."}
	if b, err := os.ReadFile(filepath.Join(sandbox.InputsDir, "context", "task_packet.json")); err == nil {
		var tp struct {
			Goal map[string]any `json:"goal"`
		}
		if json.Unmarshal(b, &tp) == nil && tp.Goal != nil {
			g := map[string]any{}
			if s, ok := tp.Goal["summary"].(string); ok {
				g["summary"] = truncate(s, 2000)
			}
			if ac, ok := tp.Goal["acceptance_criteria"].([]any); ok {
				g["acceptance_criteria"] = ac
			}
			g["task_packet_sha256"] = sha(b)
			if g["summary"] != nil {
				goal = g
			}
		}
	} else if fm, err := os.ReadFile(filepath.Join(sandbox.InputsDir, "first-message.md")); err == nil {
		line := strings.TrimSpace(strings.SplitN(strings.TrimSpace(string(fm)), "\n", 2)[0])
		if line != "" {
			goal["summary"] = truncate(line, 2000)
		}
	}
	base := rc.plan.Workspace.BaseSHA
	rc.mu.Lock()
	cmds := []map[string]any{}
	order := rc.cmdOrder
	if len(order) > 200 {
		order = order[len(order)-200:]
	}
	for _, c := range order {
		r := rc.commands[c]
		cmds = append(cmds, map[string]any{"command": r.Command, "exit_code": r.ExitCode, "count": r.Count, "classified_as": r.Class})
	}
	rc.mu.Unlock()
	files := col.files
	if files == nil {
		files = []map[string]any{}
	}
	mech := map[string]any{"base_sha": base, "files_touched": files, "commands": cmds, "memory_injected": nonNil(rc.manifest.MemoryEntries)}
	if col.headTree != "" {
		mech["head_tree_sha"] = col.headTree
	}
	if col.diffKey != "" {
		mech["diff_bundle_key"] = col.diffKey
		mech["diff_stats"] = map[string]any{"files_changed": col.changes["files_changed"], "insertions": col.changes["insertions"], "deletions": col.changes["deletions"]}
	}
	if tr, ok := report["tests_run"].([]any); ok {
		var tests []map[string]any
		for _, t := range tr {
			if m, ok := t.(map[string]any); ok {
				st := "passed"
				if ec, _ := m["exit_code"].(float64); ec != 0 {
					st = "failed"
				}
				tests = append(tests, map[string]any{"command": truncate(fmt.Sprint(m["command"]), 1000), "status": st})
			}
		}
		if tests != nil {
			mech["tests"] = firstNMaps(tests, 50)
		}
	}
	h := map[string]any{"schema": "replicant.handoff/v1", "producer": producer, "goal": goal, "mechanical": mech}
	if md, err := os.ReadFile(filepath.Join(sandbox.OutDir, "handoff.md")); err == nil && len(bytes.TrimSpace(md)) > 0 {
		status := "markdown_only"
		if len(md) > 64<<10 {
			md, status = md[:64<<10], "truncated"
		}
		h["narrative_markdown"], h["narrative_status"] = string(md), status
	} else if s, _ := report["summary"].(string); s != "" {
		h["narrative_markdown"], h["narrative_status"], h["synthesized"] = s, "markdown_only", true
	} else {
		h["narrative_status"] = "missing"
	}
	b, _ := json.MarshalIndent(h, "", "  ")
	if err := schemas.Validate("https://schemas.replicant.example.com/ws04/handoff/v1.json", b); err != nil {
		log.Warn("handoff does not validate; written anyway", "err", err)
	}
	path := filepath.Join(sandbox.OutDir, "handoff", "handoff.json")
	_ = os.MkdirAll(filepath.Dir(path), 0o755)
	if os.WriteFile(path, b, 0o644) == nil {
		req := rc.plan.Agent.OutputContract.Outputs["handoff"] == "required"
		col.entries = append(col.entries, sandbox.OutputEntry{Type: "handoff", BucketClass: "artifacts", Path: path, Name: "handoff.json",
			ContentType: "application/json", Required: req, Redaction: "required", Producer: "entrypoint"})
		col.outputs["handoff"] = rc.artifactsKey("handoff", "handoff.json")
	}
}

func firstNMaps(m []map[string]any, n int) []map[string]any {
	if len(m) > n {
		return m[len(m)-n:]
	}
	return m
}

// finish writes outputs.json (atomic), the markers, reports `finished`, and returns code. No run_result.
func (rc *runCtx) finish(code int, col *collected) int {
	return rc.writeIndex(code, col, false)
}

func (rc *runCtx) finishWithResult(code int, col *collected) int {
	return rc.writeIndex(code, col, true)
}

func (rc *runCtx) writeIndex(code int, col *collected, withResult bool) int {
	if col == nil {
		col = &collected{}
	}
	entries := col.entries
	if !withResult {
		var keep []sandbox.OutputEntry
		for _, e := range entries {
			if e.Type != "run_result" {
				keep = append(keep, e)
			}
		}
		entries = keep
	}
	if entries == nil {
		entries = []sandbox.OutputEntry{}
	}
	sort.SliceStable(entries, func(i, j int) bool { return entries[i].Type == "run_result" && entries[j].Type != "run_result" })
	idx := sandbox.OutputsIndex{Schema: "replicant.run-outputs/v1", RunID: runIDOf(rc), ExitCode: code, Partial: code != 0, Entries: entries}
	if withResult {
		idx.Result = sandbox.ResultPath
	}
	_ = os.MkdirAll(sandbox.OutDir, 0o775)
	if b, _ := json.Marshal(idx); schemas.Validate(schemas.RunOutputs, b) != nil {
		log.Warn("outputs.json does not validate", "err", schemas.Validate(schemas.RunOutputs, b))
	}
	if err := sandbox.WriteJSONAtomic(filepath.Join(sandbox.OutDir, "outputs.json"), idx, 0o644); err != nil {
		log.Error("write outputs.json", "err", err)
	}
	mkStatusDir()
	_ = os.WriteFile(sandbox.MarkerAgentExit, []byte(nowRFC()), 0o644)
	_ = rc.agentd.Status(context.Background(), "finished")
	_ = os.WriteFile(sandbox.MarkerFinished, []byte(nowRFC()), 0o644)
	log.Info("run finished", "exit_code", code, "entries", len(entries))
	return code
}

func renderPlanMD(report map[string]any) []byte {
	var b strings.Builder
	b.WriteString("# Plan\n\n")
	if s, ok := report["summary"].(string); ok {
		b.WriteString(s + "\n\n")
	}
	if steps, ok := report["steps"].([]any); ok && len(steps) > 0 {
		b.WriteString("## Steps\n\n")
		for _, st := range steps {
			if m, ok := st.(map[string]any); ok {
				fmt.Fprintf(&b, "- %v: %v\n", m["id"], m["title"])
			}
		}
		b.WriteString("\n")
	}
	if acs, ok := report["acceptance_criteria"].([]any); ok && len(acs) > 0 {
		b.WriteString("## Acceptance criteria\n\n")
		for _, a := range acs {
			if m, ok := a.(map[string]any); ok {
				fmt.Fprintf(&b, "- %v: %v\n", m["id"], m["text"])
			}
		}
	}
	return []byte(b.String())
}
