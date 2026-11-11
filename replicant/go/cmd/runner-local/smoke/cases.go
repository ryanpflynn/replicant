package main

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/klauspost/compress/zstd"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/jose"
	"example.com/replicant/internal/runner/s3x"
	"example.com/replicant/internal/runner/schemas"
)

type tcase struct {
	id, name        string
	smoke, contract bool
	fn              func(context.Context, *env) (string, error)
}

// isolatedOnly cases need the driver's contract fakes (the fake grants host revokes jobs for RC-GRANT-05);
// v0Only cases need the live v0 stack (sbx network, egress proxy, mirrors, STS credentials, real run-grants).
var isolatedOnly = map[string]bool{"RC-GRANT-05": true, "RC-REPO-CFG": true}
var v0Only = map[string]bool{"SEC-EGRESS": true}

func (e *env) record(id, name string, pass bool, detail string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.results = append(e.results, caseResult{ID: id, Name: name, Pass: pass, Detail: detail})
	fmt.Printf("    %s %s: %s\n", map[bool]string{true: "PASS", false: "FAIL"}[pass], id, detail)
}

func expect(cond bool, f string, a ...any) error {
	if cond {
		return nil
	}
	return fmt.Errorf(f, a...)
}

func code(out map[string]any) string { return fmt.Sprint(out["code"]) }

func allCases() []tcase {
	return append(baseCases(), m3Cases()...)
}

