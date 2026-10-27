package main

// M3 Runner contract cases (test-strategy.md §5.3) that RUNNER left uncovered, all against runner-local in the live
// v0 stack (SMOKE_TARGET=v0): RC-RUN-03/04/06/08/13/14/15/19/20/21/22, RC-LEASE-01, RC-MSG-01/02, RC-AUD-01,
// RC-REV-03, RC-ENV-01..04, RC-CAN-01. The driver reaches the container engine through CONTAINER_HOST for the
// cases that inject faults (stop Temporal, kill a container, pause agentd).

import (
	"context"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/base32"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/engine"
	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/s3x"
)

func m3Cases() []tcase {
	return []tcase{
		{"RC-CAN-01", "cancel while Pending (test hook holds the run before the sandbox starts)", false, true, caseCancelPending},
		{"RC-RUN-13", "scenario invalid_result", false, true, func(ctx context.Context, e *env) (string, error) {
			return e.failCase(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "invalid_result"}, "AgentResultInvalid", true, 6*time.Minute)
		}},
		{"RC-RUN-14", "scenario disk_exceeded", false, true, caseDisk},
		{"RC-RUN-15", "scenario oom", false, true, func(ctx context.Context, e *env) (string, error) {
			return e.failCase(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "oom"}, "OOMKilled", false, 8*time.Minute)
		}},
		{"RC-RUN-21", "agent definition blob deleted before admission", false, true, caseDefinitionGone},
		{"RC-RUN-22", "environment setup step exits 1", false, true, caseEnvSetup},
		{"RC-ENV-01..04+RUN-08", "custody, trace context, proxy/mirror env, deterministic mode, repo at base_sha", false, true, caseProbeEnv},
		{"RC-RUN-04", "probe workflow already closed", false, true, caseWorkflowClosed},
		{"RC-MSG-01/02", "steering delivered live; same message_id again", false, true, caseSteering},
		{"RC-AUD-01+REV-03+RUN-19", "cross-audience replay; verifiers honor the feed; security cancel", false, true, caseAudRev},
		{"RC-RUN-20", "security violation (canary credential reported by sbx-edge)", false, true, caseCanary},
		{"RC-RUN-06", "streaming evidence up to a container kill", false, true, caseKillEvidence},
		{"RC-LEASE-01", "heartbeats blocked 150 s → NodeLost; heartbeat after terminal → continue:false", false, true, caseLease},
		{"RC-RUN-03", "Temporal unavailable 60 s: Signals delivered after recovery", false, true, caseNotifierOutage},
	}
}

func init() {
	for _, c := range m3Cases() {
		v0Only[c.id] = true
	}
}

func (e *env) engine() (*engine.Client, error) {
	h := os.Getenv("CONTAINER_HOST")
	if h == "" {
		return nil, fmt.Errorf("CONTAINER_HOST not set (fault-injection cases need the engine)")
	}
	return engine.New(h)
}

func (e *env) readToken(runID, aud string, wait time.Duration) string {
	deadline := time.Now().Add(wait)
	for time.Now().Before(deadline) {
		if b, err := os.ReadFile(filepath.Join(e.stateDir, "runs", runID, "tokens", aud)); err == nil {
			return strings.TrimSpace(string(b))
		}
		time.Sleep(300 * time.Millisecond)
	}
	return ""
}

func httpStatus(method, url, token string, hdr map[string]string) int {
	req, _ := http.NewRequest(method, url, nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	for k, v := range hdr {
		if k == "Host" {
			req.Host = v
			continue
		}
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return 0
	}
	resp.Body.Close()
	return resp.StatusCode
}

func caseCancelPending(ctx context.Context, e *env) (string, error) {
	st, err := e.stage(ctx, runOpts{lai: "holdpending.c0.n1.run"})
	if err != nil {
		return "", err
	}
	if c, out := e.create(st); c != 201 {
		return "", fmt.Errorf("create %d %v", c, out)
	}
	time.Sleep(4 * time.Second)
	_, _, out := e.do("GET", "/v1/runs/"+st.runID, e.workerToken, "", nil)
	if out["phase"] != "Pending" {
		return "", fmt.Errorf("not held in Pending: %v", out["phase"])
	}
	c, _, _ := e.do("POST", "/v1/runs/"+st.runID+":cancel", e.workerToken, "", map[string]any{"reason": "job_cancelled"})
	final, phases, err := e.wait(st, 3*time.Minute)
	if err != nil {
		return "", err
	}
	got := failureReason(final)
	return fmt.Sprintf("cancel %d → %v/%s via %s", c, final["phase"], got, strings.Join(phases, " → ")),
		expect(c == 202 && final["phase"] == "Cancelled" && got == "CancelledByRequest" && !contains(phases, "Running"), "unexpected")
}

func caseDisk(ctx context.Context, e *env) (string, error) {
	detail, err := e.failCase(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "disk_exceeded"}, "WorkspaceDiskExceeded", false, 10*time.Minute)
	if err != nil {
		return detail, err
	}
	return detail, expect(strings.Contains(detail, "Finalizing"), "not via Finalizing")
}

