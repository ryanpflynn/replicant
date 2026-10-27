// Command replicant-entrypoint is the WS-03 runner entrypoint (cli-invocation.md §4): `prepare` (init
// container), `run` (PID 1 of the agent container: bootstrap, CLI or QA harness, supervise, finalize),
// `hook <name>` (Claude Code managed hooks), `materialize` (dry-run of the CLI configuration) and `version`.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"runtime/debug"
	"strconv"
	"strings"
	"time"

	"example.com/replicant/internal/runner/httpx"
	"example.com/replicant/internal/runner/sandbox"
)

var version = "1.1.0"

// Exit codes (§4.6, normative).
const (
	exitOK             = 0
	exitAgentNonZero   = 10
	exitResultMissing  = 11
	exitResultInvalid  = 12
	exitBudget         = 20
	exitGatewayDown    = 21
	exitDisk           = 30
	exitOOM            = 31
	exitInputs         = 40
	exitDiffApply      = 41
	exitSessionRestore = 42
	exitRepoFetch      = 43
	exitEnvSetup       = 44
	exitEnvServices    = 45
	exitTerminated     = 143
)

var log = httpx.Logger("replicant-entrypoint")

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: replicant-entrypoint prepare|run|hook <name>|materialize|version")
		os.Exit(2)
	}
	switch os.Args[1] {
	case "prepare":
		os.Exit(guard(prepareMain, exitInputs))
	case "run":
		start := time.Now()
		code := guard(runMain, exitAgentNonZero)
		emitSandboxRun(start, code)
		os.Exit(code)
	case "hook":
		name := ""
		if len(os.Args) > 2 {
			name = os.Args[2]
		}
		os.Exit(hookMain(name))
	case "materialize":
		os.Exit(materializeMain(os.Args[2:]))
	case "version":
		fmt.Printf("replicant-entrypoint %s claude-code %s\n", version, cliVersion())
	default:
		fmt.Fprintln(os.Stderr, "unknown subcommand", os.Args[1])
		os.Exit(2)
	}
}

// guard recovers panics (§4.5 "Entrypoint internal panic": exit 10 with entrypoint_panic).
func guard(f func() int, panicCode int) (code int) {
	defer func() {
		if r := recover(); r != nil {
			log.Error("entrypoint_panic", "panic", fmt.Sprint(r), "stack", string(debug.Stack()))
			writeTermination(panicCode, "AgentExitedNonZero", "entrypoint_panic")
			code = panicCode
		}
	}()
	return f()
}

// cliVersion is the Claude Code version shipped in this image (/opt/replicant/cli/claude-code/<v>/claude).
func cliVersion() string {
	if v := os.Getenv("REPLICANT_CLI_VERSION"); v != "" {
		return v
	}
	ents, err := os.ReadDir("/opt/replicant/cli/claude-code")
	if err != nil {
		return ""
	}
	for _, e := range ents {
		if e.IsDir() {
			return e.Name()
		}
	}
	return ""
}

var reasonByCode = map[int]string{
	exitAgentNonZero: "AgentExitedNonZero", exitResultMissing: "AgentResultMissing", exitResultInvalid: "AgentResultInvalid",
	exitBudget: "GatewayBudgetExceeded", exitGatewayDown: "GatewayUnavailable", exitDisk: "WorkspaceDiskExceeded",
	exitOOM: "OOMKilled", exitInputs: "InputsFetchFailed", exitDiffApply: "DiffApplyFailed", exitSessionRestore: "SessionRestoreFailed",
	exitRepoFetch: "RepoFetchFailed", exitEnvSetup: "EnvironmentSetupFailed", exitEnvServices: "EnvironmentSetupFailed",
}

// writeTermination writes the one-line JSON termination message (≤ 1,024 bytes, §4.5 step 7).
func writeTermination(code int, reason, message string) {
	if reason == "" {
		reason = reasonByCode[code]
	}
	b, _ := json.Marshal(map[string]any{"code": code, "reason": reason, "message": message})
	if len(b) > 1024 {
		message = message[:max(0, len(message)-(len(b)-1024)-3)] + "..."
		b, _ = json.Marshal(map[string]any{"code": code, "reason": reason, "message": message})
	}
	paths := []string{sandbox.TerminationDefault}
	if p := os.Getenv("REPLICANT_TERMINATION_MESSAGE_PATH"); p != "" {
		paths = append(paths, p)
	}
	for _, p := range paths {
		if strings.HasPrefix(p, "/replicant/out/") {
			_ = os.MkdirAll(dirOf(p), 0o755)
		}
		_ = os.WriteFile(p, b, 0o644)
	}
	fmt.Fprintln(os.Stderr, string(b))
}

func dirOf(p string) string {
	i := strings.LastIndex(p, "/")
	if i <= 0 {
		return "/"
	}
	return p[:i]
}

func nowRFC() string { return time.Now().UTC().Format(time.RFC3339) }

// mkStatusDir creates /run/replicant/ipc/status world-writable (sticky): the watchdog (10003), the entrypoint
// (10001) and agentd (10000) all write marker files there.
func mkStatusDir() {
	_ = os.MkdirAll(sandbox.StatusDir, 0o777)
	_ = os.Chmod(sandbox.StatusDir, 0o1777)
}

// emitSandboxRun exports span `sandbox.run` (C-IDS T-5b: span ID = sha256("replicant.run.span.v1:"+run_id)[:16]) for the
// whole agent lifetime, in the trace of TRACEPARENT (the job trace, T-1) under its span; the gateway shim parents
// requests without a traceparent under the same ID (WS-08 M3).
func emitSandboxRun(start time.Time, code int) {
	e := otel("replicant-entrypoint")
	runID, jobID := os.Getenv("REPLICANT_RUN_ID"), os.Getenv("REPLICANT_JOB_ID")
	if e == nil || !strings.HasPrefix(runID, "run_") {
		return
	}
	trace, parent := otelJobTraceID(jobID), otelJobRootSpanID(jobID)
	if p := strings.Split(os.Getenv("TRACEPARENT"), "-"); len(p) == 4 && len(p[1]) == 32 && len(p[2]) == 16 {
		trace, parent = p[1], p[2]
	}
	e.emitSync(otelSpan{TraceID: trace, SpanID: otelRunSpanID(runID), ParentID: parent, Name: "sandbox.run", Kind: 1,
		Start: start, End: time.Now(), Error: code != 0, Attrs: map[string]string{
			"replicant.job_id": jobID, "replicant.run_id": runID, "replicant.tenant_id": os.Getenv("REPLICANT_TENANT_ID"),
			"replicant.role": os.Getenv("REPLICANT_ROLE"), "replicant.release_bundle_id": os.Getenv("REPLICANT_RELEASE_BUNDLE_ID"),
			"replicant.cli": "claude-code", "process.exit_code": strconv.Itoa(code)}})
}