func baseCases() []tcase {
	return []tcase{
		{"RC-META-01", "healthz and readyz", false, true, func(ctx context.Context, e *env) (string, error) {
			for _, p := range []string{"/healthz", "/readyz"} {
				c, _, _ := e.do("GET", p, "", "", nil)
				if c != 200 {
					return "", fmt.Errorf("%s: %d", p, c)
				}
			}
			return "200/200", nil
		}},
		{"RC-META-02", "JWKS", false, true, func(ctx context.Context, e *env) (string, error) {
			for _, base := range []string{e.runner, e.runnerSbx} {
				resp, err := http.Get(base + "/.well-known/jwks.json")
				if err != nil {
					return "", err
				}
				var s jose.JWKS
				_ = json.NewDecoder(resp.Body).Decode(&s)
				resp.Body.Close()
				if len(s.Keys) == 0 || s.Keys[0].Kty != "EC" || s.Keys[0].Crv != "P-256" || s.Keys[0].Alg != "ES256" || s.Keys[0].Use != "sig" || s.Keys[0].Kid == "" {
					return "", fmt.Errorf("bad jwks at %s: %+v", base, s)
				}
			}
			return "EC P-256 ES256 sig kid on both listeners", nil
		}},
		{"RC-AUTH-01", "no token", false, true, func(ctx context.Context, e *env) (string, error) {
			c, _, out := e.do("POST", "/v1/runs", "", `"ik1:x:y"`, map[string]any{})
			return fmt.Sprint(c, " ", code(out)), expect(c == 401 && code(out) == "unauthenticated" && out["retryable"] == true, "got %d %v", c, out)
		}},
		{"RC-AUTH-02", "token of an unknown subject", false, true, func(ctx context.Context, e *env) (string, error) {
			c, _, out := e.do("POST", "/v1/runs", e.readerToken, `"ik1:x:y"`, map[string]any{})
			return fmt.Sprint(c, " ", code(out)), expect(c == 403 && code(out) == "forbidden", "got %d %v", c, out)
		}},
		{"RC-IDEM-05/06/07", "missing, unquoted and mismatched keys", false, true, caseIdemHeaders},
		{"RC-GRANT-01..04", "grant missing, forged, digest mismatch, repo coverage", false, true, caseGrants},
		{"RC-ADM-01/02/03/05", "admission: prefix, definition, inputs digest, callback namespace", false, true, caseAdmission},
		{"RC-GET-01/03", "unknown run, list without filter", false, true, func(ctx context.Context, e *env) (string, error) {
			c, _, out := e.do("GET", "/v1/runs/"+ids.ULID("run"), e.workerToken, "", nil)
			if c != 404 || code(out) != "run_not_found" {
				return "", fmt.Errorf("get unknown: %d %v", c, out)
			}
			c, _, out = e.do("GET", "/v1/runs", e.workerToken, "", nil)
			return "404 run_not_found; 400 without job_id/tenant_id", expect(c == 400 && code(out) == "invalid_request", "list: %d %v", c, out)
		}},
		{"RC-CAN-04", "cancel unknown run", false, true, func(ctx context.Context, e *env) (string, error) {
			c, _, out := e.do("POST", "/v1/runs/"+ids.ULID("run")+":cancel", e.workerToken, "", map[string]any{"reason": "operator"})
			return fmt.Sprint(c), expect(c == 404 && code(out) == "run_not_found", "got %d %v", c, out)
		}},
		{"RC-REV-02", "feed auth", false, true, func(ctx context.Context, e *env) (string, error) {
			c1, _, _ := e.do("GET", "/v1/revocations", e.workerToken, "", nil)
			c2, _, _ := e.do("GET", "/v1/revocations", "", "", nil)
			c3, _, _ := e.do("GET", "/v1/revocations", e.readerToken, "", nil)
			return fmt.Sprintf("worker %d, none %d, reader %d", c1, c2, c3), expect(c1 == 403 && c2 == 401 && c3 == 200, "unexpected")
		}},
		{"RC-SBX-05", "isolation of listener", false, true, func(ctx context.Context, e *env) (string, error) {
			resp, err := http.Get(e.runnerSbx + "/v1/runs")
			if err != nil {
				return "", err
			}
			resp.Body.Close()
			resp2, err := http.Post(e.runnerSbx+"/v1/runs", "application/json", strings.NewReader("{}"))
			if err != nil {
				return "", err
			}
			resp2.Body.Close()
			return fmt.Sprintf("GET %d POST %d on the sandbox listener", resp.StatusCode, resp2.StatusCode),
				expect(resp.StatusCode == 404 && resp2.StatusCode == 404, "orchestration routes reachable on sandbox listener")
		}},
		{"SMOKE-IMPL", "implementer run: claude -p through the shim, run_result and outputs in S3", true, true, caseImplementer},
		{"SMOKE-QA", "QA run: replicant-qa install/unit/build on base + the implementer's diff", true, true, caseQA},
		{"RC-REPO-CFG", "repo_config use: repo hook runs, rules and nested CLAUDE.md load, .mcp.json skipped, managed deny wins", false, true, caseRepoConfig},
		{"RC-GRANT-05", "grant of a revoked job", false, true, func(ctx context.Context, e *env) (string, error) {
			st, err := e.stage(ctx, runOpts{})
			if err != nil {
				return "", err
			}
			e.grants.revokeJob(st.jobID)
			time.Sleep(8 * time.Second) // runner-local polls /grants/revoked every 5 s in the smoke stack
			c, out := e.create(st)
			return fmt.Sprint(c, " ", code(out)), expect(c == 422 && code(out) == "run_grant_invalid", "got %d %v", c, out)
		}},
		{"RC-RUN-09+ID-01", "C-IDS vector key; nonexistent base_sha", true, true, caseRepoFetch},
		{"RC-RUN-10", "inputs digest wrong", true, true, func(ctx context.Context, e *env) (string, error) {
			return e.failCase(ctx, runOpts{corruptInput: true}, "InputsFetchFailed", false, 4*time.Minute)
		}},
		{"RC-RUN-11", "diff that does not apply", true, true, func(ctx context.Context, e *env) (string, error) {
			bundle := diffBundle([]byte("--- a/nope.go\n+++ b/nope.go\n@@ -1,1 +1,1 @@\n-x\n+y\n"))
			return e.failCase(ctx, runOpts{extraObjects: []extraObj{{"prior_diff", "diff_bundle", "apply_diff", "", "application/vnd.replicant.diff-bundle.v1+tar+zstd", bundle}}},
				"DiffApplyFailed", false, 4*time.Minute)
		}},
		{"RC-RUN-07", "CLI exits non-zero", true, true, func(ctx context.Context, e *env) (string, error) {
			return e.failCase(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "exit_nonzero"}, "AgentExitedNonZero", true, 6*time.Minute)
		}},
		{"RC-RUN-12", "no structured result", true, true, func(ctx context.Context, e *env) (string, error) {
			return e.failCase(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "no_result"}, "AgentResultMissing", true, 6*time.Minute)
		}},
		{"RC-RUN-18", "budget below scripted usage", true, true, func(ctx context.Context, e *env) (string, error) {
			return e.failCase(ctx, runOpts{budget: 0.01}, "GatewayBudgetExceeded", false, 6*time.Minute)
		}},
		{"RC-RUN-16", "deadline", true, true, func(ctx context.Context, e *env) (string, error) {
			return e.failCase(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "long_running", active: 60, finalize: 30}, "DeadlineExceeded", false, 6*time.Minute)
		}},
		{"RC-CAN-02+LEASE-02", "cancel while running (and security cancel control key)", true, true, caseCancel},
		{"SEC-EGRESS", "sandbox egress and reachability SEC-EGRESS-01..08 (+ RC-SBX-04)", false, true, caseEgress},
		{"RC-RUN-17", "image that does not exist", true, true, func(ctx context.Context, e *env) (string, error) {
			ref := e.badEnvRef
			return e.failCase(ctx, runOpts{envRef: &ref, startup: 60}, "ImagePullFailed", false, 4*time.Minute)
		}},
	}
}

func caseIdemHeaders(ctx context.Context, e *env) (string, error) {
	st, err := e.stage(ctx, runOpts{})
	if err != nil {
		return "", err
	}
	c, _, out := e.do("POST", "/v1/runs", e.workerToken, "", st.body)
	if c != 400 || code(out) != "missing_idempotency_key" {
		return "", fmt.Errorf("missing: %d %v", c, out)
	}
	c, _, out = e.do("POST", "/v1/runs", e.workerToken, st.key, st.body)
	if c != 400 || code(out) != "invalid_request" {
		return "", fmt.Errorf("unquoted: %d %v", c, out)
	}
	c, _, out = e.do("POST", "/v1/runs", e.workerToken, `"ik1:`+st.wf+`:other.c0.n1.run"`, st.body)
	if c != 400 {
		return "", fmt.Errorf("mismatch: %d %v", c, out)
	}
	return "400 missing_idempotency_key / 400 invalid_request / 400", nil
}

