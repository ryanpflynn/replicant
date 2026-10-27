// Command repo-mirror serves read-only git over smart HTTP (protocol v2, upload-pack only) to sandboxes,
// authenticated by `repo-mirror`-audience run tokens (ADR-02-006, fast-path.md §4.1). Upstream fetches use
// installation tokens from github-token-broker (Gitea tokens locally, parity P-12).
package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/cgi"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/authz"
	"example.com/replicant/internal/runner/httpx"
	"example.com/replicant/internal/runner/jose"
	"example.com/replicant/internal/runner/revoke"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		os.Exit(httpx.Healthcheck(os.Args[2:]))
	}
	log := httpx.Logger("repo-mirror")
	if err := run(log); err != nil {
		log.Error("fatal", "err", err)
		os.Exit(1)
	}
}

type Mirror struct {
	log       *slog.Logger
	cache     string
	verifier  *authz.TokenVerifier
	core      *revoke.Core
	exec      *revoke.Exec
	upstreams map[string]string // provider -> base URL
	brokerURL string
	brokerTok string
	staticTok string // optional MIRROR_UPSTREAM_TOKEN_FILE (dev fallback)
	backend   string

	mu        sync.Mutex
	locks     map[string]*sync.Mutex
	lastFetch map[string]time.Time
	tokens    map[string]brokerToken
	verified  map[string]string // repo_id -> owner/name verified
}

type brokerToken struct {
	Token     string    `json:"token"`
	ExpiresAt time.Time `json:"expires_at"`
}

func run(log *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	core := &revoke.Core{URL: httpx.Env("MIRROR_RUN_GRANT_REVOCATION_URL", ""), Interval: time.Duration(httpx.EnvInt("MIRROR_RUN_GRANT_REVOCATION_POLL_SECONDS", 60)) * time.Second}
	revTok, _ := httpx.ReadToken(httpx.Env("MIRROR_EXEC_REVOCATIONS_TOKEN_FILE", ""))
	ex := &revoke.Exec{URLs: httpx.List(httpx.Env("MIRROR_EXEC_REVOCATIONS_URLS", "")), Token: revTok,
		Interval: time.Duration(httpx.EnvInt("MIRROR_EXEC_REVOCATIONS_POLL_SECONDS", 10)) * time.Second}
	go core.Run(ctx, log)
	go ex.Run(ctx, log)
	grants := &authz.GrantVerifier{Issuer: httpx.Env("MIRROR_RUN_GRANT_ISSUER", "replicant-local-core"),
		JWKS: &jose.RemoteJWKS{URLs: httpx.List(httpx.Env("MIRROR_RUN_GRANT_JWKS_URL", ""))}, Revoked: core, Skew: 30 * time.Second}
	m := &Mirror{
		log:   log,
		cache: httpx.Env("MIRROR_CACHE_DIR", "/var/cache/repo-mirror"),
		verifier: &authz.TokenVerifier{Audience: api.AudMirror, Issuers: httpx.List(httpx.Env("MIRROR_RUN_TOKEN_ISSUERS", "")),
			JWKS: &jose.RemoteJWKS{URLs: httpx.List(httpx.Env("MIRROR_RUN_TOKEN_JWKS_URLS", ""))}, Grants: grants, Revoked: revoke.Multi{core, ex}},
		core: core, exec: ex, upstreams: map[string]string{}, brokerURL: httpx.Env("MIRROR_BROKER_URL", ""),
		locks: map[string]*sync.Mutex{}, lastFetch: map[string]time.Time{}, tokens: map[string]brokerToken{}, verified: map[string]string{},
	}
	m.brokerTok, _ = httpx.ReadToken(httpx.Env("MIRROR_BROKER_TOKEN_FILE", ""))
	m.staticTok, _ = httpx.ReadToken(httpx.Env("MIRROR_UPSTREAM_TOKEN_FILE", ""))
	for _, kv := range httpx.List(httpx.Env("MIRROR_UPSTREAMS", "gitea=http://git.replicant.localhost:3300")) {
		if k, v, ok := strings.Cut(kv, "="); ok {
			m.upstreams[k] = strings.TrimSuffix(v, "/")
		}
	}
	for _, p := range []string{"/usr/lib/git-core/git-http-backend", "/usr/libexec/git-core/git-http-backend"} {
		if _, err := os.Stat(p); err == nil {
			m.backend = p
		}
	}
	if m.backend == "" {
		return errors.New("git-http-backend not found")
	}
	if err := os.MkdirAll(m.cache, 0o755); err != nil {
		return err
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })
	mux.HandleFunc("GET /readyz", m.readyz)
	mux.HandleFunc("/git/", m.serveGit)
	srv := &http.Server{Addr: httpx.Env("MIRROR_LISTEN", "0.0.0.0:8080"), Handler: mux, ReadHeaderTimeout: 30 * time.Second}
	go func() {
		<-ctx.Done()
		sctx, c := context.WithTimeout(context.Background(), 5*time.Second)
		defer c()
		_ = srv.Shutdown(sctx)
	}()
	log.Info("repo-mirror listening", "addr", srv.Addr, "upstreams", m.upstreams)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

