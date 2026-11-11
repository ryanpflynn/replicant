package main

import (
	"regexp"
	"strconv"
	"strings"

	"example.com/replicant/internal/runner/sandbox"
)

// commandSpec is one repo command stage (onboarding-interfaces.md §5 environment.commands).
type commandSpec struct {
	stage, command, failCause string
	timeoutS                  int
}

// commandStages runs the repo's own install, build, test and lint commands through the shell, in that order, and
// classifies each failure (§3 probe_failure causes): a missing tool or an unreachable network host is the
// environment's fault (inconclusive, like a failed install); a build or test failure is the change's. Lint is
// advisory: a lint failure is a warning. A stage after a blocking failure is not run.
func (h *harness) commandStages(c *sandbox.EnvCommands, counts map[string]int) (stages []stageResult, failures []map[string]any,
	envFailures int, verdict, reason, exitClass string) {
	verdict, reason, exitClass = "pass", "all_passed", "pass"
	specs := []commandSpec{{"install", c.Install, "install_failure", 900}, {"build", c.Build, "build_failure", 900},
		{"unit", c.Test, "test_failure", 1800}, {"lint", c.Lint, "lint_failure", 600}}
	stopped := false
	for _, sp := range specs {
		if strings.TrimSpace(sp.command) == "" {
			continue
		}
		if stopped {
			stages = append(stages, stageResult{Stage: sp.stage, Status: "not_run", Command: sp.command})
			continue
		}
		st := h.stage(sp.stage, sp.timeoutS, shell(), "-c", sp.command)
		st.Command = sp.command
		parsed := false
		if sp.stage == "unit" {
			parsed = testCounts(string(st.log), counts)
		}
		if st.Status == "passed" {
			stages = append(stages, st)
			continue
		}
		cause, detail := sandbox.ClassifyFailure(st.exitCode, st.timedOut, st.log)
		env := cause == sandbox.CauseMissingTool || cause == sandbox.CauseNetwork
		if cause == "" {
			cause = sp.failCause
		}
		st.Cause, st.Detail = cause, detail
		blocking := true
		switch {
		case env || sp.stage == "install":
			verdict, reason, exitClass = "inconclusive", "environment_failure", "environment_setup"
			stopped = true
		case sp.stage == "lint":
			st.Status, blocking = "warning", false
			if verdict == "pass" {
				verdict, reason, exitClass = "pass_with_warnings", "non_blocking_findings", "pass_with_warnings"
			}
		case sp.stage == "build":
			verdict, reason, exitClass = "fail", "build_broken", "fail"
			stopped = true
		default:
			verdict, reason, exitClass = "fail", "regression", "fail"
			if !parsed && cause == "test_failure" && looksLikeBuildError(string(st.log)) {
				reason = "build_broken"
			}
			stopped = true
		}
		stages = append(stages, st)
		if blocking && !(parsed && counts["failed"] > 0) {
			// no per-test counts: the failed stage counts as one blocking failure
			counts["total"]++
			counts["failed"]++
			counts["blocking"]++
			if env {
				envFailures++
			}
		}
		fcause := "code"
		if env {
			fcause = "environment"
		}
		if len(failures) < 20 {
			msg := lastLines(string(st.log), 8)
			if detail != "" {
				msg = cause + ": " + detail + "\n" + msg
			}
			failures = append(failures, map[string]any{"stage": sp.stage, "test_id": "command:" + sp.stage, "status": "failed",
				"cause": fcause, "blocking": blocking, "message": trunc(msg, 500)})
		}
	}
	return
}

func shell() string {
	if fileExists("/bin/bash") {
		return "/bin/bash"
	}
	return "/bin/sh"
}

var (
	// pytest: "==== 1 failed, 3 passed, 1 skipped in 0.12s ====" or (-q) "3 passed in 0.01s"
	pytestRe = regexp.MustCompile(`(?m)^[= ]*((?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|warnings?|deselected)(?:, )?)+) in [\d.]+s`)
	// jest: "Tests:       1 failed, 2 passed, 3 total"
	jestRe = regexp.MustCompile(`(?m)^Tests:\s+(.*\d+ total)\s*$`)
	// vitest: "      Tests  1 failed | 2 passed (3)"
	vitestRe = regexp.MustCompile(`(?m)^\s*Tests\s+(.*?)\s*\(\d+\)\s*$`)
	countRe  = regexp.MustCompile(`(\d+) (passed|failed|errors?|skipped|todo|pending)`)
	ansiRe   = regexp.MustCompile("\x1b\\[[0-9;]*m")
)

// testCounts fills counts from a pytest, jest or vitest summary line (the last one wins); false when none is found.
func testCounts(log string, counts map[string]int) bool {
	log = ansiRe.ReplaceAllString(log, "")
	var summary string
	for _, re := range []*regexp.Regexp{pytestRe, jestRe, vitestRe} {
		if m := re.FindAllStringSubmatch(log, -1); len(m) > 0 {
			summary = m[len(m)-1][1]
			break
		}
	}
	if summary == "" {
		return false
	}
	for _, m := range countRe.FindAllStringSubmatch(summary, -1) {
		n, _ := strconv.Atoi(m[1])
		switch m[2] {
		case "passed":
			counts["passed"] += n
		case "failed", "error", "errors":
			counts["failed"] += n
			counts["blocking"] += n
		case "skipped", "todo", "pending":
			counts["skipped"] += n
		}
	}
	counts["total"] = counts["passed"] + counts["failed"] + counts["skipped"]
	return true
}

// looksLikeBuildError: the test command failed before running tests (collection or compile errors).
func looksLikeBuildError(log string) bool {
	for _, s := range []string{"SyntaxError", "error TS", "ImportError while importing", "build failed", "[build failed]", "cannot find module"} {
		if strings.Contains(log, s) {
			return true
		}
	}
	return false
}