func caseGrants(ctx context.Context, e *env) (string, error) {
	st, err := e.stage(ctx, runOpts{})
	if err != nil {
		return "", err
	}
	idem := `"` + st.key + `"`
	// 01 missing
	spec := clone(st.spec)
	delete(spec, "run_grant")
	c, _, out := e.do("POST", "/v1/runs", e.workerToken, idem, spec)
	if c != 400 || code(out) != "invalid_request" {
		return "", fmt.Errorf("RC-GRANT-01: %d %v", c, out)
	}
	// 02 forged: signed by a key not in the JWKS
	forged := jose.GenerateSigner("rg-local-1")
	var claims api.RunGrantClaims
	_ = jose.UnsafeClaims(st.grant, &claims)
	fg, _ := forged.Sign(api.GrantTyp, claims)
	spec = clone(st.spec)
	spec["run_grant"] = fg
	c, _, out = e.do("POST", "/v1/runs", e.workerToken, idem, spec)
	if c != 422 || code(out) != "run_grant_invalid" {
		return "", fmt.Errorf("RC-GRANT-02: %d %v", c, out)
	}
	// 03 digest mismatch: change limits after signing
	spec = clone(st.spec)
	spec["limits"] = map[string]any{"max_cost_usd": 2}
	c, _, out = e.do("POST", "/v1/runs", e.workerToken, idem, spec)
	if c != 422 || code(out) != "run_grant_invalid" {
		return "", fmt.Errorf("RC-GRANT-03: %d %v", c, out)
	}
	// 04 repo coverage: additional repo not in the grant
	spec = clone(st.spec)
	spec["additional_repos"] = []any{map[string]any{"provider": "gitea", "host": "git.replicant.localhost:3300", "owner": "acme-payments",
		"name": "payments-contracts", "repo_id": "99", "base_sha": e.gitea.baseSHA}}
	b, _ := json.Marshal(spec)
	st2 := &staged{spec: spec, runID: st.runID, key: st.key, jobID: st.jobID}
	e.resign(st2, runOpts{jobID: st.jobID})
	_ = b
	c, _, out = e.do("POST", "/v1/runs", e.workerToken, idem, st2.body)
	if c != 422 || code(out) != "run_grant_invalid" {
		return "", fmt.Errorf("RC-GRANT-04: %d %v", c, out)
	}
	return "400 / 422 run_grant_invalid x3", nil
}

func caseAdmission(ctx context.Context, e *env) (string, error) {
	mk := func(mut func(m map[string]any)) (*staged, error) {
		st, err := e.stage(ctx, runOpts{})
		if err != nil {
			return nil, err
		}
		mut(st.spec)
		e.resign(st, runOpts{jobID: st.jobID})
		return st, nil
	}
	// 01 prefix not canonical
	st, err := mk(func(m map[string]any) {
		p := m["artifact_prefixes"].(api.ArtifactPrefixes)
		p.Artifacts = strings.Replace(p.Artifacts, "/runs/", "/runz/", 1)
		p.Artifacts = "s3://" + e.bucket("artifacts") + "/tenants/payments/jobs/" + m["job_id"].(string) + "/runs/" + ids.ULID("run") + "/"
		m["artifact_prefixes"] = p
	})
	if err != nil {
		return "", err
	}
	c, out := e.create(st)
	if c != 422 || code(out) != "admission_denied" || !strings.Contains(fmt.Sprint(out["violations"]), "artifact_prefixes.artifacts") {
		return "", fmt.Errorf("RC-ADM-01: %d %v", c, out)
	}
	// 02 unknown definition digest
	st, _ = mk(func(m map[string]any) {
		m["agent_definition"] = api.DefinitionRef{Kind: "agent", Name: "implementer-claude", Version: 1, Digest: "sha256:" + strings.Repeat("a", 64)}
	})
	c, out = e.create(st)
	if c != 422 || code(out) != "definition_not_resolvable" {
		return "", fmt.Errorf("RC-ADM-02: %d %v", c, out)
	}
	// 03 inputs manifest digest mismatch
	st, _ = mk(func(m map[string]any) {
		in := m["inputs"].(map[string]any)
		in["manifest_sha256"] = strings.Repeat("b", 64)
	})
	c, out = e.create(st)
	if c != 422 || code(out) != "admission_denied" {
		return "", fmt.Errorf("RC-ADM-03: %d %v", c, out)
	}
	// 05 callback namespace
	st, _ = mk(func(m map[string]any) { m["callback"].(map[string]any)["namespace"] = "replicant-dev" })
	c, out = e.create(st)
	if c != 422 {
		return "", fmt.Errorf("RC-ADM-05: %d %v", c, out)
	}
	return "422 admission_denied (prefix, inputs), 422 definition_not_resolvable, 422 callback namespace", nil
}