func (m *Mirror) readyz(w http.ResponseWriter, r *http.Request) {
	var problems []string
	if _, err := m.verifier.JWKS.Keys(r.Context(), ""); err != nil {
		problems = append(problems, "run-token jwks: "+err.Error())
	}
	if f := m.exec.Fresh(); len(m.exec.URLs) > 0 && time.Since(f) > 60*time.Second {
		problems = append(problems, "exec revocation feed stale")
	}
	if len(problems) > 0 {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(503)
		_ = json.NewEncoder(w).Encode(map[string]any{"ready": false, "problems": problems})
		return
	}
	w.WriteHeader(200)
}

func (m *Mirror) lock(id string) *sync.Mutex {
	m.mu.Lock()
	defer m.mu.Unlock()
	l, ok := m.locks[id]
	if !ok {
		l = &sync.Mutex{}
		m.locks[id] = l
	}
	return l
}

var repoIDRe = regexp.MustCompile(`^[A-Za-z0-9_:.-]{1,64}$`)

func runToken(r *http.Request) string {
	if t := httpx.Bearer(r); t != "" {
		return t
	}
	if _, p, ok := r.BasicAuth(); ok {
		return p
	}
	return ""
}

func (m *Mirror) serveGit(w http.ResponseWriter, r *http.Request) {
	rest := strings.TrimPrefix(r.URL.Path, "/git/")
	repoID, sub, _ := strings.Cut(rest, "/")
	repoID = strings.TrimSuffix(repoID, ".git")
	if strings.Contains(sub, "receive-pack") || r.URL.Query().Get("service") == "git-receive-pack" {
		http.Error(w, "read-only mirror", http.StatusForbidden)
		return
	}
	if !repoIDRe.MatchString(repoID) || !(sub == "info/refs" || sub == "git-upload-pack") {
		http.NotFound(w, r)
		return
	}
	tok := runToken(r)
	if tok == "" {
		w.Header().Set("WWW-Authenticate", `Basic realm="repo-mirror"`)
		http.Error(w, "run token required", http.StatusUnauthorized)
		return
	}
	v, err := m.verifier.Verify(r.Context(), tok)
	if err != nil {
		m.log.Warn("token rejected", "err", err.Error())
		http.Error(w, "invalid run token", http.StatusUnauthorized)
		return
	}
	if !v.HasScope("repo:fetch") || !slices.Contains(v.Claims.RepoIDs, repoID) {
		http.Error(w, "repository not in grant", http.StatusForbidden)
		return
	}
	provider := r.Header.Get("X-Replicant-Repo-Provider")
	if provider == "" {
		provider = "gitea"
	}
	base, ok := m.upstreams[provider]
	if !ok {
		http.Error(w, "unknown provider", http.StatusBadRequest)
		return
	}
	owner, name := r.Header.Get("X-Replicant-Repo-Owner"), r.Header.Get("X-Replicant-Repo-Name")
	m.log.Info("fetch", "repo_id", repoID, "run_id", v.Claims.RunID, "job_id", v.Claims.JobID, "op", sub, "attribution", "token")
	var body []byte
	if r.Method == http.MethodPost {
		body, err = io.ReadAll(io.LimitReader(r.Body, 64<<20))
		if err != nil {
			http.Error(w, "read body", 400)
			return
		}
	}
	if err := m.ensure(r.Context(), repoID, provider, base, owner, name, v.Claims.JobID, wants(body), sub == "info/refs"); err != nil {
		m.log.Warn("upstream sync failed", "repo_id", repoID, "err", err.Error())
		code := http.StatusBadGateway
		if errors.Is(err, errForbidden) {
			code = http.StatusForbidden
		} else if errors.Is(err, errNotFound) {
			code = http.StatusNotFound
		}
		http.Error(w, err.Error(), code)
		return
	}
	env := []string{"GIT_PROJECT_ROOT=" + m.cache, "GIT_HTTP_EXPORT_ALL=1", "GIT_CONFIG_NOSYSTEM=1", "HOME=" + m.cache}
	if gp := r.Header.Get("Git-Protocol"); gp != "" {
		env = append(env, "GIT_PROTOCOL="+gp)
	}
	r2 := r.Clone(r.Context())
	r2.URL.Path = "/" + repoID + ".git/" + sub
	if body != nil {
		r2.Body = io.NopCloser(bytes.NewReader(body))
		r2.ContentLength = int64(len(body))
	}
	h := &cgi.Handler{Path: m.backend, Root: "/", Env: env, InheritEnv: []string{"PATH"}}
	h.ServeHTTP(w, r2)
}

