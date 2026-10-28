package shim

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"example.com/replicant/cmd/replicant-gateway-shim/internal/usage"
	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/jose"
)

const testConfig = `
listen: 127.0.0.1:0
routes:
  anthropic: {prefix: /anthropic, upstream: UPSTREAM, protocol: anthropic_messages}
  openai: {prefix: /openai, upstream: UPSTREAM, protocol: openai_responses}
  embeddings: {prefix: /embeddings, upstream_by_model: {voyage: UPSTREAM, openai: UPSTREAM}, protocol: embeddings}
model_map:
  anthropic/claude-sonnet-5-5: claude-sonnet-5-5
  anthropic/claude-haiku-5-5: claude-haiku-5-5
  openai/gpt-5-codex-2025-09-15: gpt-5-codex
price_book: {source: file, path: PRICES}
auth:
  run_token: {audience: ai-gateway, issuers_env: REPLICANT_RUN_TOKEN_ISSUERS, jwks_urls_env: REPLICANT_RUN_TOKEN_JWKS_URLS}
  run_grant: {issuer_env: REPLICANT_RUN_GRANT_ISSUER, jwks_url_env: REPLICANT_RUN_GRANT_JWKS_URL, revocation_url_env: REPLICANT_RUN_GRANT_REVOCATION_URL}
  service_tokens_file_env: REPLICANT_GATEWAY_SERVICE_TOKENS_FILE
budgets:
  dev_defaults: {run_usd: 2, job_usd: 5}
  embeddings: {per_tenant_day_usd: 1, reembed_backfill_usd_max: 2}
local:
  cassette_roots_env: REPLICANT_GATEWAY_CASSETTE_ROOTS
  miss: {status: 599, code: cassette_miss}
`

const testPrices = `
anthropic/claude-sonnet-5-5: {input: 3.00, output: 15.00, cache_read: 0.30, cache_write: 3.75}
anthropic/claude-haiku-5-5: {input: 1.00, output: 5.00}
openai/gpt-5-codex-2025-09-15: {input: 1.25, output: 10.00, cache_read: 0.125}
`

type memUsage struct {
	mu     sync.Mutex
	events []usage.Event
}

func (m *memUsage) Append(e usage.Event) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.events = append(m.events, e)
	return nil
}
func (m *memUsage) Healthy() bool { return true }
func (m *memUsage) all() []usage.Event {
	m.mu.Lock()
	defer m.mu.Unlock()
	return append([]usage.Event(nil), m.events...)
}

type harness struct {
	t        *testing.T
	dir      string
	runKey   *ecdsa.PrivateKey
	grantKey *ecdsa.PrivateKey
	svcToken string
	cfg      *Config
	srv      *Server
	http     *httptest.Server
	usage    *memUsage
}

type runOpts struct {
	aud         any
	budget      float64
	role        string
	grantKey    *ecdsa.PrivateKey
	grantBudget float64
	scopes      []string
	exp         time.Duration
	jobID       string
}

type minted struct {
	token  string
	runID  string
	jobID  string
	tenant string
	role   string
	budget float64
}

func writeFile(t *testing.T, p, s string) {
	t.Helper()
	if err := os.WriteFile(p, []byte(s), 0o600); err != nil {
		t.Fatal(err)
	}
}