func clone(m map[string]any) map[string]any {
	b, _ := json.Marshal(m)
	var c map[string]any
	_ = json.Unmarshal(b, &c)
	return c
}

// caseImplementer is the M1 smoke: a real `claude -p` implementer run, plus the contract cases that the same run
// proves (RC-IDEM-01..04, RC-GRANT-06/07, RC-RUN-01/02/05/08, RC-SBX-01..03, RC-REV-01, RC-ENV-01, RC-GET-02,
// RC-CAN-03, RC-MSG-03).
func caseImplementer(ctx context.Context, e *env) (string, error) {
	st, err := e.stage(ctx, runOpts{})
	if err != nil {
		return "", err
	}
	if e.probe != nil {
		if err := e.probe.begin(ctx, st.wf); err != nil {
			fmt.Println("  probe start:", err)
		}
	}
	// RC-IDEM-03: 20 concurrent creates, exactly one 201.
	var wg sync.WaitGroup
	codes := make([]int, 20)
	var first map[string]any
	var fmu sync.Mutex
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			c, out := e.create(st)
			codes[i] = c
			if c == 201 {
				fmu.Lock()
				first = out
				fmu.Unlock()
			}
		}(i)
	}
	wg.Wait()
	n201, n200 := 0, 0
	for _, c := range codes {
		switch c {
		case 201:
			n201++
		case 200:
			n200++
		}
	}
	e.record("RC-IDEM-03", "20 concurrent creates", n201 == 1 && n200 == 19, fmt.Sprintf("%d x 201, %d x 200", n201, n200))
	if first == nil {
		return "", fmt.Errorf("no 201: %v", codes)
	}
	e.record("RC-IDEM-01", "create → 201 Pending, run_id = derived(run, tenant, key)",
		first["run_id"] == st.runID && first["phase"] == "Pending" && first["spec_digest"] == st.digest,
		fmt.Sprintf("run_id %v phase %v", first["run_id"], first["phase"]))
	c, out := e.create(st)
	e.record("RC-IDEM-02", "repeat → 200 same run_id and spec_digest", c == 200 && out["run_id"] == st.runID && out["spec_digest"] == st.digest, fmt.Sprint(c))
	// RC-GRANT-06: re-signed grant; RC-GRANT-07: new traceparent.
	st6 := &staged{spec: clone(st.spec), runID: st.runID, key: st.key, jobID: st.jobID}
	e.resign(st6, runOpts{jobID: st.jobID})
	c, out = e.create(st6)
	e.record("RC-GRANT-06", "re-signed grant → 200, same digest", c == 200 && out["spec_digest"] == st.digest, fmt.Sprint(c))
	st7 := &staged{spec: clone(st.spec), runID: st.runID, key: st.key, jobID: st.jobID}
	st7.spec["trace_context"] = map[string]any{"traceparent": "00-fc0575170c8597ce9805552723c08d6e-7f3e9d2c1b0a4958-01"}
	st7.body, _ = json.Marshal(st7.spec)
	c, _ = e.create(st7)
	e.record("RC-GRANT-07", "different traceparent → 200", c == 200, fmt.Sprint(c))
	// RC-IDEM-04: same key, different spec.
	st4 := &staged{spec: clone(st.spec), runID: st.runID, key: st.key, jobID: st.jobID}
	st4.spec["priority"] = "high"
	e.resign(st4, runOpts{jobID: st.jobID})
	c, out = e.create(st4)
	e.record("RC-IDEM-04", "same key, different spec → 422 idempotency_key_reuse", c == 422 && code(out) == "idempotency_key_reuse", fmt.Sprint(c, " ", code(out)))
	// RC-MSG-03: steering not enabled.
	c, _, out = e.do("POST", "/v1/runs/"+st.runID+"/messages", e.workerToken, `"ik1:`+st.wf+`:steer.c0.n1.send"`,
		map[string]any{"message_id": ids.ULID("msg"), "text": "hello", "author": "svc_operator", "sent_at": time.Now().UTC().Format(time.RFC3339)})
	e.record("RC-MSG-03", "steering not supported → 409", c == 409 && code(out) == "steering_not_supported", fmt.Sprint(c, " ", code(out)))
	// RC-SBX-01/02 while the run is live: read the token set the runner wrote for agentd.
	go e.tokenChecks(ctx, st)
	final, phases, err := e.wait(st, 12*time.Minute)
	if err != nil {
		return "", err
	}
	fmt.Printf("  final: phase=%v failure=%v\n", final["phase"], final["failure"])
	ok := final["phase"] == "Succeeded"
	e.record("RC-RUN-01", "lifecycle Pending → Provisioning → Running → Finalizing → Succeeded", ok && ordered(phases), strings.Join(phases, " → "))
	if !ok {
		e.dumpRun(ctx, st)
		return "", fmt.Errorf("run did not succeed: %v", final["failure"])
	}
	// run_result and manifest
	detail, err := e.verifyArtifacts(ctx, st, final)
	e.record("RC-RUN-05", "manifest valid, digests match, tags present", err == nil, firstNonEmpty(errString(err), detail))
	if err != nil {
		return "", err
	}
	// RC-RUN-02 notifications
	if e.probe != nil {
		time.Sleep(3 * time.Second)
		sigs, err := e.probe.signals(ctx, st.wf)
		var got []string
		valid := err == nil
		for _, s := range sigs {
			got = append(got, fmt.Sprint(s["phase"]))
			id := fmt.Sprint(s["notification_id"])
			if !strings.HasPrefix(id, st.runID+":"+fmt.Sprint(s["phase"])+":") || s["schema"] != api.NotifSchema {
				valid = false
			}
		}
		cd := condStatus(final, "CallbackDelivered")
		e.record("RC-RUN-02", "≥ 1 Signal per phase, in order, notification_id <run_id>:<phase>:<gen>", valid && ordered(got) && len(got) >= 5,
			fmt.Sprintf("signals %v, CallbackDelivered=%s", got, cd))
		e.probe.stop(ctx, st.wf)
	}
	// RC-REV-01
	time.Sleep(2 * time.Second)
	c, _, rv := e.do("GET", "/v1/revocations", e.readerToken, "", nil)
	found := strings.Contains(fmt.Sprint(rv["revoked"]), st.runID)
	keys, _ := e.s3.List(ctx, e.bucket("ops"), "exec-revocations/local/", 0)
	e.record("RC-REV-01", "terminal run on the feed and in the ops bucket", c == 200 && found && len(keys) > 0 && rv["next_cursor"] != "",
		fmt.Sprintf("listed=%v next_cursor=%v objects=%d", found, rv["next_cursor"], len(keys)))
	// RC-GET-02
	c, _, l := e.do("GET", "/v1/runs?job_id="+st.jobID+"&page_size=1", e.workerToken, "", nil)
	runs, _ := l["runs"].([]any)
	e.record("RC-GET-02", "list by job_id", c == 200 && len(runs) == 1, fmt.Sprintf("%d runs", len(runs)))
	// RC-CAN-03
	c, _, out = e.do("POST", "/v1/runs/"+st.runID+":cancel", e.workerToken, "", map[string]any{"reason": "operator"})
	e.record("RC-CAN-03", "cancel a terminal run → 200 no-op", c == 200 && out["phase"] == "Succeeded", fmt.Sprint(c))
	// RC-SBX-03: the old runner-sandbox token after terminal
	if tok := e.lastSandboxToken; tok != "" {
		req, _ := http.NewRequest("POST", e.runnerSbx+"/sandbox/v1/token:refresh", nil)
		req.Header.Set("Authorization", "Bearer "+tok)
		resp, err := http.DefaultClient.Do(req)
		if err == nil {
			resp.Body.Close()
			e.record("RC-SBX-03", "refresh after terminal → 401/409", resp.StatusCode == 401 || resp.StatusCode == 409, fmt.Sprint(resp.StatusCode))
		}
	}
	return detail, nil
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func firstNonEmpty(a ...string) string {
	for _, s := range a {
		if s != "" {
			return s
		}
	}
	return ""
}