var wantRe = regexp.MustCompile(`want ([0-9a-f]{40}([0-9a-f]{24})?)`)

func wants(body []byte) []string {
	var out []string
	for _, m := range wantRe.FindAllSubmatch(body, -1) {
		out = append(out, string(m[1]))
	}
	return out
}

var (
	errForbidden = errors.New("forbidden")
	errNotFound  = errors.New("not found")
)

// ensure creates/refreshes the bare repo and fetches missing wanted SHAs under a per-repo lock.
func (m *Mirror) ensure(ctx context.Context, repoID, provider, base, owner, name, jobID string, want []string, refresh bool) error {
	l := m.lock(repoID)
	l.Lock()
	defer l.Unlock()
	dir := filepath.Join(m.cache, repoID+".git")
	tok, err := m.upstreamToken(ctx, repoID, jobID)
	if err != nil {
		return err
	}
	m.mu.Lock()
	verified := m.verified[repoID]
	m.mu.Unlock()
	if owner == "" || name == "" {
		if verified == "" {
			return fmt.Errorf("%w: upstream repository unknown for %s", errNotFound, repoID)
		}
		owner, name, _ = strings.Cut(verified, "/")
	}
	if verified != owner+"/"+name {
		if err := m.verifyRepo(ctx, provider, base, owner, name, repoID, tok); err != nil {
			return err
		}
		m.mu.Lock()
		m.verified[repoID] = owner + "/" + name
		m.mu.Unlock()
	}
	upstream := base + "/" + url.PathEscape(owner) + "/" + url.PathEscape(name) + ".git"
	if _, err := os.Stat(filepath.Join(dir, "HEAD")); err != nil {
		if out, err := gitCmd(ctx, "", nil, "init", "--bare", "-q", dir); err != nil {
			return fmt.Errorf("init: %v %s", err, out)
		}
		for _, kv := range [][2]string{{"uploadpack.allowFilter", "true"}, {"uploadpack.allowAnySHA1InWant", "true"},
			{"protocol.version", "2"}, {"repack.writeBitmaps", "true"}, {"core.commitGraph", "true"}, {"http.receivepack", "false"}} {
			_, _ = gitCmd(ctx, dir, nil, "config", kv[0], kv[1])
		}
	}
	_, _ = gitCmd(ctx, dir, nil, "config", "remote.origin.url", upstream)
	auth := []string{"-c", "http.extraHeader=Authorization: Basic " + base64.StdEncoding.EncodeToString([]byte("replicant:"+tok))}
	missing := false
	for _, w := range want {
		if _, err := gitCmd(ctx, dir, nil, "cat-file", "-e", w+"^{commit}"); err != nil {
			missing = true
		}
	}
	m.mu.Lock()
	stale := time.Since(m.lastFetch[repoID]) > 15*time.Second
	m.mu.Unlock()
	if missing || (refresh && stale) {
		if out, err := gitCmd(ctx, dir, auth, "fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"); err != nil {
			return fmt.Errorf("upstream fetch: %v %s", err, scrub(out, tok))
		}
		m.mu.Lock()
		m.lastFetch[repoID] = time.Now()
		m.mu.Unlock()
		for _, w := range want {
			if _, err := gitCmd(ctx, dir, nil, "cat-file", "-e", w+"^{commit}"); err != nil {
				if _, err := gitCmd(ctx, dir, auth, "fetch", "origin", w); err != nil {
					return fmt.Errorf("%w: %s not available upstream", errNotFound, w)
				}
			}
		}
	}
	return nil
}