func caseDefinitionGone(ctx context.Context, e *env) (string, error) {
	// A fresh agent definition (unique description) whose blob is published, referenced, then deleted.
	doc := map[string]any{"apiVersion": "replicant.example.com/v1", "kind": "AgentDefinition",
		"metadata": map[string]any{"name": "implementer-claude", "scope": map[string]any{"level": "global"}, "owners": []string{"replicant-platform"},
			"description": "RC-RUN-21 " + ids.ULID("x")}, "spec": agentSpec()}
	body, dig := jcsDigest(doc)
	key := "blobs/sha256/" + strings.TrimPrefix(dig, "sha256:") + ".json"
	if _, err := e.s3.Put(ctx, e.bucket("registry"), key, body, s3x.PutOptions{ContentType: "application/json"}); err != nil {
		return "", err
	}
	st, err := e.stage(ctx, runOpts{})
	if err != nil {
		return "", err
	}
	st.spec["agent_definition"] = api.DefinitionRef{Kind: "agent", Name: "implementer-claude", Version: 1, Digest: dig}
	e.resign(st, runOpts{jobID: st.jobID})
	if _, err := e.s3.S3.DeleteObject(ctx, &s3.DeleteObjectInput{Bucket: aws.String(e.bucket("registry")), Key: aws.String(key)}); err != nil {
		return "", err
	}
	c, out := e.create(st)
	if c == 422 {
		return fmt.Sprint("422 ", code(out)), expect(code(out) == "definition_not_resolvable", "422 %v", out)
	}
	if c != 201 {
		return "", fmt.Errorf("create %d %v", c, out)
	}
	final, _, err := e.wait(st, 4*time.Minute)
	if err != nil {
		return "", err
	}
	return failureReason(final), expect(failureReason(final) == "DefinitionUnavailable", "got %v", final["failure"])
}

func caseEnvSetup(ctx context.Context, e *env) (string, error) {
	spec := envSpec("localhost/replicant/env-go-service:dev")
	spec["bootstrap"] = map[string]any{"forRoles": []string{"implementer", "repair", "reviewer", "qa"},
		"steps": []map[string]any{{"name": "broken-setup", "run": "echo setup failing >&2; exit 1", "timeoutSeconds": 60}}}
	doc := map[string]any{"apiVersion": "replicant.example.com/v1", "kind": "EnvironmentDefinition",
		"metadata": map[string]any{"name": "go-service-broken-setup", "scope": map[string]any{"level": "global"}, "owners": []string{"replicant-platform"}},
		"spec":     spec}
	body, dig := jcsDigest(doc)
	if err := e.s3.PutWriteOnceIdempotent(ctx, e.bucket("registry"), "blobs/sha256/"+strings.TrimPrefix(dig, "sha256:")+".json", body,
		s3x.PutOptions{ContentType: "application/json"}); err != nil {
		return "", err
	}
	if err := e.addToBundle(ctx, "environment/go-service-broken-setup@1", dig); err != nil {
		return "", err
	}
	ref := api.DefinitionRef{Kind: "environment", Name: "go-service-broken-setup", Version: 1, Digest: dig}
	saved := e.envSpec
	e.envSpec = spec // run plan bootstrap steps come from the environment
	defer func() { e.envSpec = saved }()
	// EnvironmentSetupFailed is retryable in C-RUNNER's reason table. Bootstrap steps run in prepare (an init
	// container), so the run fails from Provisioning, not via Finalizing (DEVIATIONS PLATFORM-D19).
	return e.failCase(ctx, runOpts{envRef: &ref}, "EnvironmentSetupFailed", true, 6*time.Minute)
}