func condStatus(st map[string]any, typ string) string {
	cs, _ := st["conditions"].([]any)
	for _, c := range cs {
		m, _ := c.(map[string]any)
		if m["type"] == typ {
			return fmt.Sprintf("%v/%v", m["status"], m["reason"])
		}
	}
	return "absent"
}

var phaseOrder = map[string]int{"Pending": 0, "Provisioning": 1, "Running": 2, "Finalizing": 3, "Cancelling": 3, "Succeeded": 4, "Failed": 4, "Cancelled": 4}

func ordered(ph []string) bool {
	for i := 1; i < len(ph); i++ {
		if phaseOrder[ph[i]] < phaseOrder[ph[i-1]] {
			return false
		}
	}
	return len(ph) > 0
}

// tokenChecks reads the per-audience token files (RC-SBX-01) and refreshes (RC-SBX-02) while the run is live.
func (e *env) tokenChecks(ctx context.Context, st *staged) {
	if e.stateDir == "" {
		return
	}
	dir := filepath.Join(e.stateDir, "runs", st.runID, "tokens")
	var files []os.DirEntry
	for i := 0; i < 120; i++ {
		files, _ = os.ReadDir(dir)
		if len(files) > 0 {
			break
		}
		time.Sleep(500 * time.Millisecond)
	}
	if len(files) == 0 {
		e.record("RC-SBX-01", "per-audience tokens", false, "no token files at "+dir)
		return
	}
	var gc api.RunGrantClaims
	_ = jose.UnsafeClaims(st.grant, &gc)
	okAll := true
	var auds []string
	for _, f := range files {
		b, err := os.ReadFile(filepath.Join(dir, f.Name()))
		if err != nil {
			okAll = false
			continue
		}
		var c api.RunTokenClaims
		if jose.UnsafeClaims(string(b), &c) != nil {
			okAll = false
			continue
		}
		auds = append(auds, c.Aud)
		if c.Aud != f.Name() || c.Sub != "run:"+st.runID || c.GrantJti != gc.Jti || c.Grant != st.grant || c.Exp > gc.Exp || c.Exp-c.Iat > 3600 ||
			c.ActingFor != gc.ActingFor || !strings.HasPrefix(c.Jti, "rtk_") {
			okAll = false
		}
		if f.Name() == api.AudSandbox {
			e.lastSandboxToken = string(b)
		}
	}
	e.record("RC-SBX-01", "one token per grant audience, aud = file name, claims within the grant", okAll && len(files) == len(gc.Audiences),
		fmt.Sprintf("audiences %v (grant %v)", auds, gc.Audiences))
	if e.lastSandboxToken != "" {
		req, _ := http.NewRequest("POST", e.runnerSbx+"/sandbox/v1/token:refresh", nil)
		req.Header.Set("Authorization", "Bearer "+e.lastSandboxToken)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			e.record("RC-SBX-02", "token refresh", false, err.Error())
			return
		}
		var tr api.RunTokenResponse
		_ = json.NewDecoder(resp.Body).Decode(&tr)
		resp.Body.Close()
		newJti := true
		for aud, t := range tr.Tokens {
			var c api.RunTokenClaims
			_ = jose.UnsafeClaims(t.Token, &c)
			if c.Aud != aud || c.RunID != st.runID || c.GrantJti != gc.Jti {
				newJti = false
			}
		}
		e.record("RC-SBX-02", "refresh → token map, one per audience", resp.StatusCode == 200 && len(tr.Tokens) == len(gc.Audiences) && newJti,
			fmt.Sprintf("%d, %d tokens", resp.StatusCode, len(tr.Tokens)))
	}
}

