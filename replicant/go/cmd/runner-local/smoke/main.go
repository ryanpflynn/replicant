// Command smoke is runner-local's component smoke test and Runner contract-case driver (test-strategy.md §5.3,
// the cases that apply to M1). It plays the workflow side (seed registry, stage inputs, sign the run grant,
// register the gateway run policy, createRun/poll), hosts contract fakes for the core grants host, Gitea and
// github-token-broker, and a Temporal probe workflow that records run.status_changed Signals.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/httpx"
	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/jcsx"
	"example.com/replicant/internal/runner/jose"
	"example.com/replicant/internal/runner/s3x"
)

type env struct {
	runner, runnerSbx, shim, shimToken, workerToken, readerToken string
	s3                                                           *s3x.Client
	org, envName                                                 string
	grants                                                       *fakeGrants
	gitea                                                        *fakeGitea
	broker                                                       *fakeBroker
	rb                                                           string
	agentRef, envRef, badEnvRef                                  api.DefinitionRef
	agentSpec                                                    map[string]any
	envSpec                                                      map[string]any
	probe                                                        *probe
	stateDir                                                     string
	lastSandboxToken                                             string
	lastDiff                                                     []byte
	mu                                                           sync.Mutex
	results                                                      []caseResult
	target                                                       string // isolated | v0
}

type caseResult struct {
	ID, Name string
	Pass     bool
	Detail   string
}

func main() {
	mode := flag.String("mode", "all", "smoke | contract | all")
	only := flag.String("only", "", "comma-separated case IDs")
	flag.Parse()
	e := &env{
		runner: httpx.Env("RUNNER_URL", "http://run-runner:8480"), runnerSbx: httpx.Env("RUNNER_SANDBOX_URL", "http://run-runner:8490"),
		shim: httpx.Env("SHIM_URL", "http://run-shim:8420"), org: "local", envName: "local",
		stateDir: httpx.Env("RUNNER_STATE_DIR", ""),
	}
	e.workerToken, _ = httpx.ReadToken(httpx.Env("WORKER_TOKEN_FILE", "/secrets/worker_token"))
	e.readerToken, _ = httpx.ReadToken(httpx.Env("READER_TOKEN_FILE", "/secrets/reader_token"))
	e.shimToken, _ = httpx.ReadToken(httpx.Env("SHIM_TOKEN_FILE", "/secrets/shim_token"))
	e.s3 = s3x.New(s3x.Config{Endpoint: httpx.Env("S3_ENDPOINT", "http://run-s3:8333"), AccessKey: httpx.Env("S3_ACCESS_KEY", "admin"),
		SecretKey: httpx.Env("S3_SECRET_KEY", "")})
	signer := jose.GenerateSigner("rg-smoke-1")
	if p := os.Getenv("GRANT_KEY_FILE"); p != "" {
		if s, err := jose.LoadSigner(p, "rg-local-1"); err == nil {
			signer = s
		}
	}
	e.grants = &fakeGrants{signer: signer}
	e.gitea = &fakeGitea{root: "/tmp/gitea-repos", owner: "acme-payments", name: "ledger-api", id: 1, token: "gitea-upstream-token"}
	e.broker = &fakeBroker{callerToken: httpx.Env("BROKER_CALLER_TOKEN", "broker-mirror-token"), token: e.gitea.token}
	e.target = httpx.Env("SMOKE_TARGET", "isolated")
	if e.target == "v0" {
		// v0 mode (M3): the live v0 stack is the neighbourhood (run-grants, Gitea, broker, repo-mirror, shim, sbx-edge,
		// sbx-dns, egress proxy, mirrors). Runs target the real acme-payments/ledger-api at its current main.
		sha, err := giteaMainSHA(httpx.Env("GITEA_URL", "http://gitea:3300"), httpx.Env("GITEA_ADMIN_PASSWORD_FILE", ""))
		if err != nil {
			fatal("gitea main sha: %v", err)
		}
		e.gitea.baseSHA = sha
	} else if err := e.gitea.setup(httpx.Env("SAMPLE_REPO", "/src/dev/sample-repos/ledger-api")); err != nil {
		fatal("sample repo: %v", err)
	}
	serve(":8403", e.grants.handler())
	serve(":3300", e.gitea.handler())
	serve(":8081", e.broker.handler())
	fmt.Printf("fakes up: grants :8403, gitea :3300 (base_sha %s), broker :8081\n", e.gitea.baseSHA)
	ctx := context.Background()
	if addr := os.Getenv("TEMPORAL_ADDRESS"); addr != "" {
		p, err := startProbe(addr, httpx.Env("TEMPORAL_NAMESPACE", "replicant-local"))
		if err != nil {
			fmt.Println("temporal probe unavailable:", err)
		} else {
			e.probe = p
		}
	}
	if err := e.seed(ctx); err != nil {
		fatal("seed: %v", err)
	}
	e.waitReady(ctx)
	want := map[string]bool{}
	for _, c := range httpx.List(*only) {
		want[c] = true
	}
	for _, c := range allCases() {
		if len(want) > 0 && !want[c.id] {
			continue
		}
		if *mode == "smoke" && !c.smoke {
			continue
		}
		if *mode == "contract" && c.smoke && !c.contract {
			continue
		}
		if (e.target == "v0" && isolatedOnly[c.id]) || (e.target != "v0" && v0Only[c.id]) {
			continue
		}
		fmt.Printf("\n=== %s %s\n", c.id, c.name)
		start := time.Now()
		detail, err := c.fn(ctx, e)
		r := caseResult{ID: c.id, Name: c.name, Pass: err == nil, Detail: detail}
		if err != nil {
			r.Detail = err.Error()
		}
		fmt.Printf("--- %s %s (%.0fs): %s\n", map[bool]string{true: "PASS", false: "FAIL"}[r.Pass], c.id, time.Since(start).Seconds(), r.Detail)
		e.results = append(e.results, r)
	}
	fmt.Println("\n=== summary")
	pass := 0
	for _, r := range e.results {
		mark := "FAIL"
		if r.Pass {
			mark, pass = "PASS", pass+1
		}
		fmt.Printf("%-4s %-12s %s\n", mark, r.ID, r.Name)
	}
	fmt.Printf("%d/%d passed\n", pass, len(e.results))
	if b, err := json.MarshalIndent(e.results, "", " "); err == nil {
		_ = os.WriteFile(httpx.Env("RESULTS_FILE", "/tmp/smoke-results.json"), b, 0o644)
	}
	if pass != len(e.results) {
		os.Exit(1)
	}
}