// addToBundle republishes the driver's bundle with one more environment (new rb).
func (e *env) addToBundle(ctx context.Context, ref, dig string) error {
	spec := map[string]any{
		"agents":       []map[string]string{{"ref": "agent/implementer-claude@1", "digest": e.agentRef.Digest}},
		"environments": []map[string]string{{"ref": "environment/go-service@1", "digest": e.envRef.Digest}, {"ref": "environment/go-service-missing-image@1", "digest": e.badEnvRef.Digest}, {"ref": ref, "digest": dig}},
		"policies":     []any{},
	}
	bb, bdig := jcsDigest(spec)
	sum := sha256.Sum256(bb)
	rb := "rb_" + ids.B32_130(sum[:])
	if err := e.s3.PutWriteOnceIdempotent(ctx, e.bucket("registry"), "blobs/sha256/"+strings.TrimPrefix(bdig, "sha256:")+".json", bb, s3x.PutOptions{ContentType: "application/json"}); err != nil {
		return err
	}
	ptr, _ := json.Marshal(map[string]string{"release_bundle_id": rb, "digest": bdig})
	if err := e.s3.PutWriteOnceIdempotent(ctx, e.bucket("registry"), "bundles/"+rb+".json", ptr, s3x.PutOptions{ContentType: "application/json"}); err != nil {
		return err
	}
	e.rb = rb
	return nil
}