func (e *env) verifyArtifacts(ctx context.Context, st *staged, final map[string]any) (string, error) {
	ab, ak, _ := s3x.ParseURI(st.prefixes.Artifacts)
	mb, _, err := e.s3.Get(ctx, ab, ak+"manifest.json")
	if err != nil {
		return "", fmt.Errorf("manifest: %v", err)
	}
	if err := schemas.Validate(schemas.RunManifest, mb); err != nil {
		return "", err
	}
	var m struct {
		Partial bool `json:"partial"`
		Result  *struct {
			Key    string `json:"key"`
			SHA256 string `json:"sha256"`
		} `json:"result"`
		Artifacts []struct {
			Type        string `json:"type"`
			BucketClass string `json:"bucket_class"`
			Key         string `json:"key"`
			SHA256      string `json:"sha256"`
			SizeBytes   int64  `json:"size_bytes"`
		} `json:"artifacts"`
	}
	_ = json.Unmarshal(mb, &m)
	type ent struct{ class, key, sha string }
	var all []ent
	if m.Result != nil {
		all = append(all, ent{"artifacts", m.Result.Key, m.Result.SHA256})
	}
	for _, a := range m.Artifacts {
		all = append(all, ent{a.BucketClass, a.Key, a.SHA256})
	}
	var types []string
	for _, a := range m.Artifacts {
		types = append(types, a.Type)
	}
	for _, x := range all {
		b, _, err := e.s3.Get(ctx, e.bucket(x.class), x.key)
		if err != nil {
			return "", fmt.Errorf("%s: %v", x.key, err)
		}
		if s3x.Sha256Hex(b) != x.sha {
			return "", fmt.Errorf("%s sha256 mismatch", x.key)
		}
		tg, err := e.s3.S3.GetObjectTagging(ctx, &s3.GetObjectTaggingInput{Bucket: aws.String(e.bucket(x.class)), Key: aws.String(x.key)})
		if err != nil {
			return "", fmt.Errorf("tags %s: %v", x.key, err)
		}
		have := map[string]bool{}
		for _, t := range tg.TagSet {
			have[aws.ToString(t.Key)] = true
		}
		if !have["replicant-retention"] || !have["replicant-type"] || !have["replicant-tenant"] {
			return "", fmt.Errorf("%s missing tags", x.key)
		}
	}
	// run_result
	rb, _, err := e.s3.Get(ctx, ab, ak+"run_result/result.json")
	if err != nil {
		return "", fmt.Errorf("result: %v", err)
	}
	if err := schemas.Validate(schemas.RunResult, rb); err != nil {
		return "", err
	}
	var rr map[string]any
	_ = json.Unmarshal(rb, &rr)
	if final["result_uri"] != st.prefixes.Artifacts+"run_result/result.json" {
		return "", fmt.Errorf("result_uri %v", final["result_uri"])
	}
	// diff bundle carries the fix
	out, _ := rr["outputs"].(map[string]any)
	dk := fmt.Sprint(out["diff_bundle"])
	db, _, err := e.s3.Get(ctx, ab, dk)
	if err != nil {
		return "", fmt.Errorf("diff bundle %s: %v", dk, err)
	}
	e.lastDiff = db
	patch := patchOf(db)
	fix := strings.Contains(patch, "+\t\tfxReversal := Prorate(c.FXFeeMinor, refundMinor, c.AmountMinor)")
	fmt.Printf("  run_result: outcome=%v summary=%.80q\n  outputs=%v\n  manifest artifacts: %v\n  patch:\n%s\n", rr["outcome"], rr["summary"], keysOf(out), types, indent(patch))
	if !fix {
		return "", fmt.Errorf("diff bundle does not contain the expected fix")
	}
	if m.Partial {
		return "", fmt.Errorf("manifest partial")
	}
	return fmt.Sprintf("outcome=%v, %d manifest objects verified (sha256 + tags), diff carries the one-line fix", rr["outcome"], len(all)), nil
}

