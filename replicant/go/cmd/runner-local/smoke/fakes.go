package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/http/cgi"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"example.com/replicant/internal/runner/jose"
)

// Contract fakes for the neighbours runner-local's smoke needs when the v0 stack is not up:
// the core grants host (JWKS + /grants/revoked), a Gitea-shaped upstream (repo API + smart HTTP) and the
// github-token-broker. They implement exactly the contract shapes (control-plane-api.openapi.yaml
// grantRevocationList, github-token-broker.openapi.yaml TokenResponse, Gitea GET /api/v1/repos/{o}/{n}).

type fakeGrants struct {
	signer *jose.Signer
	mu     sync.Mutex
	jobs   []string
	runs   []string
}

func (g *fakeGrants) revokeJob(id string) { g.mu.Lock(); g.jobs = append(g.jobs, id); g.mu.Unlock() }

func (g *fakeGrants) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /.well-known/jwks.json", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, jose.JWKS{Keys: []jose.JWK{g.signer.PublicJWK()}})
	})
	mux.HandleFunc("GET /grants/revoked", func(w http.ResponseWriter, r *http.Request) {
		g.mu.Lock()
		defer g.mu.Unlock()
		type ent struct {
			ID        string `json:"id"`
			RevokedAt string `json:"revoked_at"`
		}
		out := map[string]any{"generated_at": time.Now().UTC().Format(time.RFC3339), "job_ids": []ent{}, "run_ids": []ent{}}
		var js, rs []ent
		for _, j := range g.jobs {
			js = append(js, ent{j, time.Now().UTC().Format(time.RFC3339)})
		}
		for _, j := range g.runs {
			rs = append(rs, ent{j, time.Now().UTC().Format(time.RFC3339)})
		}
		if js != nil {
			out["job_ids"] = js
		}
		if rs != nil {
			out["run_ids"] = rs
		}
		writeJSON(w, 200, out)
	})
	return mux
}

// fakeGitea serves one org/repo with numeric id 1 over smart HTTP (git http-backend) and the repo API.
type fakeGitea struct {
	root    string // directory holding <owner>/<name>.git
	owner   string
	name    string
	id      int
	token   string
	baseSHA string
}