func uuid5URL(name string) string {
	ns := []byte{0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1, 0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30, 0xc8}
	h := sha1.New()
	h.Write(ns)
	h.Write([]byte(name))
	u := h.Sum(nil)[:16]
	u[6] = (u[6] & 0x0f) | 0x50
	u[8] = (u[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", u[0:4], u[4:6], u[6:8], u[8:10], u[10:16])
}

func (e *env) handoffText(ctx context.Context, st *staged) (string, error) {
	ab, ak, _ := s3x.ParseURI(st.prefixes.Artifacts)
	rb, _, err := e.s3.Get(ctx, ab, ak+"run_result/result.json")
	if err != nil {
		return "", err
	}
	var rr struct {
		Outputs map[string]string `json:"outputs"`
	}
	_ = json.Unmarshal(rb, &rr)
	hb, _, err := e.s3.Get(ctx, ab, rr.Outputs["handoff"])
	if err != nil {
		return "", err
	}
	var ho struct {
		Narrative string `json:"narrative_markdown"`
	}
	if json.Unmarshal(hb, &ho) == nil && ho.Narrative != "" {
		return ho.Narrative, nil
	}
	return string(hb), nil
}

func kv(text string) map[string]string {
	m := map[string]string{}
	for _, l := range strings.Split(text, "\n") {
		if k, v, ok := strings.Cut(strings.TrimSpace(l), "="); ok && k != "" && !strings.Contains(k, " ") {
			if k == "PATH_PRESENT" || k == "PATH_ABSENT" {
				m[k] += v + ","
				continue
			}
			m[k] = v
		}
	}
	return m
}

func caseProbeEnv(ctx context.Context, e *env) (string, error) {
	st, err := e.stage(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "probe_env"})
	if err != nil {
		return "", err
	}
	if c, out := e.create(st); c != 201 {
		return "", fmt.Errorf("create %d %v", c, out)
	}
	final, _, err := e.wait(st, 8*time.Minute)
	if err != nil {
		return "", err
	}
	if final["phase"] != "Succeeded" {
		return "", fmt.Errorf("probe run %v %v", final["phase"], final["failure"])
	}
	text, err := e.handoffText(ctx, st)
	if err != nil {
		return "", err
	}
	m := kv(text)
	names := "," + m["ENV_NAMES"]
	// RC-ENV-01 custody
	bad := []string{}
	for _, n := range []string{"REPLICANT_RUN_TOKEN_DIR", "REPLICANT_RUN_TOKEN_SEED_DIR", "GITHUB_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
		"AWS_SESSION_TOKEN", "TEMPORAL_ADDRESS", "REPLICANT_DB_DSN", "REPLICANT_DB_PASSWORD_FILE"} {
		if strings.Contains(names, ","+n+",") {
			bad = append(bad, n)
		}
	}
	for _, n := range []string{"REPLICANT_TENANT_ID", "REPLICANT_JOB_ID", "REPLICANT_RUN_ID", "REPLICANT_ROLE", "TRACEPARENT"} {
		if !strings.Contains(names, ","+n+",") {
			bad = append(bad, "missing:"+n)
		}
	}
	if strings.Contains(m["PATH_PRESENT"], "/run/replicant/token") {
		bad = append(bad, "token dir present: "+m["PATH_PRESENT"])
	}
	e.record("RC-ENV-01", "custody: no token dirs, no credentials, C-IDS §7 variables present", len(bad) == 0,
		fmt.Sprintf("violations=%v absent=%s", bad, strings.TrimSuffix(m["PATH_ABSENT"], ",")))
	// RC-ENV-02 trace
	tp := strings.Split(m["TRACEPARENT"], "-")
	wantTrace := ids.JobTraceID(st.jobID)
	ra := m["OTEL_RESOURCE_ATTRIBUTES"]
	okRA := true
	for _, k := range []string{"service.name=replicant-sandbox", "replicant.env=local", "replicant.tenant_id=payments", "replicant.job_id=" + st.jobID,
		"replicant.run_id=" + st.runID, "replicant.role=implementer", "replicant.release_bundle_id=" + e.rb} {
		if !strings.Contains(ra, k) {
			okRA = false
		}
	}
	e.record("RC-ENV-02", "TRACEPARENT carries the RunSpec trace; OTEL_RESOURCE_ATTRIBUTES per C-IDS §5",
		len(tp) == 4 && tp[1] == wantTrace && okRA, fmt.Sprintf("traceparent=%s resource=%s", m["TRACEPARENT"], ra))
	// RC-ENV-03 proxy and mirror passthrough (runner-local.yaml sandbox.env)
	want := map[string]string{"HTTPS_PROXY": "http://egress.sbx.local:3128", "HTTP_PROXY": "http://egress.sbx.local:3128",
		"NPM_CONFIG_REGISTRY": "http://npm.sbx.local:4873/", "GOPROXY": httpx_env("EXPECT_GOPROXY", "https://proxy.golang.org"), "PIP_INDEX_URL": "http://pypi.sbx.local:5000/index/"}
	okEnv := strings.Contains(m["NO_PROXY"], "127.0.0.1") && strings.Contains(m["NO_PROXY"], "localhost")
	for k, v := range want {
		if m[k] != v {
			okEnv = false
		}
	}
	e.record("RC-ENV-03", "proxy and mirror variables equal runner-local.yaml sandbox.env; NO_PROXY has 127.0.0.1,localhost", okEnv,
		fmt.Sprintf("HTTPS_PROXY=%s GOPROXY=%s NPM=%s PIP=%s NO_PROXY=%s", m["HTTPS_PROXY"], m["GOPROXY"], m["NPM_CONFIG_REGISTRY"], m["PIP_INDEX_URL"], m["NO_PROXY"]))
	// RC-ENV-04 deterministic mode
	dates := strings.Split(m["GIT_DATES"], "|")
	wantSess := uuid5URL("replicant:" + st.runID)
	okDet := len(dates) == 2 && strings.HasPrefix(dates[0], "2026-10-24T00:00:00") && strings.HasPrefix(dates[1], "2026-10-24T00:00:00") &&
		m["CLI_SESSION"] == wantSess
	e.record("RC-ENV-04", "deterministic: commit dates = the job's received_at; session id = uuid5(URL, replicant:<run_id>)", okDet,
		fmt.Sprintf("dates=%s session=%s want=%s", m["GIT_DATES"], m["CLI_SESSION"], wantSess))
	// RC-RUN-08 repo at base_sha
	e.record("RC-RUN-08", "workspace HEAD equals base_sha (fetched through agentd → git.sbx.local → repo-mirror)", m["HEAD"] == e.gitea.baseSHA,
		fmt.Sprintf("HEAD=%s base_sha=%s", m["HEAD"], e.gitea.baseSHA))
	if len(bad) > 0 || !okRA || !okEnv || !okDet || m["HEAD"] != e.gitea.baseSHA {
		fmt.Printf("  probe output:\n%s\n", indent(text))
		return "", fmt.Errorf("one or more RC-ENV / RC-RUN-08 checks failed")
	}
	return "RC-ENV-01..04 and RC-RUN-08 pass", nil
}

func caseWorkflowClosed(ctx context.Context, e *env) (string, error) {
	if e.probe == nil {
		return "", fmt.Errorf("no Temporal probe")
	}
	st, err := e.stage(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "no_result"})
	if err != nil {
		return "", err
	}
	if err := e.probe.begin(ctx, st.wf); err != nil {
		return "", err
	}
	if c, out := e.create(st); c != 201 {
		return "", fmt.Errorf("create %d %v", c, out)
	}
	time.Sleep(2 * time.Second)
	e.probe.stop(ctx, st.wf) // the callback workflow closes before the run ends
	final, _, err := e.wait(st, 6*time.Minute)
	if err != nil {
		return "", err
	}
	var cd string
	for i := 0; i < 30; i++ {
		_, _, final = e.do("GET", "/v1/runs/"+st.runID, e.workerToken, "", nil)
		if cd = condStatus(final, "CallbackDelivered"); strings.Contains(cd, "WorkflowClosed") {
			break
		}
		time.Sleep(2 * time.Second)
	}
	return "CallbackDelivered=" + cd, expect(cd == "True/WorkflowClosed", "got %s", cd)
}