func indent(s string) string {
	var b strings.Builder
	for _, l := range strings.Split(strings.TrimRight(s, "\n"), "\n") {
		b.WriteString("    | " + l + "\n")
	}
	return b.String()
}

func patchOf(bundle []byte) string {
	dec, err := zstd.NewReader(bytes.NewReader(bundle))
	if err != nil {
		return ""
	}
	defer dec.Close()
	tr := tar.NewReader(dec)
	for {
		h, err := tr.Next()
		if err != nil {
			return ""
		}
		if h.Name == "changes.patch" {
			b, _ := io.ReadAll(tr)
			return string(b)
		}
	}
}

func diffBundle(patch []byte) []byte {
	var buf bytes.Buffer
	zw, _ := zstd.NewWriter(&buf)
	tw := tar.NewWriter(zw)
	_ = tw.WriteHeader(&tar.Header{Name: "changes.patch", Mode: 0o644, Size: int64(len(patch))})
	_, _ = tw.Write(patch)
	_ = tw.Close()
	_ = zw.Close()
	return buf.Bytes()
}

func (e *env) dumpRun(ctx context.Context, st *staged) {
	ab, ak, _ := s3x.ParseURI(st.prefixes.Artifacts)
	if b, _, err := e.s3.Get(ctx, ab, ak+"manifest.json"); err == nil {
		fmt.Printf("  manifest: %s\n", b)
	}
	tb, tk, _ := s3x.ParseURI(st.prefixes.Transcripts)
	keys, _ := e.s3.List(ctx, tb, tk, 0)
	fmt.Printf("  transcript objects: %v\n", keys)
}

// failCase runs a scenario that must end Failed with reason.
func (e *env) failCase(ctx context.Context, o runOpts, reason string, retryable bool, timeout time.Duration) (string, error) {
	st, err := e.stage(ctx, o)
	if err != nil {
		return "", err
	}
	c, out := e.create(st)
	if c != 201 {
		return "", fmt.Errorf("create: %d %v", c, out)
	}
	final, phases, err := e.wait(st, timeout)
	if err != nil {
		return "", err
	}
	f, _ := final["failure"].(map[string]any)
	got := failureReason(final)
	detail := fmt.Sprintf("%s retryable=%v via %s; message=%.120q", got, f["retryable"], strings.Join(phases, " → "), f["message"])
	if final["phase"] != "Failed" || got != reason || f["retryable"] != retryable {
		e.dumpRun(ctx, st)
		return detail, fmt.Errorf("want Failed/%s retryable=%v, got %v/%s: %s", reason, retryable, final["phase"], got, detail)
	}
	return detail, nil
}

func caseRepoFetch(ctx context.Context, e *env) (string, error) {
	jobID := "job_01m5p7x7r0bt69j0h0fbxepw90"
	st, err := e.stage(ctx, runOpts{jobID: jobID, baseSHA: strings.Repeat("0", 39) + "1", steering: true})
	if err != nil {
		return "", err
	}
	c, out := e.create(st)
	if c != 201 && c != 200 {
		if c == 422 && code(out) == "idempotency_key_reuse" {
			return "", fmt.Errorf("vector run already exists with another spec (re-run with a fresh runner state)")
		}
		return "", fmt.Errorf("create %d %v", c, out)
	}
	wantID := "run_gj3h3ep3phjfs9e23v768zrca5"
	ar, _ := out["agentrun"].(map[string]any)
	e.record("RC-ID-01", "C-IDS vector key → run_gj3h3ep3phjfs9e23v768zrca5, ar-<body>", out["run_id"] == wantID && ar["name"] == "ar-"+ids.Body(wantID),
		fmt.Sprint(out["run_id"], " ", ar["name"]))
	final, phases, err := e.wait(st, 5*time.Minute)
	if err != nil {
		return "", err
	}
	got := failureReason(final)
	// RC-MSG-04 on the terminal steering-enabled run.
	mc, _, mo := e.do("POST", "/v1/runs/"+st.runID+"/messages", e.workerToken, `"ik1:`+st.wf+`:steer.c0.n1.send"`,
		map[string]any{"message_id": ids.ULID("msg"), "text": "hi", "author": "svc_operator", "sent_at": time.Now().UTC().Format(time.RFC3339)})
	e.record("RC-MSG-04", "steering a terminal run → 409 run_not_active", mc == 409 && code(mo) == "run_not_active", fmt.Sprint(mc, " ", code(mo)))
	return fmt.Sprintf("%s via %s", got, strings.Join(phases, " → ")), expect(final["phase"] == "Failed" && got == "RepoFetchFailed", "got %v/%s", final["phase"], got)
}