// newHarness builds a shim with file:// JWKS, a static service token, local quota and the given backend.
func newHarness(t *testing.T, backend, upstream string, env map[string]string) *harness {
	t.Helper()
	h := &harness{t: t, dir: t.TempDir(), usage: &memUsage{}}
	h.runKey, _ = ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	h.grantKey, _ = ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	jw := func(name string, k *ecdsa.PrivateKey, kid string) string {
		b, _ := json.Marshal(jose.JWKS{Keys: []jose.JWK{jose.PublicJWK(&k.PublicKey, kid)}})
		p := filepath.Join(h.dir, name)
		writeFile(t, p, string(b))
		return "file://" + p
	}
	h.svcToken = "svc-" + ids.Body(ids.ULID(""))
	writeFile(t, filepath.Join(h.dir, "svc.yaml"), h.svcToken+": system:serviceaccount:replicant-workers:replicant-worker\nadmin-"+h.svcToken+": svc_control-plane-api\n")
	scripts := filepath.Join(h.dir, "scripts")
	_ = os.MkdirAll(scripts, 0o755)
	if upstream == "" {
		upstream = "http://127.0.0.1:1"
	}
	writeFile(t, filepath.Join(h.dir, "prices.yaml"), testPrices)
	cfgText := strings.ReplaceAll(strings.ReplaceAll(testConfig, "UPSTREAM", upstream), "PRICES", filepath.Join(h.dir, "prices.yaml"))
	if backend == "enterprise" {
		cfgText = cfgText[:strings.Index(cfgText, "local:")]
	}
	writeFile(t, filepath.Join(h.dir, "gw.yaml"), cfgText)
	base := map[string]string{
		"REPLICANT_RUN_TOKEN_ISSUERS":           "http://runner-local:8480",
		"REPLICANT_RUN_TOKEN_JWKS_URLS":         jw("runner.json", h.runKey, "r1"),
		"REPLICANT_RUN_GRANT_ISSUER":            "replicant-local-core",
		"REPLICANT_RUN_GRANT_JWKS_URL":          jw("grant.json", h.grantKey, "g1"),
		"REPLICANT_RUN_GRANT_REVOCATION_URL":    "",
		"REPLICANT_EXEC_REVOCATIONS_URLS":       "",
		"REPLICANT_GATEWAY_SERVICE_TOKENS_FILE": filepath.Join(h.dir, "svc.yaml"),
		"REPLICANT_GATEWAY_CASSETTE_ROOTS":      "scripts=" + scripts,
		"REPLICANT_GATEWAY_LOCAL_QUOTA":         "monthly=100,daily=50,platform_daily=500,platform_monthly=2000",
		"REPLICANT_CONTROL_PLANE_URL":           "",
		"REPLICANT_GATEWAY_SCRIPT_DEFAULT_SET":  "",
		"ANTHROPIC_API_KEY":                     "",
		"OPENAI_API_KEY":                        "",
	}
	for k, v := range env {
		base[k] = v
	}
	for k, v := range base {
		t.Setenv(k, v)
	}
	cfg, err := LoadConfig(filepath.Join(h.dir, "gw.yaml"), backend, "sequence", "local")
	if err != nil {
		t.Fatal(err)
	}
	h.cfg = cfg
	st := NewMemState()
	var be Backend
	switch backend {
	case "script":
		be = NewScriptBackend(cfg, st)
	default:
		be = NewPassthroughBackend(cfg)
	}
	h.srv = &Server{Cfg: cfg, State: st, Auth: NewAuthenticator(cfg), Quotas: NewQuotaClient(cfg), Backend: be, Usage: h.usage, Feeds: NewFeeds(cfg, st, nil)}
	h.http = httptest.NewServer(h.srv.Handler())
	t.Cleanup(h.http.Close)
	return h
}

func (h *harness) script(set, key, body string) {
	h.t.Helper()
	d := filepath.Join(h.cfg.CassetteRoots["scripts"], set)
	_ = os.MkdirAll(d, 0o755)
	writeFile(h.t, filepath.Join(d, key+".yaml"), body)
}