func caseSteering(ctx context.Context, e *env) (string, error) {
	st, err := e.stage(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "long_running", steering: true, finalize: 45})
	if err != nil {
		return "", err
	}
	if c, out := e.create(st); c != 201 {
		return "", fmt.Errorf("create %d %v", c, out)
	}
	if err := e.waitPhase(st, "Running", 5*time.Minute); err != nil {
		return "", err
	}
	time.Sleep(8 * time.Second)
	mid := ids.ULID("msg")
	msg := map[string]any{"message_id": mid, "text": "Please also add a test for full refunds.", "author": "usr_" + ids.Body(ids.ULID("x")),
		"sent_at": time.Now().UTC().Format(time.RFC3339)}
	key := `"ik1:` + st.wf + `:steer.c0.n1.send"`
	c1, _, o1 := e.do("POST", "/v1/runs/"+st.runID+"/messages", e.workerToken, key, msg)
	state := fmt.Sprint(o1["state"])
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) && state != "delivered" && !strings.HasPrefix(state, "delivered") {
		time.Sleep(2 * time.Second)
		_, _, o := e.do("GET", "/v1/runs/"+st.runID+"/messages/"+mid, e.workerToken, "", nil)
		state = fmt.Sprint(o["state"])
	}
	c2, _, o2 := e.do("POST", "/v1/runs/"+st.runID+"/messages", e.workerToken, key, msg)
	ab, ak, _ := s3x.ParseURI(st.prefixes.Artifacts)
	keys, _ := e.s3.List(ctx, ab, ak+"inbox/", 0)
	nObj := 0
	for _, k := range keys {
		if strings.HasSuffix(k, "-"+mid+".json") && !strings.Contains(k, "/ack-") {
			nObj++
		}
	}
	_, _, _ = e.do("POST", "/v1/runs/"+st.runID+":cancel", e.workerToken, "", map[string]any{"reason": "job_cancelled"})
	_, _, _ = e.wait(st, 3*time.Minute)
	e.record("RC-MSG-01", "steering on a live run → 202 queued, delivered within 30 s, inbox object", c1 == 202 && strings.HasPrefix(state, "delivered") && nObj == 1,
		fmt.Sprintf("%d %v → %s; inbox objects for the message: %d (%v)", c1, o1["state"], state, nObj, keys))
	e.record("RC-MSG-02", "same message_id again → 200 with current status, one inbox object", c2 == 200 && nObj == 1, fmt.Sprintf("%d %v", c2, o2["state"]))
	return state, expect(c1 == 202 && c2 == 200 && strings.HasPrefix(state, "delivered") && nObj == 1, "steering checks failed")
}