func scrub(s, tok string) string {
	if tok != "" {
		s = strings.ReplaceAll(s, tok, "[REDACTED]")
	}
	return s
}

func gitCmd(ctx context.Context, dir string, pre []string, args ...string) (string, error) {
	cctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()
	full := append(append([]string{}, pre...), args...)
	cmd := exec.CommandContext(cctx, "git", full...)
	if dir != "" {
		cmd.Dir = dir
	}
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "HOME=/tmp", "GIT_TERMINAL_PROMPT=0", "GIT_CONFIG_NOSYSTEM=1"}
	for _, k := range []string{"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"} {
		if v := os.Getenv(k); v != "" {
			cmd.Env = append(cmd.Env, k+"="+v)
		}
	}
	out, err := cmd.CombinedOutput()
	return string(out), err
}

// upstreamToken returns a read-only upstream token (broker, cached until 10 min before expiry).
func (m *Mirror) upstreamToken(ctx context.Context, repoID, jobID string) (string, error) {
	m.mu.Lock()
	if t, ok := m.tokens[repoID]; ok && time.Until(t.ExpiresAt) > 10*time.Minute {
		m.mu.Unlock()
		return t.Token, nil
	}
	m.mu.Unlock()
	if m.brokerURL == "" {
		if m.staticTok != "" {
			return m.staticTok, nil
		}
		return "", errors.New("no broker configured")
	}
	req := map[string]any{"installation_id": 1, "permissions": map[string]string{"contents": "read", "metadata": "read"}, "purpose": "mirror.fetch"}
	if n, err := strconv.Atoi(repoID); err == nil && n > 0 {
		req["repository_ids"] = []int{n}
	}
	if jobID != "" {
		req["job_id"] = jobID
	}
	b, _ := json.Marshal(req)
	hreq, _ := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimSuffix(m.brokerURL, "/")+"/v1/installation-tokens", bytes.NewReader(b))
	hreq.Header.Set("Content-Type", "application/json")
	if m.brokerTok != "" {
		hreq.Header.Set("Authorization", "Bearer "+m.brokerTok)
	}
	resp, err := (&http.Client{Timeout: 15 * time.Second}).Do(hreq)
	if err != nil {
		if m.staticTok != "" {
			return m.staticTok, nil
		}
		return "", fmt.Errorf("broker: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		rb, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		if m.staticTok != "" {
			return m.staticTok, nil
		}
		if resp.StatusCode == 403 {
			return "", fmt.Errorf("%w: broker refused: %s", errForbidden, rb)
		}
		return "", fmt.Errorf("broker HTTP %d: %s", resp.StatusCode, rb)
	}
	var t brokerToken
	if err := json.NewDecoder(resp.Body).Decode(&t); err != nil || t.Token == "" {
		return "", fmt.Errorf("broker response: %v", err)
	}
	if t.ExpiresAt.IsZero() {
		t.ExpiresAt = time.Now().Add(time.Hour)
	}
	m.mu.Lock()
	m.tokens[repoID] = t
	m.mu.Unlock()
	return t.Token, nil
}

// verifyRepo checks via the upstream API that owner/name really is repo_id (a sandbox cannot map a granted
// id onto another repository).
func (m *Mirror) verifyRepo(ctx context.Context, provider, base, owner, name, repoID, tok string) error {
	if provider != "gitea" {
		return nil // GitHub (M3): the broker token is already restricted to the granted repository ids
	}
	u := base + "/api/v1/repos/" + url.PathEscape(owner) + "/" + url.PathEscape(name)
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	req.Header.Set("Authorization", "token "+tok)
	resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		return fmt.Errorf("upstream api: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == 404 {
		return fmt.Errorf("%w: %s/%s", errNotFound, owner, name)
	}
	if resp.StatusCode != 200 {
		return fmt.Errorf("upstream api HTTP %d", resp.StatusCode)
	}
	var repo struct {
		ID       int64  `json:"id"`
		FullName string `json:"full_name"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&repo); err != nil {
		return err
	}
	id := strconv.FormatInt(repo.ID, 10)
	if repoID == id || repoID == "gitea:"+id || repoID == "gitea-"+id || strings.EqualFold(repoID, repo.FullName) {
		return nil
	}
	return fmt.Errorf("%w: %s/%s is repository %s, not %s", errForbidden, owner, name, id, repoID)
}
