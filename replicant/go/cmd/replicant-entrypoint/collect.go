package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"example.com/replicant/internal/runner/ipc"
	"example.com/replicant/internal/runner/s3x"
	"example.com/replicant/internal/runner/sandbox"
	"example.com/replicant/internal/runner/schemas"
)

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
