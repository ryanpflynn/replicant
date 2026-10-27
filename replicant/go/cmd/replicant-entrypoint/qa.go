package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"example.com/replicant/internal/runner/ipc"
	"example.com/replicant/internal/runner/sandbox"
	"example.com/replicant/internal/runner/schemas"
)

var qaTypes = map[string]bool{"qa_report": true, "junit_xml": true, "build_log": true, "health_log": true, "screenshot": true,
	"playwright_trace": true, "video": true, "a11y_report": true, "sbom": true, "image_metadata": true, "secret_scan_report": true,
	"qa_generated_flow": true}

// runQA is §4.10: run the replicant-qa harness instead of the CLI and map its exit class.
func (rc *runCtx) runQA(ctx context.Context, term chan os.Signal) int {
	schemaJSON, _, _ := agentReportSchema(rc.plan)
	_ = renderHome(rc.plan, schemaJSON) // the harness may launch the CLI for agent tasks
	if _, err := joinPrompt(rc.manifest); err != nil {
		log.Warn("joined prompt", "err", err)
	}
	outDir := filepath.Join(sandbox.OutDir, "qa")
	_ = os.MkdirAll(outDir, 0o775)
	cmd := exec.Command("/opt/replicant/bin/replicant-qa", "run", "--inputs", sandbox.InputsDir, "--out", outDir, "--plan",
		filepath.Join(sandbox.InputsDir, "run-plan.json"))
	cmd.Dir = sandbox.RepoDir
	cmd.Env = cliEnv(rc.plan, os.Getenv("TRACEPARENT"))
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Stdout = spoolWriter{rc.spool, "sandbox_log"}
	cmd.Stderr = spoolWriter{rc.spool, "sandbox_log"}
	mkStatusDir()
	if err := cmd.Start(); err != nil {
		writeTermination(exitAgentNonZero, "", "replicant-qa start: "+err.Error())
		return rc.finish(exitAgentNonZero, nil)
	}
	rc.cliStart = time.Now()
	_ = os.WriteFile(sandbox.MarkerAgentStarted, []byte(nowRFC()), 0o644)
	_ = rc.agentd.Status(ctx, "agent-started")
	rc.mu.Lock()
	rc.cmd = cmd
	rc.mu.Unlock()
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	events := make(chan ipc.Event, 16)
	ectx, cancel := context.WithCancel(ctx)
	defer cancel()
	go rc.pollEvents(ectx, events)
	forced := 0
	var hErr error
loop:
	for {
		select {
		case hErr = <-done:
			break loop
		case ev := <-events:
			if (ev.Kind == ipc.EvCancelRequested || ev.Kind == ipc.EvHeartbeatLost) && forced == 0 {
				rc.cancelReason, forced = ev.Reason, exitTerminated
				rc.stopGracefully()
			}
		case <-term:
			if forced == 0 {
				rc.cancelReason, forced = rc.cancelReasonOnTerm(ctx), exitTerminated
				rc.stopGracefully()
			}
		case <-time.After(time.Second):
			if exists(sandbox.MarkerDiskExceeded) && forced == 0 {
				forced = exitDisk
				rc.stop(syscall.SIGINT)
			}
		}
	}
	killAll(rc)
	var hr struct {
		Verdict    string         `json:"verdict"`
		ExitClass  string         `json:"exit_class"`
		RoleResult map[string]any `json:"role_result"`
	}
	b, err := os.ReadFile(filepath.Join(outDir, "result.json"))
	if err != nil || json.Unmarshal(b, &hr) != nil || hr.RoleResult == nil {
		if forced != 0 {
			writeTermination(forced, "", "QA harness stopped")
			return rc.finish(forced, nil)
		}
		code := exitResultMissing
		if hErr != nil {
			code = exitAgentNonZero
		}
		writeTermination(code, "", "replicant-qa wrote no result.json")
		return rc.finish(code, nil)
	}
	col := &collected{outputs: map[string]string{}}
	// harness entries, validated before merging
	var he struct {
		Entries []sandbox.OutputEntry `json:"entries"`
	}
	if eb, err := os.ReadFile(filepath.Join(outDir, "outputs.entries.json")); err == nil {
		if json.Unmarshal(eb, &he) != nil {
			var list []sandbox.OutputEntry
			if json.Unmarshal(eb, &list) == nil {
				he.Entries = list
			}
		}
	}
	for _, e := range he.Entries {
		clean := filepath.Clean(e.Path)
		okPath := strings.HasPrefix(clean, outDir+"/") ||
			(e.Type == "qa_report" && strings.HasPrefix(clean, filepath.Join(sandbox.OutDir, "qa-checkpoints")+"/"))
		t, known := sandbox.Types[e.Type]
		if !qaTypes[e.Type] || !okPath || !known {
			log.Warn("harness entry rejected", "type", e.Type, "path", e.Path)
			continue
		}
		if fi, err := os.Stat(clean); err != nil || fi.Size() > t.MaxBytes && !t.Text {
			log.Warn("harness entry missing or too large", "path", e.Path)
			continue
		}
		e.Producer, e.BucketClass, e.Path = "replicant-qa", "artifacts", clean
		if e.Redaction == "" {
			e.Redaction = t.Redaction
		}
		if e.ContentType == "" {
			e.ContentType = t.ContentType
		}
		col.entries = append(col.entries, e)
		if e.Type == "qa_report" && col.outputs["qa_report"] == "" && !strings.Contains(e.Name, "checkpoints/") {
			col.outputs["qa_report"] = rc.artifactsKey("qa_report", e.Name)
		}
	}
	code := exitOK
	switch hr.ExitClass {
	case "pass", "pass_with_warnings", "fail", "inconclusive_other", "not_applicable":
	case "environment_setup":
		code = exitEnvSetup
		hr.RoleResult["agentrun_exit_class"] = "environment_setup"
	case "environment_services":
		code = exitEnvServices
		hr.RoleResult["agentrun_exit_class"] = "environment_services"
	default:
		code = exitAgentNonZero
	}
	if forced != 0 {
		code = forced
	}
	if code == exitAgentNonZero {
		writeTermination(code, "", "unknown QA exit class "+hr.ExitClass)
		return rc.finish(code, col)
	}
	if hr.RoleResult["schema"] == nil {
		hr.RoleResult["schema"] = "replicant.role-result.qa/v1"
	}
	if err := schemas.Validate(schemas.RoleQA, hr.RoleResult); err != nil {
		writeTermination(exitResultInvalid, "", "QA role_result invalid: "+truncate(err.Error(), 800))
		return rc.finish(exitResultInvalid, col)
	}
	verdict, _ := hr.RoleResult["verdict"].(string)
	outcome := map[string]string{"pass": "completed", "pass_with_warnings": "completed", "not_applicable": "no_changes",
		"fail": "failed", "inconclusive": "failed"}[verdict]
	if outcome == "" {
		outcome = "failed"
	}
	var usage ipc.Usage
	_ = rc.agentd.Do(ctx, http.MethodGet, "/v1/usage", nil, nil, &usage)
	summary := fmt.Sprintf("QA verdict %s (%s)", verdict, hr.ExitClass)
	if rc, ok := hr.RoleResult["reason_code"].(string); ok {
		summary += ": " + rc
	}
	rr := map[string]any{"schema": "replicant.run-result/v1", "run_id": runIDOf(rc), "job_id": rc.manifest.JobID, "role": "qa",
		"outcome": outcome, "summary": summary, "outputs": col.outputs,
		"usage": map[string]any{"wall_seconds": round1(time.Since(rc.start).Seconds()), "model_calls": usage.ModelCalls,
			"input_tokens": usage.InputTokens, "output_tokens": usage.OutputTokens, "cost_usd_reported": usage.CostUSDReported},
		"memory_entries_used": nonNil(rc.manifest.MemoryEntries), "steering_consumed": []string{}, "role_result": hr.RoleResult}
	rb, _ := json.MarshalIndent(rr, "", "  ")
	if err := schemas.Validate(schemas.RunResult, rb); err != nil {
		writeTermination(exitResultInvalid, "", "run_result invalid: "+truncate(err.Error(), 800))
		return rc.finish(exitResultInvalid, col)
	}
	_ = os.MkdirAll(filepath.Dir(sandbox.ResultPath), 0o755)
	if err := os.WriteFile(sandbox.ResultPath, rb, 0o644); err != nil {
		return rc.finish(exitAgentNonZero, col)
	}
	col.entries = append([]sandbox.OutputEntry{{Type: "run_result", BucketClass: "artifacts", Path: sandbox.ResultPath, Name: "result.json",
		ContentType: "application/json", Required: true, Redaction: "required", Producer: "entrypoint"}}, col.entries...)
	if code != exitOK {
		writeTermination(code, "", "QA "+hr.ExitClass)
	}
	return rc.finishWithResult(code, col)
}

type spoolWriter struct {
	s   *spool
	typ string
}

func (w spoolWriter) Write(b []byte) (int, error) {
	w.s.write(w.typ, b)
	os.Stdout.Write(b)
	return len(b), nil
}
