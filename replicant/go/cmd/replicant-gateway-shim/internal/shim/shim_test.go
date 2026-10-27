package shim

import (
	"bufio"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

const happyScript = `
schema: replicant.gateway-script/v1
name: t-happy
protocol: anthropic_messages
turns:
  - expect: {model: anthropic/claude-sonnet-5-5, tools: [Read]}
    respond:
      text: "reading"
      tool_use: {name: Read, input: {file_path: "${cwd}/a.go"}}
      usage: {input_tokens: 1000, output_tokens: 100}
  - fault: {status: 529}
  - respond:
      text: "done"
      structured_output: {outcome: completed, summary: s, tests_run: []}
`

func scriptHarness(t *testing.T) (*harness, minted) {
	h := newHarness(t, "script", "", nil)
	h.script("set1", "implementer", happyScript)
	m := h.mint(runOpts{})
	mustStatus(t, h.policy(m, &Stub{Root: "scripts", Set: "set1", Key: "implementer.c0.n1"}), 200)
	return h, m
}

// GW-01: non-streaming request with a valid token, grant and policy; usage event with attribution.
func TestGW01NonStreamingAndUsageEvent(t *testing.T) {
	h, m := scriptHarness(t)
	resp := h.do("POST", "/anthropic/v1/messages?beta=true", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true),
		map[string]string{"X-Replicant-Run-Id": m.runID, "X-Replicant-Role": "implementer", "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"})
	if !strings.HasPrefix(resp.Header.Get("X-Replicant-Request-Id"), "gwu_") {
		t.Fatal("missing X-Replicant-Request-Id")
	}
	if got := resp.Header.Get("X-Replicant-Budget-Run-Limit-Usd"); got != "15.000000" {
		t.Fatalf("run limit header %q", got)
	}
	// 1000 in × $3/M + 100 out × $15/M = 3000 + 1500 micros, included for non-streaming responses
	if got := resp.Header.Get("X-Replicant-Budget-Run-Spent-Usd"); got != "0.004500" {
		t.Fatalf("run spent header %q", got)
	}
	if resp.Header.Get("X-Replicant-Served-Model") != "claude-sonnet-5-5" {
		t.Fatal("served model header")
	}
	out := mustStatus(t, resp, 200)
	if out["stop_reason"] != "tool_use" || out["model"] != "claude-sonnet-5-5" {
		t.Fatalf("body %v", out)
	}
	content := out["content"].([]any)
	tu := content[1].(map[string]any)
	if tu["name"] != "Read" || tu["input"].(map[string]any)["file_path"] != "/workspace/repo-x/a.go" {
		t.Fatalf("tool_use %v (cwd substitution)", tu)
	}
	ev := h.usage.all()
	if len(ev) != 1 {
		t.Fatalf("%d events", len(ev))
	}
	e := ev[0]
	if e.RunID != m.runID || e.JobID != m.jobID || e.TenantID != "acme-payments" || e.ActingFor == "" || e.Cost.MicrosUSD != 4500 ||
		e.Usage.InputTokens != 1000 || e.Purpose != "agent" || e.UsageSource != "parsed" || e.TraceID != "0af7651916cd43dd8448eb211c80319c" ||
		e.Caller.Kind != "run" || !strings.HasPrefix(e.Caller.GrantJti, "rgr_") || e.EvaluationMode != "none" {
		t.Fatalf("event %+v", e)
	}
}