func (h *harness) mint(o runOpts) minted {
	h.t.Helper()
	if o.aud == nil {
		o.aud = "ai-gateway"
	}
	if o.budget == 0 {
		o.budget = 15
	}
	if o.grantBudget == 0 {
		o.grantBudget = o.budget
	}
	if o.role == "" {
		o.role = "implementer"
	}
	if o.grantKey == nil {
		o.grantKey = h.grantKey
	}
	if o.scopes == nil {
		o.scopes = []string{"gateway:invoke"}
	}
	if o.exp == 0 {
		o.exp = time.Hour
	}
	m := minted{runID: ids.ULID("run"), jobID: o.jobID, tenant: "acme-payments", role: o.role, budget: o.budget}
	if m.jobID == "" {
		m.jobID = ids.ULID("job")
	}
	now := time.Now()
	gj, af := ids.ULID("rgr"), ids.ULID("usr")
	grant, err := (&jose.Signer{Key: o.grantKey, Kid: "g1"}).Sign("replicant-run-grant+jwt", map[string]any{
		"iss": "replicant-local-core", "typ": "replicant-run-grant+jwt", "jti": gj, "iat": now.Unix(), "exp": now.Add(2 * time.Hour).Unix(),
		"tenant_id": m.tenant, "job_id": m.jobID, "run_id": m.runID, "role": o.role, "repo_ids": []string{"r1"},
		"audiences": []string{"ai-gateway"}, "scopes": []string{"gateway:invoke"}, "budget_usd": o.grantBudget, "acting_for": af,
		"spec_digest": "sha256:" + strings.Repeat("0", 64)})
	if err != nil {
		h.t.Fatal(err)
	}
	m.token, err = (&jose.Signer{Key: h.runKey, Kid: "r1"}).Sign("", map[string]any{
		"iss": "http://runner-local:8480", "sub": "run:" + m.runID, "aud": o.aud, "exp": now.Add(o.exp).Unix(), "iat": now.Unix(),
		"jti": ids.ULID("rtk"), "tenant_id": m.tenant, "job_id": m.jobID, "run_id": m.runID, "role": o.role,
		"release_bundle_id": ids.ULID("rb"), "scopes": o.scopes, "budget_usd": o.budget, "acting_for": af, "grant": grant, "grant_jti": gj})
	if err != nil {
		h.t.Fatal(err)
	}
	return m
}

func (h *harness) do(method, path, token string, body any, hdr map[string]string) *http.Response {
	h.t.Helper()
	var rd *bytes.Reader
	switch b := body.(type) {
	case nil:
		rd = bytes.NewReader(nil)
	case string:
		rd = bytes.NewReader([]byte(b))
	default:
		j, _ := json.Marshal(b)
		rd = bytes.NewReader(j)
	}
	req, _ := http.NewRequest(method, h.http.URL+path, rd)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	req.Header.Set("Content-Type", "application/json")
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	return resp
}

func (h *harness) policy(m minted, stub *Stub, models ...string) *http.Response {
	if len(models) == 0 {
		models = []string{"anthropic/claude-sonnet-5-5", "anthropic/claude-haiku-5-5"}
	}
	p := Policy{TenantID: m.tenant, JobID: m.jobID, Role: m.role, ReleaseBundleID: ids.ULID("rb"), Models: models,
		BudgetUSD: m.budget, MaxConcurrency: 4, ExpiresAt: time.Now().Add(6 * time.Hour).UTC().Format(time.RFC3339), Stub: stub}
	return h.do("PUT", "/internal/v1/runs/"+m.runID+"/policy", h.svcToken, p, nil)
}

func mustStatus(t *testing.T, resp *http.Response, want int) map[string]any {
	t.Helper()
	defer resp.Body.Close()
	var out map[string]any
	b := new(bytes.Buffer)
	_, _ = b.ReadFrom(resp.Body)
	_ = json.Unmarshal(b.Bytes(), &out)
	if resp.StatusCode != want {
		t.Fatalf("status %d, want %d: %s", resp.StatusCode, want, b.String())
	}
	return out
}

func msgBody(model string, stream bool, nmsg int, tools bool) map[string]any {
	var msgs []map[string]any
	for i := 0; i < nmsg; i++ {
		role := "user"
		if i%2 == 1 {
			role = "assistant"
		}
		msgs = append(msgs, map[string]any{"role": role, "content": "m"})
	}
	b := map[string]any{"model": model, "max_tokens": 1000, "stream": stream, "messages": msgs,
		"system": []map[string]any{{"type": "text", "text": "env\n - Primary working directory: /workspace/repo-x\n"}}}
	if tools {
		b["tools"] = []map[string]any{{"name": "Read"}, {"name": "Edit"}, {"name": "Bash"}, {"name": "StructuredOutput"}}
	}
	return b
}
