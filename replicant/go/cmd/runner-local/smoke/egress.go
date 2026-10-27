package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/s3x"
)

// caseEgress (v0 only): SEC-EGRESS-01..08 (test-strategy.md §8.1) on runner-local with the real sandbox network
// (replicant-sbx: sbx-dns, sbx-edge, egress proxy, mirrors). 01-07 run inside the agent container of a real run
// (script runner-contract-egress/egress_probe: Claude Code's Bash tool executes tests/security/egress/egress-probe.sh,
// whose output becomes the run's handoff); 08 uses the run's vended S3 credentials (RC-SBX-04 matrix).
func caseEgress(ctx context.Context, e *env) (string, error) {
	// A second run's inputs object, to be read with the probe run's credentials (must be denied).
	otherRun := ids.ULID("run")
	otherKey := fmt.Sprintf("tenants/payments/jobs/%s/runs/%s/inputs/secret.json", ids.ULID("job"), otherRun)
	if _, err := e.s3.Put(ctx, e.bucket("artifacts"), otherKey, []byte(`{"secret":"other run"}`), s3x.PutOptions{ContentType: "application/json"}); err != nil {
		return "", fmt.Errorf("stage other run object: %v", err)
	}
	st, err := e.stage(ctx, runOpts{scriptSet: "runner-contract-egress", scriptKey: "egress_probe", active: 900, finalize: 60})
	if err != nil {
		return "", err
	}
	if c, out := e.create(st); c != 201 {
		return "", fmt.Errorf("create %d %v", c, out)
	}
	if err := e.waitPhase(st, "Running", 5*time.Minute); err != nil {
		return "", err
	}
	e.credsMatrix(ctx, st, otherKey)
	final, phases, err := e.wait(st, 15*time.Minute)
	if err != nil {
		return "", err
	}
	if final["phase"] != "Succeeded" {
		e.dumpRun(ctx, st)
		return "", fmt.Errorf("probe run %v %v via %v", final["phase"], final["failure"], phases)
	}
	ab, ak, _ := s3x.ParseURI(st.prefixes.Artifacts)
	rb, _, err := e.s3.Get(ctx, ab, ak+"run_result/result.json")
	if err != nil {
		return "", err
	}
	var rr struct {
		Outputs map[string]string `json:"outputs"`
	}
	_ = json.Unmarshal(rb, &rr)
	hk := rr.Outputs["handoff"]
	hb, _, err := e.s3.Get(ctx, ab, hk)
	if err != nil {
		return "", fmt.Errorf("handoff %q: %v", hk, err)
	}
	text := string(hb)
	var ho struct {
		Narrative string `json:"narrative_markdown"`
	}
	if json.Unmarshal(hb, &ho) == nil && ho.Narrative != "" { // replicant.handoff/v1 wraps handoff.md
		text = ho.Narrative
	}
	seen, failed := map[string]bool{}, []string{}
	for _, line := range strings.Split(text, "\n") {
		f := strings.SplitN(strings.TrimSpace(line), " ", 3)
		if len(f) < 2 || !strings.HasPrefix(f[0], "SEC-EGRESS-0") {
			continue
		}
		detail := ""
		if len(f) == 3 {
			detail = f[2]
		}
		seen[f[0]] = true
		if f[1] != "PASS" {
			failed = append(failed, f[0])
		}
		e.record(f[0], "inside the sandbox (agent container)", f[1] == "PASS", detail)
	}
	missing := []string{}
	for i := 1; i <= 7; i++ {
		id := fmt.Sprintf("SEC-EGRESS-0%d", i)
		if !seen[id] {
			missing = append(missing, id)
			e.record(id, "inside the sandbox (agent container)", false, "no result line in the probe output")
		}
	}
	if len(missing) > 0 {
		fmt.Printf("  probe output:\n%s\n", indent(text))
	}
	return fmt.Sprintf("probe run %s via %s; handoff %s", st.runID, strings.Join(phases, " → "), hk), expect(len(missing) == 0 && len(failed) == 0, "missing %v, failed %v", missing, failed)
}