func (f *fakeGitea) setup(sample string) error {
	work := filepath.Join(os.TempDir(), "sample-work")
	_ = os.RemoveAll(work)
	if err := exec.Command("cp", "-r", sample, work).Run(); err != nil {
		return fmt.Errorf("copy sample: %w", err)
	}
	_ = os.RemoveAll(filepath.Join(work, "ISSUES"))
	run := func(dir string, args ...string) (string, error) {
		cmd := exec.Command("git", args...)
		cmd.Dir = dir
		cmd.Env = append(os.Environ(), "GIT_AUTHOR_NAME=replicant-bot", "GIT_AUTHOR_EMAIL=bot@replicant.invalid", "GIT_COMMITTER_NAME=replicant-bot",
			"GIT_COMMITTER_EMAIL=bot@replicant.invalid", "GIT_AUTHOR_DATE=2026-10-24T00:00:00Z", "GIT_COMMITTER_DATE=2026-10-24T00:00:00Z")
		out, err := cmd.CombinedOutput()
		return strings.TrimSpace(string(out)), err
	}
	for _, a := range [][]string{{"init", "-q", "-b", "main"}, {"add", "-A"}, {"commit", "-q", "-m", "initial ledger-api"}} {
		if out, err := run(work, a...); err != nil {
			return fmt.Errorf("git %v: %v %s", a, err, out)
		}
	}
	sha, err := run(work, "rev-parse", "HEAD")
	if err != nil {
		return err
	}
	f.baseSHA = sha
	bare := filepath.Join(f.root, f.owner, f.name+".git")
	_ = os.RemoveAll(bare)
	_ = os.MkdirAll(filepath.Dir(bare), 0o755)
	if out, err := run(f.root, "clone", "-q", "--bare", work, bare); err != nil {
		return fmt.Errorf("bare clone: %v %s", err, out)
	}
	_, _ = run(bare, "config", "http.receivepack", "false")
	_, _ = run(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
	_, _ = run(bare, "config", "uploadpack.allowFilter", "true")
	return nil
}

// commitBranch commits files (path → content) on top of the base commit as branch `branch` of the served repo and
// returns the new commit; main is unchanged. Used by RC-REPO-CFG for a repo that carries Claude Code configuration.
func (f *fakeGitea) commitBranch(branch, msg string, files map[string]string) (string, error) {
	work := filepath.Join(os.TempDir(), "sample-work")
	run := func(args ...string) (string, error) {
		cmd := exec.Command("git", args...)
		cmd.Dir = work
		cmd.Env = append(os.Environ(), "GIT_AUTHOR_NAME=replicant-bot", "GIT_AUTHOR_EMAIL=bot@replicant.invalid", "GIT_COMMITTER_NAME=replicant-bot",
			"GIT_COMMITTER_EMAIL=bot@replicant.invalid", "GIT_AUTHOR_DATE=2026-10-24T00:00:00Z", "GIT_COMMITTER_DATE=2026-10-24T00:00:00Z")
		out, err := cmd.CombinedOutput()
		if err != nil {
			return "", fmt.Errorf("git %v: %v %s", args, err, out)
		}
		return strings.TrimSpace(string(out)), nil
	}
	if _, err := run("checkout", "-q", "-B", branch, f.baseSHA); err != nil {
		return "", err
	}
	defer func() { _, _ = run("checkout", "-q", "main") }()
	for p, c := range files {
		full := filepath.Join(work, p)
		_ = os.MkdirAll(filepath.Dir(full), 0o755)
		if err := os.WriteFile(full, []byte(c), 0o644); err != nil {
			return "", err
		}
	}
	if _, err := run("add", "-A"); err != nil {
		return "", err
	}
	if _, err := run("commit", "-q", "-m", msg); err != nil {
		return "", err
	}
	sha, err := run("rev-parse", "HEAD")
	if err != nil {
		return "", err
	}
	if _, err := run("push", "-q", "-f", filepath.Join(f.root, f.owner, f.name+".git"), branch); err != nil {
		return "", err
	}
	return sha, nil
}

func (f *fakeGitea) handler() http.Handler {
	backend := "/usr/lib/git-core/git-http-backend"
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		authOK := false
		if _, p, ok := r.BasicAuth(); ok && p == f.token {
			authOK = true
		}
		if r.Header.Get("Authorization") == "token "+f.token {
			authOK = true
		}
		if !authOK {
			w.Header().Set("WWW-Authenticate", `Basic realm="gitea"`)
			http.Error(w, "unauthorized", 401)
			return
		}
		if r.URL.Path == "/api/v1/repos/"+f.owner+"/"+f.name {
			writeJSON(w, 200, map[string]any{"id": f.id, "full_name": f.owner + "/" + f.name, "default_branch": "main"})
			return
		}
		if strings.HasPrefix(r.URL.Path, "/api/") {
			http.NotFound(w, r)
			return
		}
		h := &cgi.Handler{Path: backend, Root: "/", Env: []string{"GIT_PROJECT_ROOT=" + f.root, "GIT_HTTP_EXPORT_ALL=1"},
			InheritEnv: []string{"PATH"}}
		h.ServeHTTP(w, r)
	})
}

type fakeBroker struct {
	callerToken, token string
	calls              int
	mu                 sync.Mutex
}

func (b *fakeBroker) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/installation-tokens", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+b.callerToken {
			writeJSON(w, 403, map[string]any{"title": "caller not allowed", "status": 403, "code": "caller_not_allowed"})
			return
		}
		var req map[string]any
		_ = json.NewDecoder(r.Body).Decode(&req)
		b.mu.Lock()
		b.calls++
		b.mu.Unlock()
		writeJSON(w, 200, map[string]any{"token": b.token, "expires_at": time.Now().Add(time.Hour).UTC().Format(time.RFC3339),
			"permissions": req["permissions"], "repository_ids": req["repository_ids"], "cached": false})
	})
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })
	return mux
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func serve(addr string, h http.Handler) {
	go func() {
		if err := http.ListenAndServe(addr, h); err != nil {
			log.Fatalf("listen %s: %v", addr, err)
		}
	}()
}