// GW-02: streaming in provider event order with final usage; a scripted fault is served once and the retry
// (same conversation length) gets the next turn; StructuredOutput is emitted as a tool call and then finished.
func TestGW02StreamingFaultAndFinish(t *testing.T) {
	h, m := scriptHarness(t)
	h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", true, 1, true), nil).Body.Close()
	// retry of the same request replays turn 0
	resp := h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", true, 1, true), nil)
	var events []string
	sc := bufio.NewScanner(resp.Body)
	sawTool := false
	for sc.Scan() {
		if e, ok := strings.CutPrefix(sc.Text(), "event: "); ok {
			events = append(events, e)
		}
		if strings.Contains(sc.Text(), `"name":"Read"`) {
			sawTool = true
		}
	}
	resp.Body.Close()
	want := "message_start,ping,content_block_start,content_block_delta,content_block_stop,content_block_start,content_block_delta,content_block_stop,message_delta,message_stop"
	if strings.Join(events, ",") != want || !sawTool {
		t.Fatalf("events %v sawTool %v", events, sawTool)
	}
	// next main-loop request hits the fault turn (529), the retry gets turn 2
	mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 3, true), nil), 529)
	out := mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 3, true), nil), 200)
	blocks := out["content"].([]any)
	so := blocks[len(blocks)-1].(map[string]any)
	if so["name"] != "StructuredOutput" {
		t.Fatalf("expected StructuredOutput call, got %v", blocks)
	}
	// the CLI answers the StructuredOutput call: the shim finishes with end_turn without consuming turns
	body := msgBody("anthropic/claude-sonnet-5-5", false, 3, true)
	msgs := body["messages"].([]map[string]any)
	msgs = append(msgs, map[string]any{"role": "assistant", "content": []any{so}},
		map[string]any{"role": "user", "content": []any{map[string]any{"type": "tool_result", "tool_use_id": so["id"], "content": "ok"}}})
	body["messages"] = msgs
	fin := mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, body, nil), 200)
	if fin["stop_reason"] != "end_turn" {
		t.Fatalf("finish %v", fin)
	}
	// auxiliary (tool-less) request never consumes a turn; script is now exhausted for main-loop requests
	mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-haiku-5-5", false, 1, false), nil), 200)
	miss := mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 7, true), nil), 599)
	if miss["replicant_code"] != "cassette_miss" {
		t.Fatalf("miss %v", miss)
	}
}

// GW-03: a token for another audience is rejected; GW-04: grant signed by an unknown key, or token outside grant.
func TestGW03GW04TokenChecks(t *testing.T) {
	h := newHarness(t, "script", "", nil)
	for name, o := range map[string]runOpts{
		"aud context-mcp":    {aud: "context-mcp"},
		"aud array without":  {aud: []string{"memory-mcp"}},
		"unknown grant key":  {grantKey: mustKey()},
		"budget above grant": {budget: 20, grantBudget: 10},
		"expired":            {exp: -2 * time.Minute},
	} {
		m := h.mint(o)
		out := mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil), 401)
		if out["replicant_code"] != "unauthenticated" || out["type"] != "error" {
			t.Fatalf("%s: %v", name, out)
		}
	}
	m := h.mint(runOpts{scopes: []string{"inbox:read"}})
	if out := mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("x/y", false, 1, true), nil), 403); out["replicant_code"] != "scope_missing" {
		t.Fatal(out)
	}
	// aud as array containing ai-gateway is accepted during the CCR-09-01 window
	m = h.mint(runOpts{aud: []string{"ai-gateway"}})
	mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil), 503) // no policy
	// header mismatch is 401
	m = h.mint(runOpts{})
	mustStatus(t, h.policy(m, nil), 200)
	mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), map[string]string{"X-Replicant-Role": "reviewer"}), 401)
	// no auth at all
	mustStatus(t, h.do("POST", "/anthropic/v1/messages", "", msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil), 401)
}

func mustKey() *ecdsa.PrivateKey {
	k, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	return k
}

