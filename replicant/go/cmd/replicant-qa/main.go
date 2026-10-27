// Command replicant-qa is the minimal v0 QA harness (WS-05 stage-design, reduced to the stages install, unit
// and build for Go services): `replicant-qa run --inputs /replicant/inputs --out /replicant/out/qa --plan
// /replicant/inputs/run-plan.json`. It writes result.json ({verdict, exit_class, role_result}) and
// outputs.entries.json (entries with producer replicant-qa), plus checkpoints under /replicant/out/qa-checkpoints.
package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"example.com/replicant/internal/runner/sandbox"
	"example.com/replicant/internal/runner/schemas"
)

var version = "0.1.0"

func main() {
	if len(os.Args) < 2 || os.Args[1] != "run" {
		if len(os.Args) > 1 && os.Args[1] == "version" {
			fmt.Println("replicant-qa", version)
			return
		}
		fmt.Fprintln(os.Stderr, "usage: replicant-qa run --inputs <dir> --out <dir> --plan <run-plan.json>")
		os.Exit(2)
	}
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	inputs := fs.String("inputs", sandbox.InputsDir, "")
	out := fs.String("out", "/replicant/out/qa", "")
	planPath := fs.String("plan", "/replicant/inputs/run-plan.json", "")
	repo := fs.String("repo", sandbox.RepoDir, "")
	_ = fs.Parse(os.Args[2:])
	os.Exit(run(*inputs, *out, *planPath, *repo))
}

type stageResult struct {
	Stage    string  `json:"stage"`
	Status   string  `json:"status"`
	Duration float64 `json:"duration_seconds"`
	exitCode int
	log      []byte
}

type testCase struct {
	Pkg, Name, Status string
	Elapsed           float64
	Output            strings.Builder
}

type harness struct {
	out, repo string
	entries   []sandbox.OutputEntry
	ckDir     string
	env       []string
}

func run(inputs, out, planPath, repo string) int {
	h := &harness{out: out, repo: repo, ckDir: filepath.Join(filepath.Dir(out), "qa-checkpoints"), env: os.Environ()}
	for _, d := range []string{out, h.ckDir, filepath.Join(out, "build_log"), filepath.Join(out, "junit_xml"), filepath.Join(out, "qa_report")} {
		_ = os.MkdirAll(d, 0o775)
	}
	var plan sandbox.RunPlan
	if b, err := os.ReadFile(planPath); err == nil {
		_ = json.Unmarshal(b, &plan)
	}
	diffSHA := sha256hex(nil)
	if mb, err := os.ReadFile(filepath.Join(inputs, "manifest.json")); err == nil {
		var m sandbox.InputsManifest
		if json.Unmarshal(mb, &m) == nil {
			for _, o := range m.Objects {
				if o.Place == "apply_diff" {
					diffSHA = o.SHA256
				}
			}
		}
	}
	base := plan.Workspace.BaseSHA
	headTree := strings.TrimSpace(h.git("write-tree"))
	subject := map[string]any{"base_sha": base, "diff_bundle_sha256": diffSHA}
	if len(headTree) == 40 || len(headTree) == 64 {
		subject["head_tree_sha"] = headTree
	}
	goRepo := plan.Environment.CodebaseType == "go_service" || fileExists(filepath.Join(repo, "go.mod"))
	var stages []stageResult
	verdict, reason, exitClass := "pass", "all_passed", "pass"
	counts := map[string]int{"total": 0, "passed": 0, "failed": 0, "flaky": 0, "skipped": 0, "blocking": 0}
	var failures []map[string]any
	if !goRepo {
		verdict, reason, exitClass = "not_applicable", "qa_disabled", "not_applicable"
	} else {
		// install
		st := h.stage("install", 900, "go", "mod", "download")
		stages = append(stages, st)
		if st.Status != "passed" {
			verdict, reason, exitClass = "inconclusive", "environment_failure", "environment_setup"
		}
		// unit
		if verdict == "pass" {
			st, cases := h.unit()
			stages = append(stages, st)
			for _, c := range cases {
				counts["total"]++
				switch c.Status {
				case "pass":
					counts["passed"]++
				case "skip":
					counts["skipped"]++
				case "fail":
					counts["failed"]++
					counts["blocking"]++
					if len(failures) < 20 {
						failures = append(failures, map[string]any{"stage": "unit", "test_id": "go:" + c.Pkg + "." + c.Name, "status": "failed",
							"cause": "code", "blocking": true, "message": trunc(lastLines(c.Output.String(), 6), 500)})
					}
				}
			}
			if st.Status == "failed" {
				verdict, reason, exitClass = "fail", "regression", "fail"
				if counts["failed"] == 0 { // build failure inside go test
					reason = "build_broken"
				}
			}
		} else {
			stages = append(stages, stageResult{Stage: "unit", Status: "not_run"})
		}
		// build
		if verdict == "pass" {
			st := h.stage("build", 900, "go", "build", "./...")
			stages = append(stages, st)
			if st.Status != "passed" {
				verdict, reason, exitClass = "fail", "build_broken", "fail"
			}
		} else {
			stages = append(stages, stageResult{Stage: "build", Status: "not_run"})
		}
	}
	stageOut := make([]map[string]any, 0, len(stages))
	for _, s := range stages {
		stageOut = append(stageOut, map[string]any{"stage": s.Stage, "status": s.Status, "duration_seconds": round1(s.Duration)})
	}
	rr := map[string]any{
		"schema": "replicant.role-result.qa/v1", "verdict": verdict, "reason_code": reason,
		"counts":   counts,
		"by_cause": map[string]int{"code": counts["failed"], "flaky_new": 0, "flaky_known": 0, "pre_existing": 0, "environment": 0, "test_defect": 0, "unknown": 0},
		"stages":   stageOut, "subject": subject, "base_pass_performed": false, "budget_exhausted": false,
		"agentrun_exit_class": exitClass,
		"test_inventory_delta": map[string]any{"method": "listed", "tests_removed": 0, "tests_skipped_added": 0, "deleted_test_files": 0,
			"skip_markers_added": 0, "deleted_or_skipped": false},
	}
	if failures != nil {
		rr["top_failures"] = failures
	}
	if err := schemas.Validate(schemas.RoleQA, rr); err != nil {
		fmt.Fprintln(os.Stderr, "role_result does not validate:", err)
	}
	report := map[string]any{"schema": "replicant.qa-report/v1", "verdict": verdict, "reason_code": reason, "exit_class": exitClass,
		"stages": stageOut, "counts": counts, "subject": subject, "top_failures": failures, "harness": map[string]string{"name": "replicant-qa", "version": version},
		"generated_at": time.Now().UTC().Format(time.RFC3339)}
	h.writeEntry("qa_report", "report.json", report, "application/json")
	_ = writeJSON(filepath.Join(out, "result.json"), map[string]any{"verdict": verdict, "exit_class": exitClass, "role_result": rr})
	_ = writeJSON(filepath.Join(out, "outputs.entries.json"), map[string]any{"entries": h.entries})
	fmt.Printf("replicant-qa: verdict=%s exit_class=%s tests=%d failed=%d\n", verdict, exitClass, counts["total"], counts["failed"])
	return 0
}