// caseCancel: cancel while Running (RC-CAN-02), then a security cancel of a second run (RC-LEASE-02).
func caseCancel(ctx context.Context, e *env) (string, error) {
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
	time.Sleep(5 * time.Second)
	c, _, out := e.do("POST", "/v1/runs/"+st.runID+":cancel", e.workerToken, "", map[string]any{"reason": "job_cancelled", "requested_by": "svc_operator"})
	if c != 202 {
		return "", fmt.Errorf("cancel: %d %v", c, out)
	}
	final, phases, err := e.wait(st, 4*time.Minute)
	if err != nil {
		return "", err
	}
	got := failureReason(final)
	e.record("RC-CAN-02", "cancel while Running → Cancelling → Cancelled", final["phase"] == "Cancelled" && got == "CancelledByRequest" && contains(phases, "Cancelling"),
		fmt.Sprintf("%v/%s via %s, ArtifactsUploaded=%s", final["phase"], got, strings.Join(phases, " → "), condStatus(final, "ArtifactsUploaded")))
	// security cancel
	st2, err := e.stage(ctx, runOpts{scriptSet: "runner-contract", scriptKey: "long_running", finalize: 45})
	if err != nil {
		return "", err
	}
	if c, out := e.create(st2); c != 201 {
		return "", fmt.Errorf("create %d %v", c, out)
	}
	if err := e.waitPhase(st2, "Running", 5*time.Minute); err != nil {
		return "", err
	}
	c, _, _ = e.do("POST", "/v1/runs/"+st2.runID+":cancel", e.workerToken, "", map[string]any{"reason": "security"})
	ab, ak, _ := s3x.ParseURI(st2.prefixes.Artifacts)
	cb, _, cerr := e.s3.Get(ctx, ab, ak+"inbox/_control.json")
	final2, _, err := e.wait(st2, 4*time.Minute)
	if err != nil {
		return "", err
	}
	e.record("RC-LEASE-02", "security cancel: inbox/_control.json written; run ends Failed/SecurityViolation",
		cerr == nil && strings.Contains(string(cb), `"security"`) && failureReason(final2) == "SecurityViolation",
		fmt.Sprintf("control=%s final=%v/%s", strings.TrimSpace(string(cb)), final2["phase"], failureReason(final2)))
	return fmt.Sprintf("cancel %d", c), nil
}

func contains(s []string, x string) bool {
	for _, v := range s {
		if v == x {
			return true
		}
	}
	return false
}

func (e *env) waitPhase(st *staged, phase string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		_, _, out := e.do("GET", "/v1/runs/"+st.runID, e.workerToken, "", nil)
		if out["phase"] == phase {
			return nil
		}
		if t, _ := out["terminal"].(bool); t {
			return fmt.Errorf("terminal before %s: %v %v", phase, out["phase"], out["failure"])
		}
		time.Sleep(time.Second)
	}
	return fmt.Errorf("never reached %s", phase)
}

func caseQA(ctx context.Context, e *env) (string, error) {
	if e.lastDiff == nil {
		return "", fmt.Errorf("needs SMOKE-IMPL first (diff bundle)")
	}
	st, err := e.stage(ctx, runOpts{role: "qa", extraObjects: []extraObj{{"prior_diff", "diff_bundle", "apply_diff", "changes.tar.zst",
		"application/vnd.replicant.diff-bundle.v1+tar+zstd", e.lastDiff}}})
	if err != nil {
		return "", err
	}
	if c, out := e.create(st); c != 201 {
		return "", fmt.Errorf("create %d %v", c, out)
	}
	final, phases, err := e.wait(st, 10*time.Minute)
	if err != nil {
		return "", err
	}
	if final["phase"] != "Succeeded" {
		e.dumpRun(ctx, st)
		return "", fmt.Errorf("QA run %v %v", final["phase"], final["failure"])
	}
	ab, ak, _ := s3x.ParseURI(st.prefixes.Artifacts)
	rb, _, err := e.s3.Get(ctx, ab, ak+"run_result/result.json")
	if err != nil {
		return "", err
	}
	var rr struct {
		Outcome string            `json:"outcome"`
		Outputs map[string]string `json:"outputs"`
		RR      struct {
			Verdict string           `json:"verdict"`
			Counts  map[string]int   `json:"counts"`
			Stages  []map[string]any `json:"stages"`
		} `json:"role_result"`
	}
	_ = json.Unmarshal(rb, &rr)
	detail := fmt.Sprintf("verdict=%s outcome=%s tests=%v stages=%v qa_report=%s via %s", rr.RR.Verdict, rr.Outcome, rr.RR.Counts, rr.RR.Stages,
		rr.Outputs["qa_report"], strings.Join(phases, " → "))
	return detail, expect(rr.RR.Verdict == "pass" && rr.Outcome == "completed" && rr.Outputs["qa_report"] != "", "unexpected QA result")
}