// GW-05: a job on /grants/revoked is rejected after one poll; GW-14: a run on the exec revocation feed.
func TestGW05GW14RevocationFeeds(t *testing.T) {
	h, m := scriptHarness(t)
	m2 := h.mint(runOpts{})
	mustStatus(t, h.policy(m2, &Stub{Root: "scripts", Set: "set1", Key: "implementer"}), 200)
	grants := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{"generated_at":"2026-10-24T00:00:00Z","job_ids":[{"id":"`+m.jobID+`","revoked_at":"2026-10-24T00:00:00Z"}],"run_ids":[]}`)
	}))
	defer grants.Close()
	exec := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer feedtok" {
			w.WriteHeader(401)
			return
		}
		_, _ = io.WriteString(w, `{"revoked":[{"run_id":"`+m2.runID+`","job_id":"`+m2.jobID+`","at":"2026-10-24T00:00:00Z","reason":"Succeeded"}],"next_cursor":"c1"}`)
	}))
	defer exec.Close()
	writeFile(t, h.dir+"/feedtok", "feedtok\n")
	h.cfg.GrantRevocationURL = grants.URL
	h.cfg.ExecRevocationURLs = []string{exec.URL, "http://127.0.0.1:1/v1/revocations"} // second feed unreachable
	h.cfg.ExecRevocationTokenFile = h.dir + "/feedtok"
	if err := h.srv.Feeds.PollGrantsOnce(t.Context()); err != nil {
		t.Fatal(err)
	}
	if err := h.srv.Feeds.PollExecOnce(t.Context()); err != nil {
		t.Fatal(err)
	}
	for _, mm := range []minted{m, m2} {
		out := mustStatus(t, h.do("POST", "/anthropic/v1/messages", mm.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil), 401)
		if out["replicant_code"] != "run_closed" {
			t.Fatal(out)
		}
	}
}

// GW-06: model outside the policy, and an unpriced model.
func TestGW06ModelNotAllowed(t *testing.T) {
	h, m := scriptHarness(t)
	out := mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-opus-5-5", false, 1, true), nil), 403)
	if out["replicant_code"] != "model_not_allowed" || out["error"].(map[string]any)["type"] != "permission_error" {
		t.Fatal(out)
	}
	m2 := h.mint(runOpts{})
	mustStatus(t, h.policy(m2, nil, "anthropic/unpriced"), 200)
	out = mustStatus(t, h.do("POST", "/anthropic/v1/messages", m2.token, msgBody("anthropic/unpriced", false, 1, true), nil), 403)
	if out["detail"] != "unpriced_model" {
		t.Fatal(out)
	}
	if ev := h.usage.all(); len(ev) != 2 || ev[0].Status != "model_denied" {
		t.Fatalf("denied requests must emit usage events: %+v", ev)
	}
}

const bigScript = `
schema: replicant.gateway-script/v1
name: t-big
protocol: anthropic_messages
turns:
  - respond: {text: a, usage: {input_tokens: 1000000, output_tokens: 0}}
  - respond: {text: b, usage: {input_tokens: 1000000, output_tokens: 0}}
  - respond: {text: c, usage: {input_tokens: 1000000, output_tokens: 0}}
`

// GW-07: spend reaching the run budget is 402 run; GW-08: job budget across two runs is 402 job.
func TestGW07GW08Budgets(t *testing.T) {
	h := newHarness(t, "script", "", nil)
	h.script("b", "implementer", bigScript)
	m := h.mint(runOpts{budget: 5})
	mustStatus(t, h.policy(m, &Stub{Root: "scripts", Set: "b", Key: "implementer"}), 200)
	mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil), 200) // $3
	mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 3, true), nil), 200) // $6 ≥ $5 after
	resp := h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 5, true), nil)
	if resp.Header.Get("X-Replicant-Budget-Exceeded") != "run" {
		t.Fatalf("header %q", resp.Header.Get("X-Replicant-Budget-Exceeded"))
	}
	out := mustStatus(t, resp, 402)
	if out["replicant_code"] != "budget_exceeded" || out["budget"] == nil {
		t.Fatal(out)
	}
	// GET /v1/budget
	bs := mustStatus(t, h.do("GET", "/v1/budget", m.token, nil, nil), 200)
	if bs["run"].(map[string]any)["spent_usd"].(float64) != 6 {
		t.Fatal(bs)
	}

	// job budget across two runs
	job := m.jobID
	resp = h.do("PUT", "/internal/v1/jobs/"+job+"/budget", h.svcToken, map[string]any{"tenant_id": "acme-payments", "job_budget_usd": 7}, map[string]string{"Idempotency-Key": "k1"})
	mustStatus(t, resp, 200)
	m2 := h.mint(runOpts{budget: 100, jobID: job})
	mustStatus(t, h.policy(m2, &Stub{Root: "scripts", Set: "b", Key: "implementer"}), 200)
	mustStatus(t, h.do("POST", "/anthropic/v1/messages", m2.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil), 200) // job $9 ≥ $7
	resp = h.do("POST", "/anthropic/v1/messages", m2.token, msgBody("anthropic/claude-sonnet-5-5", false, 3, true), nil)
	if resp.Header.Get("X-Replicant-Budget-Exceeded") != "job" {
		t.Fatalf("header %q", resp.Header.Get("X-Replicant-Budget-Exceeded"))
	}
	mustStatus(t, resp, 402)
}

func TestTenantQuotaFromControlPlane(t *testing.T) {
	cp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer cptok" {
			w.WriteHeader(403)
			return
		}
		if strings.Contains(r.URL.Path, "/tenants/acme-payments/") {
			_, _ = io.WriteString(w, `{"tenant_id":"acme-payments","monthly_budget_usd":0.002,"daily_budget_usd":10,"platform_daily_usd_max":100,"platform_monthly_usd_max":1000,"version":3,"as_of":"2026-10-24T00:00:00Z"}`)
			return
		}
		w.WriteHeader(404)
	}))
	defer cp.Close()
	h := newHarness(t, "script", "", map[string]string{"REPLICANT_CONTROL_PLANE_URL": cp.URL, "REPLICANT_GATEWAY_LOCAL_QUOTA": ""})
	writeFile(t, h.dir+"/cptok", "cptok")
	h.cfg.ControlPlaneTokenFile = h.dir + "/cptok"
	h.script("set1", "implementer", happyScript)
	m := h.mint(runOpts{})
	mustStatus(t, h.policy(m, &Stub{Root: "scripts", Set: "set1", Key: "implementer"}), 200)
	mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil), 200) // spends $0.0045 ≥ $0.002
	resp := h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 3, true), nil)
	if resp.Header.Get("X-Replicant-Budget-Exceeded") != "tenant_period" {
		t.Fatalf("header %q", resp.Header.Get("X-Replicant-Budget-Exceeded"))
	}
	mustStatus(t, resp, 402)
	// another tenant has no quota (404)
	h.srv.Quotas = NewQuotaClient(h.cfg)
	tok := h.mint(runOpts{})
	_ = tok
	q, err := h.srv.Quotas.Get(t.Context(), "other-tenant")
	if err != nil || q != nil {
		t.Fatalf("404 must mean unset: %v %v", q, err)
	}
}

// GW-11: self:close then a request is 401 run_closed; internal revocations by control-plane-api.
func TestGW11SelfCloseAndInternalRevocation(t *testing.T) {
	h, m := scriptHarness(t)
	mustStatus(t, h.do("POST", "/v1/runs/self:close", m.token, nil, nil), 204)
	if out := mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil), 401); out["replicant_code"] != "run_closed" {
		t.Fatal(out)
	}
	m2 := h.mint(runOpts{})
	mustStatus(t, h.policy(m2, nil), 200)
	// the worker principal may not revoke
	mustStatus(t, h.do("POST", "/internal/v1/revocations", h.svcToken, map[string]any{"job_ids": []string{m2.jobID}, "reason": "job_cancelled"}, nil), 403)
	mustStatus(t, h.do("POST", "/internal/v1/revocations", "admin-"+h.svcToken, map[string]any{"job_ids": []string{m2.jobID}, "reason": "job_cancelled"}, nil), 204)
	mustStatus(t, h.do("POST", "/anthropic/v1/messages", m2.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil), 401)
	mustStatus(t, h.do("POST", "/internal/v1/revocations", "admin-"+h.svcToken, map[string]any{"job_ids": []string{"bad"}, "reason": "job_cancelled"}, nil), 400)
}

// GW-12: a stub in a policy sent to an enterprise (production) shim is 400 stub_not_allowed.
func TestGW12StubRejectedInProduction(t *testing.T) {
	h := newHarness(t, "enterprise", "", map[string]string{"REPLICANT_GATEWAY_LOCAL_QUOTA": ""})
	m := h.mint(runOpts{})
	out := mustStatus(t, h.policy(m, &Stub{Root: "tests", Set: "s", Key: "k"}), 400)
	if out["replicant_code"] != "stub_not_allowed" {
		t.Fatal(out)
	}
	// X-Replicant-Stub-Fault is ignored by the enterprise backend, and without a control-plane quota the tenant is denied
	mustStatus(t, h.policy(m, nil), 200)
	out = mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), map[string]string{"X-Replicant-Stub-Fault": "500"}), 402)
	if out["replicant_code"] != "tenant_quota_unset" {
		t.Fatal(out)
	}
}

func TestPolicyRegistration(t *testing.T) {
	h := newHarness(t, "script", "", nil)
	m := h.mint(runOpts{})
	p := Policy{TenantID: m.tenant, JobID: m.jobID, Role: m.role, ReleaseBundleID: "rb_01m5r7ap8fwrbbsw819jn8ptwp", Models: []string{"anthropic/claude-sonnet-5-5"},
		BudgetUSD: 15, MaxConcurrency: 1, ExpiresAt: time.Now().Add(time.Hour).UTC().Format(time.RFC3339)}
	path := "/internal/v1/runs/" + m.runID + "/policy"
	mustStatus(t, h.do("PUT", path, "", p, nil), 401)
	mustStatus(t, h.do("PUT", path, m.token, p, nil), 403) // run tokens cannot register policies
	mustStatus(t, h.do("PUT", path, h.svcToken, p, nil), 200)
	mustStatus(t, h.do("PUT", path, h.svcToken, p, nil), 200) // idempotent
	got := mustStatus(t, h.do("GET", path, h.svcToken, nil, nil), 200)
	if got["job_id"] != m.jobID {
		t.Fatal(got)
	}
	p2 := p
	p2.MaxConcurrency = 2
	if out := mustStatus(t, h.do("PUT", path, h.svcToken, p2, nil), 409); out["replicant_code"] != "run_policy_mismatch" {
		t.Fatal(out)
	}
	mustStatus(t, h.do("PUT", path, h.svcToken, `{"tenant_id":"x","unknown":1}`, nil), 400)
	mustStatus(t, h.do("GET", "/internal/v1/runs/run_01m5r7ap8fwrbbsw819jn8ptwz/policy", h.svcToken, nil, nil), 404)
	// mismatched budget between token and policy
	m3 := h.mint(runOpts{budget: 3})
	p3 := p
	p3.JobID, p3.BudgetUSD = m3.jobID, 4
	mustStatus(t, h.do("PUT", "/internal/v1/runs/"+m3.runID+"/policy", h.svcToken, p3, nil), 200)
	if out := mustStatus(t, h.do("POST", "/anthropic/v1/messages", m3.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil), 403); out["replicant_code"] != "run_policy_mismatch" {
		t.Fatal(out)
	}
	// missing policy is 503 retryable
	m4 := h.mint(runOpts{})
	resp := h.do("POST", "/anthropic/v1/messages", m4.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil)
	if resp.Header.Get("retry-after") != "5" {
		t.Fatal("retry-after")
	}
	mustStatus(t, resp, 503)
	// run concurrency: max_concurrency 1 with one request held in flight
	if !h.srv.State.AcquireInflight("run:"+m.runID, 1) {
		t.Fatal("acquire")
	}
	resp = h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil)
	if resp.Header.Get("X-Replicant-Limit") != "run_concurrency" {
		t.Fatal("X-Replicant-Limit")
	}
	mustStatus(t, resp, 429)
}

func TestStubFaultHeaderAndCountTokens(t *testing.T) {
	h, m := scriptHarness(t)
	resp := h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), map[string]string{"X-Replicant-Stub-Fault": "429;retry_after=3"})
	if resp.Header.Get("retry-after") != "3" || resp.Header.Get("X-Replicant-Limit") != "" {
		t.Fatal("upstream-shaped 429 expected")
	}
	if out := mustStatus(t, resp, 429); out["error"].(map[string]any)["type"] != "rate_limit_error" {
		t.Fatal(out)
	}
	resp = h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), map[string]string{"X-Replicant-Stub-Fault": "402;budget_level=job"})
	if resp.Header.Get("X-Replicant-Budget-Exceeded") != "job" {
		t.Fatal("budget header")
	}
	mustStatus(t, resp, 402)
	out := mustStatus(t, h.do("POST", "/anthropic/v1/messages/count_tokens", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil), 200)
	if out["input_tokens"] == nil {
		t.Fatal(out)
	}
	// faults and count_tokens are not charged
	if _, spent, _ := h.srv.State.CounterState("fgw:run:" + m.runID); spent != 0 {
		t.Fatalf("spent %d", spent)
	}
}

func TestScriptResolutionFallbacks(t *testing.T) {
	h := newHarness(t, "script", "", map[string]string{"REPLICANT_GATEWAY_SCRIPT_DEFAULT_SET": "dflt"})
	h.script("dflt", "reviewer", happyScript)
	h.script("set1", "implementer.c1", strings.Replace(happyScript, "t-happy", "t-c1", 1))
	be := h.srv.Backend.(*ScriptBackend)
	// root "tests" falls back to the scripts root; key implementer.c1.n2 -> implementer.c1
	s, _, err := be.Resolve(&DataRequest{Role: "implementer", Policy: &Policy{Stub: &Stub{Root: "tests", Set: "set1", Key: "implementer.c1.n2"}}})
	if err != nil || s.Name != "t-c1" {
		t.Fatalf("%v %v", s, err)
	}
	// no stub: default set + role
	s, _, err = be.Resolve(&DataRequest{Role: "reviewer", Policy: &Policy{}})
	if err != nil || s.Name != "t-happy" {
		t.Fatalf("%v %v", s, err)
	}
	// path traversal is refused
	if _, _, err := be.Resolve(&DataRequest{Role: "x", Policy: &Policy{Stub: &Stub{Root: "scripts", Set: "../etc", Key: "passwd"}}}); err == nil {
		t.Fatal("traversal accepted")
	}
}

func TestServiceCallerWorkerLLM(t *testing.T) {
	h := newHarness(t, "script", "", map[string]string{"REPLICANT_GATEWAY_SCRIPT_DEFAULT_SET": "svc"})
	h.script("svc", "worker_llm", happyScript)
	hdr := map[string]string{"X-Replicant-Purpose": "worker_llm", "X-Replicant-Tenant-Id": "acme-payments", "X-Replicant-Job-Id": "job_01m5r7ap8fwrbbsw819jn8ptwm"}
	// tools are not allowed for worker_llm (G-24)
	out := mustStatus(t, h.do("POST", "/anthropic/v1/messages", h.svcToken, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), hdr), 400)
	if out["replicant_code"] != "tools_not_allowed" {
		t.Fatal(out)
	}
	// purpose not bound
	hdr2 := map[string]string{"X-Replicant-Purpose": "embedding", "X-Replicant-Tenant-Id": "acme-payments"}
	mustStatus(t, h.do("POST", "/anthropic/v1/messages", h.svcToken, msgBody("anthropic/claude-sonnet-5-5", false, 1, false), hdr2), 403)
	// tool-less worker call is served (auxiliary path of the script backend) and charged to the job worker counter
	mustStatus(t, h.do("POST", "/anthropic/v1/messages", h.svcToken, msgBody("anthropic/claude-sonnet-5-5", false, 1, false), hdr), 200)
	ev := h.usage.all()
	last := ev[len(ev)-1]
	if last.Caller.Kind != "service" || last.Caller.ServicePrincipal != "svc_replicant-worker" || last.Purpose != "worker_llm" {
		t.Fatalf("%+v", last)
	}
	if _, spent, _ := h.srv.State.CounterState("fgw:job:job_01m5r7ap8fwrbbsw819jn8ptwm:worker_spent"); spent == 0 {
		t.Fatal("worker spend not charged")
	}
}

func TestBudgetAccounts(t *testing.T) {
	h, _ := scriptHarness(t)
	m := h.mint(runOpts{})
	acct := map[string]any{"account": "fgw:eval:evr_1", "cap_usd": 400}
	mustStatus(t, h.do("PUT", "/internal/v1/budgets/jobs/"+m.jobID, h.svcToken, acct, nil), 403) // worker may not bind
	admin := "admin-" + h.svcToken
	mustStatus(t, h.do("PUT", "/internal/v1/budgets/jobs/"+m.jobID, admin, acct, nil), 200)
	mustStatus(t, h.do("PUT", "/internal/v1/budgets/jobs/"+m.jobID, admin, acct, nil), 200)
	other := h.mint(runOpts{})
	if out := mustStatus(t, h.do("PUT", "/internal/v1/budgets/jobs/"+other.jobID, admin, map[string]any{"account": "fgw:eval:evr_1", "cap_usd": 1}, nil), 409); out["replicant_code"] != "account_cap_conflict" {
		t.Fatal(out)
	}
	mustStatus(t, h.policy(m, &Stub{Root: "scripts", Set: "set1", Key: "implementer"}), 200)
	mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil), 200)
	ev := h.usage.all()
	last := ev[len(ev)-1]
	if last.EvaluationMode != "eval" || last.BudgetAccount != "fgw:eval:evr_1" {
		t.Fatalf("%+v", last)
	}
	if _, spent, _ := h.srv.State.CounterState("fgw:acct:fgw:eval:evr_1"); spent == 0 {
		t.Fatal("account not charged")
	}
}

func TestModelsAndHealth(t *testing.T) {
	h := newHarness(t, "script", "", nil)
	out := mustStatus(t, h.do("GET", "/internal/v1/models", h.svcToken, nil, nil), 200)
	ms := out["models"].([]any)
	if len(ms) != 3 {
		t.Fatal(out)
	}
	mustStatus(t, h.do("GET", "/healthz", "", nil, nil), 200)
	r := mustStatus(t, h.do("GET", "/readyz", "", nil, nil), 200)
	if r["ready"] != true {
		t.Fatal(r)
	}
	b, _ := json.Marshal(r)
	if strings.Contains(string(b), h.svcToken) {
		t.Fatal("secret in readyz")
	}
}

func TestUsdFormatting(t *testing.T) {
	for in, want := range map[int64]string{0: "0.000000", 4500: "0.004500", 15_000_000: "15.000000", 1_234_567: "1.234567"} {
		if got := usd6(in); got != want {
			t.Fatalf("%d: %s", in, got)
		}
	}
}

func TestSharedLocalServiceToken(t *testing.T) {
	h := newHarness(t, "script", "", nil)
	writeFile(t, h.dir+"/svc.yaml", "\"shared-tok\": \"svc_replicant-services\"\n")
	m := h.mint(runOpts{})
	h.svcToken = "shared-tok"
	mustStatus(t, h.policy(m, nil), 200)                                                                                                                     // worker right
	mustStatus(t, h.do("POST", "/internal/v1/revocations", "shared-tok", map[string]any{"run_ids": []string{m.runID}, "reason": "job_cancelled"}, nil), 204) // control-plane-api right
	mustStatus(t, h.do("PUT", "/internal/v1/runs/"+m.runID+"/policy", "unknown-tok", map[string]any{}, nil), 401)
}