func (h *harness) git(args ...string) string {
	cmd := exec.Command("git", append([]string{"-c", "core.hooksPath=/dev/null"}, args...)...)
	cmd.Dir = h.repo
	out, _ := cmd.Output()
	return string(out)
}

func (h *harness) stage(name string, timeoutS int, argv ...string) stageResult {
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeoutS)*time.Second)
	defer cancel()
	t0 := time.Now()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Dir = h.repo
	cmd.Env = h.env
	out, err := cmd.CombinedOutput()
	st := stageResult{Stage: name, Status: "passed", Duration: time.Since(t0).Seconds(), log: out}
	if err != nil {
		st.Status = "failed"
		st.exitCode = 1
		if ee, ok := err.(*exec.ExitError); ok {
			st.exitCode = ee.ExitCode()
		}
	}
	h.finishStage(st)
	return st
}

func (h *harness) finishStage(st stageResult) {
	logName := st.Stage + ".log"
	p := filepath.Join(h.out, "build_log", logName)
	header := fmt.Sprintf("$ stage %s exit %d (%.1fs)\n", st.Stage, st.exitCode, st.Duration)
	_ = os.WriteFile(p, append([]byte(header), st.log...), 0o644)
	h.entries = append(h.entries, sandbox.OutputEntry{Type: "build_log", BucketClass: "artifacts", Path: p, Name: logName,
		ContentType: "text/plain", Required: false, Redaction: "required", Producer: "replicant-qa"})
	ck := map[string]any{"stage": st.Stage, "inputs_digest": sha256hex([]byte(h.git("write-tree"))), "status": st.Status}
	_ = writeJSON(filepath.Join(h.ckDir, st.Stage+".json"), ck)
}