// giteaMainSHA returns the ROOT commit of main of acme-payments/ledger-api in the real Gitea (v0 mode): the
// imported sample with the planted bug, whatever fixes earlier E2E jobs merged since (repo-mirror fetches any
// reachable SHA).
func giteaMainSHA(base, pwFile string) (string, error) {
	pw, err := httpx.ReadToken(pwFile)
	if err != nil {
		return "", err
	}
	req, _ := http.NewRequest("GET", base+"/api/v1/repos/acme-payments/ledger-api/commits?sha=main&limit=50&stat=false&files=false", nil)
	req.SetBasicAuth("gitea-admin", pw)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var out []struct {
		SHA     string `json:"sha"`
		Parents []any  `json:"parents"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil || len(out) == 0 {
		return "", fmt.Errorf("status %d: %v", resp.StatusCode, err)
	}
	for _, c := range out {
		if len(c.Parents) == 0 {
			return c.SHA, nil
		}
	}
	return out[len(out)-1].SHA, nil
}

func fatal(f string, a ...any) {
	fmt.Fprintf(os.Stderr, "FATAL: "+f+"\n", a...)
	os.Exit(2)
}

func (e *env) bucket(class string) string { return s3x.Bucket(e.org, e.envName, class) }

func jcsDigest(v any) ([]byte, string) {
	b, _ := json.Marshal(v)
	c, _ := jcsx.Canonical(b)
	return c, "sha256:" + s3x.Sha256Hex(c)
}

// seed creates the buckets and publishes the agent/environment definitions, the bundle and its pointer.
func (e *env) seed(ctx context.Context) error {
	for i := 0; ; i++ {
		_, err := e.s3.S3.ListBuckets(ctx, nil)
		if err == nil {
			break
		}
		if i > 60 {
			return fmt.Errorf("s3 not reachable: %v", err)
		}
		time.Sleep(time.Second)
	}
	for _, c := range []string{"artifacts", "transcripts", "session", "registry", "ops"} {
		_ = createBucket(ctx, e.s3, e.bucket(c))
	}
	e.agentSpec = agentSpec()
	agentEnv := map[string]any{"apiVersion": "replicant.example.com/v1", "kind": "AgentDefinition",
		"metadata": map[string]any{"name": "implementer-claude", "scope": map[string]any{"level": "global"}, "owners": []string{"replicant-platform"}},
		"spec":     e.agentSpec}
	e.envSpec = envSpec("localhost/replicant/env-go-service:dev")
	envEnv := map[string]any{"apiVersion": "replicant.example.com/v1", "kind": "EnvironmentDefinition",
		"metadata": map[string]any{"name": "go-service", "scope": map[string]any{"level": "global"}, "owners": []string{"replicant-platform"}},
		"spec":     e.envSpec}
	badEnv := map[string]any{"apiVersion": "replicant.example.com/v1", "kind": "EnvironmentDefinition",
		"metadata": map[string]any{"name": "go-service-missing-image", "scope": map[string]any{"level": "global"}, "owners": []string{"replicant-platform"}},
		"spec":     envSpec("localhost/replicant/env-does-not-exist@sha256:" + strings.Repeat("0", 64))}
	ab, ad := jcsDigest(agentEnv)
	eb, ed := jcsDigest(envEnv)
	bb2, bd := jcsDigest(badEnv)
	for d, b := range map[string][]byte{ad: ab, ed: eb, bd: bb2} {
		if err := e.s3.PutWriteOnceIdempotent(ctx, e.bucket("registry"), "blobs/sha256/"+strings.TrimPrefix(d, "sha256:")+".json", b,
			s3x.PutOptions{ContentType: "application/json"}); err != nil {
			return err
		}
	}
	e.agentRef = api.DefinitionRef{Kind: "agent", Name: "implementer-claude", Version: 1, Digest: ad}
	e.envRef = api.DefinitionRef{Kind: "environment", Name: "go-service", Version: 1, Digest: ed}
	e.badEnvRef = api.DefinitionRef{Kind: "environment", Name: "go-service-missing-image", Version: 1, Digest: bd}
	spec := map[string]any{
		"agents":       []map[string]string{{"ref": "agent/implementer-claude@1", "digest": ad}},
		"environments": []map[string]string{{"ref": "environment/go-service@1", "digest": ed}, {"ref": "environment/go-service-missing-image@1", "digest": bd}},
		"policies":     []any{},
	}
	// C-DEF (CONTROL-06): the bundle blob is JCS(spec); its digest names the blob and gives rb_<b32_130(digest)>.
	bb, bdig := jcsDigest(spec)
	sum := sha256sum(bb)
	e.rb = "rb_" + ids.B32_130(sum)
	if err := e.s3.PutWriteOnceIdempotent(ctx, e.bucket("registry"), "blobs/sha256/"+strings.TrimPrefix(bdig, "sha256:")+".json", bb,
		s3x.PutOptions{ContentType: "application/json"}); err != nil {
		return err
	}
	ptr, _ := json.Marshal(map[string]string{"release_bundle_id": e.rb, "digest": bdig})
	if err := e.s3.PutWriteOnceIdempotent(ctx, e.bucket("registry"), "bundles/"+e.rb+".json", ptr, s3x.PutOptions{ContentType: "application/json"}); err != nil {
		return err
	}
	fmt.Println("registry seeded: bundle", e.rb)
	return nil
}

func (e *env) waitReady(ctx context.Context) {
	for i := 0; i < 90; i++ {
		resp, err := http.Get(e.runner + "/readyz")
		if err == nil {
			b, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			if resp.StatusCode == 200 {
				fmt.Println("runner-local ready")
				return
			}
			if i%10 == 0 {
				fmt.Println("runner-local not ready:", strings.TrimSpace(string(b)))
			}
		}
		time.Sleep(2 * time.Second)
	}
	fatal("runner-local never became ready")
}

// do sends an orchestration-listener request.
func (e *env) do(method, path, token, idem string, body any) (int, http.Header, map[string]any) {
	var rd io.Reader
	if body != nil {
		switch b := body.(type) {
		case []byte:
			rd = bytes.NewReader(b)
		default:
			jb, _ := json.Marshal(body)
			rd = bytes.NewReader(jb)
		}
	}
	req, _ := http.NewRequest(method, e.runner+path, rd)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if idem != "" {
		req.Header.Set("Idempotency-Key", idem)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return 0, nil, map[string]any{"error": err.Error()}
	}
	defer resp.Body.Close()
	var out map[string]any
	b, _ := io.ReadAll(resp.Body)
	_ = json.Unmarshal(b, &out)
	if out == nil {
		out = map[string]any{"raw": string(b)}
	}
	return resp.StatusCode, resp.Header, out
}

func keysOf(m map[string]any) []string {
	var k []string
	for x := range m {
		k = append(k, x)
	}
	sort.Strings(k)
	return k
}