func caseAudRev(ctx context.Context, e *env) (string, error) {
	st, err := e.stage(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "long_running", finalize: 45})
	if err != nil {
		return "", err
	}
	if c, out := e.create(st); c != 201 {
		return "", fmt.Errorf("create %d %v", c, out)
	}
	if err := e.waitPhase(st, "Running", 5*time.Minute); err != nil {
		return "", err
	}
	gw, mir := e.readToken(st.runID, api.AudGateway, time.Minute), e.readToken(st.runID, api.AudMirror, time.Minute)
	mirror := httpx_env("MIRROR_URL", "http://repo-mirror:8080")
	gitPath := mirror + "/git/" + fmt.Sprint(e.gitea.id) + "/info/refs?service=git-upload-pack"
	repoHdr := map[string]string{"X-Replicant-Repo-Provider": "gitea", "X-Replicant-Repo-Host": "git.replicant.localhost:3300",
		"X-Replicant-Repo-Owner": e.gitea.owner, "X-Replicant-Repo-Name": e.gitea.name}
	// positive controls while the run is live
	posShim := httpStatus("GET", e.shim+"/v1/budget", gw, nil)
	posMir := httpStatus("GET", gitPath, mir, repoHdr)
	// RC-AUD-01: tokens presented to the wrong audience
	aud := map[string]int{
		"repo-mirror token → shim":           httpStatus("GET", e.shim+"/v1/budget", mir, nil),
		"ai-gateway token → repo-mirror":     httpStatus("GET", gitPath, gw, repoHdr),
		"ai-gateway token → runner-sandbox":  httpStatus("POST", e.runnerSbx+"/sandbox/v1/token:refresh", gw, nil),
		"repo-mirror token → runner-sandbox": httpStatus("POST", e.runnerSbx+"/sandbox/v1/token:refresh", mir, nil),
	}
	okAud := true
	for _, c := range aud {
		if c != 401 {
			okAud = false
		}
	}
	e.record("RC-AUD-01", "cross-audience replay → 401 everywhere (aud equality)", okAud && posShim == 200,
		fmt.Sprintf("%v (positive controls: shim %d, mirror %d)", aud, posShim, posMir))
	// RC-RUN-19: security cancel while Running
	c, _, _ := e.do("POST", "/v1/runs/"+st.runID+":cancel", e.workerToken, "", map[string]any{"reason": "security"})
	final, phases, err := e.wait(st, 4*time.Minute)
	if err != nil {
		return "", err
	}
	got := failureReason(final)
	t0 := time.Now()
	onFeed := false
	for time.Since(t0) < 15*time.Second && !onFeed {
		_, _, rv := e.do("GET", "/v1/revocations", e.readerToken, "", nil)
		onFeed = strings.Contains(fmt.Sprint(rv["revoked"]), st.runID)
		time.Sleep(time.Second)
	}
	// RC-REV-03: the still-unexpired tokens are rejected by the verifiers within one feed poll (10 s)
	var shimC, mirC int
	deadline := time.Now().Add(60 * time.Second)
	for time.Now().Before(deadline) {
		shimC, mirC = httpStatus("GET", e.shim+"/v1/budget", gw, nil), httpStatus("GET", gitPath, mir, repoHdr)
		if shimC == 401 && mirC == 401 {
			break
		}
		time.Sleep(2 * time.Second)
	}
	took := time.Since(t0).Round(time.Second)
	e.record("RC-RUN-19", "security cancel → Failed/SecurityViolation (C-RUNNER), run on the revocation feed, tokens rejected ≤ 60 s",
		c == 202 && final["phase"] == "Failed" && got == "SecurityViolation" && onFeed && shimC == 401 && mirC == 401,
		fmt.Sprintf("cancel %d → %v/%s via %s; feed=%v; shim %d mirror %d after %s", c, final["phase"], got, strings.Join(phases, " → "), onFeed, shimC, mirC, took))
	e.record("RC-REV-03", "verifiers honor the exec revocation feed (shim, repo-mirror) within one poll", shimC == 401 && mirC == 401 && took <= 30*time.Second,
		fmt.Sprintf("shim %d repo-mirror %d within %s of terminal", shimC, mirC, took))
	return "", expect(okAud && got == "SecurityViolation" && onFeed && shimC == 401 && mirC == 401, "checks failed")
}

func httpx_env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

