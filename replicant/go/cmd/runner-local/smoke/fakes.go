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
)

// Contract fakes for the neighbours runner-local's smoke needs when the v0 stack is not up:
// core (the run-scope intake record and the job revocation list runner-local polls, SIMPLIFY-D50), a
// Gitea-shaped upstream (repo API + smart HTTP) and the github-token-broker. They implement exactly the contract
// shapes (control-plane-api GET /internal/v1/jobs/{job_id}/run-scope and GET /internal/v1/revocations,
// github-token-broker.openapi.yaml TokenResponse, Gitea GET /api/v1/repos/{o}/{n}).

type coreJob struct {
	TenantID  string   `json:"tenant_id"`
	RepoIDs   []string `json:"repo_ids"`
	ActingFor string   `json:"acting_for"`
	Revoked   bool     `json:"revoked"`
}

type fakeCore struct {
	mu   sync.Mutex
	jobs map[string]*coreJob
}

// addJob records the intake record of a job the driver stages (the worker's job exists in core for real).
func (c *fakeCore) addJob(jobID, tenant string, repoIDs []string, actingFor string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.jobs == nil {
		c.jobs = map[string]*coreJob{}
	}
	if _, ok := c.jobs[jobID]; !ok {
		c.jobs[jobID] = &coreJob{TenantID: tenant, RepoIDs: repoIDs, ActingFor: actingFor}
	}
}

func (c *fakeCore) revokeJob(id string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if j := c.jobs[id]; j != nil {
		j.Revoked = true
	}
}

func (c *fakeCore) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /internal/v1/jobs/{job_id}/run-scope", func(w http.ResponseWriter, r *http.Request) {
		c.mu.Lock()
		defer c.mu.Unlock()
		id := r.PathValue("job_id")
		j := c.jobs[id]
		if j == nil || j.TenantID != r.URL.Query().Get("tenant_id") {
			writeJSON(w, 404, map[string]any{"code": "not_found"})
			return
		}
		writeJSON(w, 200, map[string]any{"job_id": id, "tenant_id": j.TenantID, "repo_ids": j.RepoIDs, "acting_for": j.ActingFor, "revoked": j.Revoked})
	})
	mux.HandleFunc("GET /internal/v1/revocations", func(w http.ResponseWriter, r *http.Request) {
		c.mu.Lock()
		defer c.mu.Unlock()
		type ent struct {
			ID        string `json:"id"`
			RevokedAt string `json:"revoked_at"`
		}
		js := []ent{}
		for id, j := range c.jobs {
			if j.Revoked {
				js = append(js, ent{id, time.Now().UTC().Format(time.RFC3339)})
			}
		}
		writeJSON(w, 200, map[string]any{"generated_at": time.Now().UTC().Format(time.RFC3339), "job_ids": js, "run_ids": []ent{}})
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