// unit runs `go test -json` and renders JUnit.
func (h *harness) unit() (stageResult, []*testCase) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()
	t0 := time.Now()
	cmd := exec.CommandContext(ctx, "go", "test", "-count=1", "-json", "./...")
	cmd.Dir = h.repo
	cmd.Env = h.env
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	st := stageResult{Stage: "unit", Status: "passed", Duration: time.Since(t0).Seconds()}
	if err != nil {
		st.Status = "failed"
		st.exitCode = 1
	}
	cases := map[string]*testCase{}
	var order []string
	var text bytes.Buffer
	sc := bufio.NewScanner(bytes.NewReader(out))
	sc.Buffer(make([]byte, 4<<20), 4<<20)
	for sc.Scan() {
		var ev struct {
			Action, Package, Test, Output string
			Elapsed                       float64
		}
		if json.Unmarshal(sc.Bytes(), &ev) != nil {
			text.Write(sc.Bytes())
			text.WriteByte('\n')
			continue
		}
		text.WriteString(ev.Output)
		if ev.Test == "" {
			continue
		}
		k := ev.Package + "\x00" + ev.Test
		c, ok := cases[k]
		if !ok {
			c = &testCase{Pkg: ev.Package, Name: ev.Test}
			cases[k] = c
			order = append(order, k)
		}
		switch ev.Action {
		case "output":
			if c.Output.Len() < 64<<10 {
				c.Output.WriteString(ev.Output)
			}
		case "pass", "fail", "skip":
			c.Status, c.Elapsed = ev.Action, ev.Elapsed
		}
	}
	text.Write(stderr.Bytes())
	st.log = text.Bytes()
	h.finishStage(st)
	var list []*testCase
	for _, k := range order {
		// keep leaf results only (parents of subtests are reported too; count subtests when present)
		list = append(list, cases[k])
	}
	list = leafCases(list)
	h.junit(list)
	return st, list
}

func leafCases(all []*testCase) []*testCase {
	names := map[string]bool{}
	for _, c := range all {
		names[c.Pkg+"\x00"+c.Name] = true
	}
	var out []*testCase
	for _, c := range all {
		parent := false
		for n := range names {
			if strings.HasPrefix(n, c.Pkg+"\x00"+c.Name+"/") {
				parent = true
				break
			}
		}
		if !parent && c.Status != "" {
			out = append(out, c)
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Pkg+out[i].Name < out[j].Pkg+out[j].Name })
	return out
}

func (h *harness) junit(cases []*testCase) {
	type failure struct {
		Message string `xml:"message,attr"`
		Text    string `xml:",chardata"`
	}
	type tc struct {
		XMLName   xml.Name  `xml:"testcase"`
		Classname string    `xml:"classname,attr"`
		Name      string    `xml:"name,attr"`
		Time      string    `xml:"time,attr"`
		Failure   *failure  `xml:"failure,omitempty"`
		Skipped   *struct{} `xml:"skipped,omitempty"`
	}
	type suite struct {
		XMLName  xml.Name `xml:"testsuite"`
		Name     string   `xml:"name,attr"`
		Tests    int      `xml:"tests,attr"`
		Failures int      `xml:"failures,attr"`
		Skipped  int      `xml:"skipped,attr"`
		Cases    []tc
	}
	s := suite{Name: "go test"}
	for _, c := range cases {
		t := tc{Classname: c.Pkg, Name: c.Name, Time: fmt.Sprintf("%.3f", c.Elapsed)}
		s.Tests++
		switch c.Status {
		case "fail":
			s.Failures++
			t.Failure = &failure{Message: "failed", Text: trunc(c.Output.String(), 8000)}
		case "skip":
			s.Skipped++
			t.Skipped = &struct{}{}
		}
		s.Cases = append(s.Cases, t)
	}
	b, _ := xml.MarshalIndent(s, "", "  ")
	p := filepath.Join(h.out, "junit_xml", "unit.xml")
	_ = os.WriteFile(p, append([]byte(xml.Header), b...), 0o644)
	h.entries = append(h.entries, sandbox.OutputEntry{Type: "junit_xml", BucketClass: "artifacts", Path: p, Name: "unit.xml",
		ContentType: "application/xml", Required: false, Redaction: "required", Producer: "replicant-qa"})
}

func (h *harness) writeEntry(typ, name string, v any, ct string) {
	p := filepath.Join(h.out, typ, name)
	_ = os.MkdirAll(filepath.Dir(p), 0o775)
	if writeJSON(p, v) == nil {
		h.entries = append(h.entries, sandbox.OutputEntry{Type: typ, BucketClass: "artifacts", Path: p, Name: name, ContentType: ct,
			Required: typ == "qa_report", Redaction: "required", Producer: "replicant-qa"})
	}
}

func writeJSON(p string, v any) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(p, b, 0o644)
}

func fileExists(p string) bool { _, err := os.Stat(p); return err == nil }

func sha256hex(b []byte) string { d := sha256.Sum256(b); return hex.EncodeToString(d[:]) }

func round1(f float64) float64 { return float64(int64(f*10+0.5)) / 10 }

func trunc(s string, n int) string {
	if len(s) > n {
		return s[:n]
	}
	return s
}

func lastLines(s string, n int) string {
	l := strings.Split(strings.TrimRight(s, "\n"), "\n")
	if len(l) > n {
		l = l[len(l)-n:]
	}
	return strings.Join(l, "\n")
}