// canaryKeyFor mirrors runner-local security.go canaryKey.
func canaryKeyFor(runID string) string {
	h := sha256.Sum256([]byte("replicant.canary.v1:" + runID))
	return "AKIAFC" + strings.ToUpper(base32.StdEncoding.EncodeToString(h[:]))[:14]
}

// caseCanary: RC-RUN-20 trigger path. The run's decoy credentials are presented on s3.sbx.local's internal
// report location by the edge; here the driver plays the edge's report (it sits on replicant-core like sbx-edge)
// and checks the outcome. The edge side (map + internal location) is exercised by the nginx config test below.
func caseCanary(ctx context.Context, e *env) (string, error) {
	st, err := e.stage(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "long_running", finalize: 45})
	if err != nil {
		return "", err
	}
	if c, out := e.create(st); c != 201 {
		return "", fmt.Errorf("create %d %v", c, out)
	}
	if err := e.waitPhase(st, "Running", 5*time.Minute); err != nil {
		return "", err
	}
	fromSandbox := httpStatus("POST", httpx_env("EDGE_URL", "http://sbx-edge")+"/sandbox/v1/security/canary-tripped", "",
		map[string]string{"Host": "runner-sandbox.sbx.local", "X-Replicant-Canary-Key": canaryKeyFor(st.runID)})
	rep := httpStatus("POST", e.runnerSbx+"/sandbox/v1/security/canary-tripped", "", map[string]string{
		"X-Replicant-Canary-Key": canaryKeyFor(st.runID), "X-Replicant-Source-IP": "172.31.250.200"})
	final, phases, err := e.wait(st, 4*time.Minute)
	if err != nil {
		return "", err
	}
	got := failureReason(final)
	f, _ := final["failure"].(map[string]any)
	return fmt.Sprintf("sandbox-side report path %d; edge report %d → %v/%s retryable=%v via %s", fromSandbox, rep, final["phase"], got, f["retryable"], strings.Join(phases, " → ")),
		expect(fromSandbox == 404 && rep == 403 && got == "SecurityViolation" && f["retryable"] == false, "sandbox %d edge %d reason %s", fromSandbox, rep, got)
}

func caseKillEvidence(ctx context.Context, e *env) (string, error) {
	en, err := e.engine()
	if err != nil {
		return "", err
	}
	st, err := e.stage(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "long_running", finalize: 45})
	if err != nil {
		return "", err
	}
	if c, out := e.create(st); c != 201 {
		return "", fmt.Errorf("create %d %v", c, out)
	}
	if err := e.waitPhase(st, "Running", 5*time.Minute); err != nil {
		return "", err
	}
	time.Sleep(20 * time.Second)
	killAt := time.Now()
	if err := en.Kill(ctx, "ar-"+ids.Body(st.runID)+"-agent", "KILL"); err != nil {
		return "", err
	}
	final, phases, err := e.wait(st, 4*time.Minute)
	if err != nil {
		return "", err
	}
	tb, tk, _ := s3x.ParseURI(st.prefixes.Transcripts)
	keys, _ := e.s3.List(ctx, tb, tk, 0)
	var parts []string
	var latest time.Time
	for _, k := range keys {
		if strings.Contains(k, "transcript/part-") {
			parts = append(parts, k)
			if h, err := e.s3.S3.HeadObject(ctx, &s3.HeadObjectInput{Bucket: aws.String(tb), Key: aws.String(k)}); err == nil && h.LastModified != nil && h.LastModified.After(latest) {
				latest = *h.LastModified
			}
		}
	}
	gap := killAt.Sub(latest)
	return fmt.Sprintf("%d transcript parts, last written %s before the kill; run %v/%s via %s", len(parts), gap.Round(time.Second), final["phase"], failureReason(final), strings.Join(phases, " → ")),
		expect(len(parts) > 0 && gap < 30*time.Second, "no transcript part within 30 s of the kill")
}

