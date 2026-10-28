package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"example.com/replicant/internal/runner/ipc"
	"example.com/replicant/internal/runner/sandbox"
	"example.com/replicant/internal/runner/schemas"
)

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