// credsMatrix: RC-SBX-04 and SEC-EGRESS-08 with the credentials the runner vends to this run's sandbox
// (GET /sandbox/v1/credentials/aws with the run's runner-sandbox token, as agentd does).
func (e *env) credsMatrix(ctx context.Context, st *staged, otherKey string) {
	tok := ""
	for i := 0; i < 120 && tok == ""; i++ {
		b, err := os.ReadFile(filepath.Join(e.stateDir, "runs", st.runID, "tokens", api.AudSandbox))
		if err == nil {
			tok = strings.TrimSpace(string(b))
			break
		}
		time.Sleep(500 * time.Millisecond)
	}
	if tok == "" {
		e.record("SEC-EGRESS-08", "vended credentials", false, "no runner-sandbox token in the runner state")
		return
	}
	req, _ := http.NewRequestWithContext(ctx, "GET", e.runnerSbx+"/sandbox/v1/credentials/aws", nil)
	req.Header.Set("Authorization", "Bearer "+tok)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.record("SEC-EGRESS-08", "vended credentials", false, err.Error())
		return
	}
	var cr api.AwsContainerCredentials
	_ = json.NewDecoder(resp.Body).Decode(&cr)
	resp.Body.Close()
	if resp.StatusCode != 200 || cr.AccessKeyId == "" {
		e.record("SEC-EGRESS-08", "vended credentials", false, fmt.Sprintf("credentials endpoint %d", resp.StatusCode))
		return
	}
	c := s3x.New(s3x.Config{Endpoint: os.Getenv("S3_ENDPOINT"), Region: "us-east-1", AccessKey: cr.AccessKeyId, SecretKey: cr.SecretAccessKey, SessionToken: cr.Token})
	ab, ak, _ := s3x.ParseURI(st.prefixes.Artifacts)
	try := func(op string, f func() error) string {
		if err := f(); err != nil {
			if strings.Contains(err.Error(), "AccessDenied") || strings.Contains(err.Error(), "403") {
				return op + "=denied"
			}
			return op + "=error(" + strings.SplitN(err.Error(), "\n", 2)[0] + ")"
		}
		return op + "=allowed"
	}
	r := []string{
		try("put_own_artifacts", func() error {
			_, err := c.Put(ctx, ab, ak+"artifacts/creds-probe.txt", []byte("ok"), s3x.PutOptions{})
			return err
		}),
		try("put_own_inputs", func() error {
			_, err := c.Put(ctx, ab, ak+"inputs/creds-probe.txt", []byte("x"), s3x.PutOptions{})
			return err
		}),
		try("get_own_inputs", func() error { _, _, err := c.Get(ctx, ab, ak+"inputs/manifest.json"); return err }),
		try("get_other_run", func() error { _, _, err := c.Get(ctx, e.bucket("artifacts"), otherKey); return err }),
		try("put_other_run", func() error {
			_, err := c.Put(ctx, e.bucket("artifacts"), strings.Replace(otherKey, "inputs/secret.json", "artifacts/x", 1), []byte("x"), s3x.PutOptions{})
			return err
		}),
		try("get_audit", func() error { _, err := c.List(ctx, e.bucket("audit"), "", 1); return err }),
	}
	want := []string{"put_own_artifacts=allowed", "put_own_inputs=denied", "get_own_inputs=allowed", "get_other_run=denied", "put_other_run=denied", "get_audit=denied"}
	ok := strings.Join(r, " ") == strings.Join(want, " ")
	e.record("RC-SBX-04", "vended credentials: the C-RUNNER session-policy matrix", ok, strings.Join(r, " "))
	e.record("SEC-EGRESS-08", "another run's S3 prefix with the run's vended credentials → 403", r[3] == "get_other_run=denied" && r[4] == "put_other_run=denied",
		fmt.Sprintf("%s %s (session token %v)", r[3], r[4], cr.Token != ""))
}