func caseLease(ctx context.Context, e *env) (string, error) {
	en, err := e.engine()
	if err != nil {
		return "", err
	}
	st, err := e.stage(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "long_running", active: 900, finalize: 45})
	if err != nil {
		return "", err
	}
	if c, out := e.create(st); c != 201 {
		return "", fmt.Errorf("create %d %v", c, out)
	}
	if err := e.waitPhase(st, "Running", 5*time.Minute); err != nil {
		return "", err
	}
	sbxTok := e.readToken(st.runID, api.AudSandbox, time.Minute)
	agentd := "ar-" + ids.Body(st.runID) + "-replicant-agentd"
	if err := en.Pause(ctx, agentd); err != nil {
		return "", fmt.Errorf("pause agentd: %v", err)
	}
	final, phases, err := e.wait(st, 6*time.Minute)
	_ = en.Unpause(context.Background(), agentd)
	if err != nil {
		return "", err
	}
	got := failureReason(final)
	req, _ := http.NewRequest("POST", e.runnerSbx+"/sandbox/v1/heartbeat", strings.NewReader(`{}`))
	req.Header.Set("Authorization", "Bearer "+sbxTok)
	req.Header.Set("Content-Type", "application/json")
	var hb map[string]any
	hc := 0
	if resp, err := http.DefaultClient.Do(req); err == nil {
		hc = resp.StatusCode
		_ = json.NewDecoder(resp.Body).Decode(&hb)
		resp.Body.Close()
	}
	cont := fmt.Sprint(hb["continue"])
	return fmt.Sprintf("agentd paused → %v/%s via %s; heartbeat after terminal %d continue=%s", final["phase"], got, strings.Join(phases, " → "), hc, cont),
		expect(final["phase"] == "Failed" && got == "NodeLost" && (cont == "false" || hc == 401 || hc == 409), "unexpected")
}

func caseNotifierOutage(ctx context.Context, e *env) (string, error) {
	en, err := e.engine()
	if err != nil {
		return "", err
	}
	if e.probe == nil {
		return "", fmt.Errorf("no Temporal probe")
	}
	// The run's deadline (60 s, the schema minimum) falls inside the outage, so Finalizing and Failed are signalled while Temporal is down.
	st, err := e.stage(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "long_running", active: 60, finalize: 30})
	if err != nil {
		return "", err
	}
	if err := e.probe.begin(ctx, st.wf); err != nil {
		return "", err
	}
	if c, out := e.create(st); c != 201 {
		return "", fmt.Errorf("create %d %v", c, out)
	}
	if err := e.waitPhase(st, "Running", 5*time.Minute); err != nil {
		return "", err
	}
	tmp := httpx_env("TEMPORAL_CONTAINER", "replicant_temporal_1")
	if err := en.Stop(ctx, tmp, 5); err != nil {
		return "", err
	}
	stoppedAt := time.Now()
	sawFailing := false
	for time.Since(stoppedAt) < 90*time.Second { // the spec says 60 s; 90 s so the 60 s deadline lands inside it
		_, _, o := e.do("GET", "/v1/runs/"+st.runID, e.workerToken, "", nil)
		if strings.Contains(condStatus(o, "CallbackDelivered"), "DeliveryFailing") {
			sawFailing = true
		}
		time.Sleep(3 * time.Second)
	}
	if err := en.Start(ctx, tmp); err != nil {
		return "", err
	}
	final, phases, err := e.wait(st, 8*time.Minute)
	if err != nil {
		return "", err
	}
	var cd string
	var got []string
	for i := 0; i < 60; i++ {
		_, _, final = e.do("GET", "/v1/runs/"+st.runID, e.workerToken, "", nil)
		cd = condStatus(final, "CallbackDelivered")
		if sigs, err := e.probe.signals(ctx, st.wf); err == nil {
			got = got[:0]
			for _, s := range sigs {
				got = append(got, fmt.Sprint(s["phase"]))
			}
			if len(got) > 0 && got[len(got)-1] == fmt.Sprint(final["phase"]) && strings.HasPrefix(cd, "True") {
				break
			}
		}
		time.Sleep(3 * time.Second)
	}
	e.probe.stop(ctx, st.wf)
	return fmt.Sprintf("Temporal down 60 s; DeliveryFailing seen=%v; final %v via %s; signals %v; CallbackDelivered=%s", sawFailing, final["phase"], strings.Join(phases, " → "), got, cd),
		expect(sawFailing && strings.HasPrefix(cd, "True") && len(got) > 0 && got[len(got)-1] == fmt.Sprint(final["phase"]) && ordered(got),
			"DeliveryFailing seen=%v CallbackDelivered=%s signals=%v final=%v", sawFailing, cd, got, final["phase"])
}
