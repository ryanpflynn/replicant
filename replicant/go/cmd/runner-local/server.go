package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/authz"
	"example.com/replicant/internal/runner/engine"
	"example.com/replicant/internal/runner/httpx"
	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/jcsx"
	"example.com/replicant/internal/runner/jose"
	"example.com/replicant/internal/runner/revoke"
	"example.com/replicant/internal/runner/s3x"
	"example.com/replicant/internal/runner/schemas"
	v1 "example.com/replicant/internal/runner/v1alpha1"
)

// Server is runner-local: Runner API listeners, executor and notifier in one process.
type Server struct {
	Cfg      *Config
	Log      *slog.Logger
	Store    *Store
	Signer   *jose.Signer
	Engine   *engine.Client
	EngineOK bool
	SELinux  bool
	S3       *s3x.Client
	Registry *Registry
	Grants   *authz.GrantVerifier
	Core     *revoke.Core
	Feed     *Feed
	Kill     *KillSwitches
	Creds    *Creds
	Inbox    *Inbox
	Notifier *Notifier
	Tokens   *authz.TokenVerifier // runner-sandbox verifier

	workers map[string]string // token -> subject
	readers map[string]string
	pageKey []byte

	createMu sync.Mutex
	wake     chan string
}

var workerSubjects = []string{
	"system:serviceaccount:replicant-workers:replicant-worker",
	"system:serviceaccount:replicant-eval:eval-worker",
}

// authWorker authenticates the orchestration caller (static dev keys locally, X-14).
func (s *Server) authWorker(w http.ResponseWriter, r *http.Request) bool {
	tok := httpx.Bearer(r)
	if tok == "" {
		api.WriteProblem(w, api.NewProblem("unauthenticated", "missing bearer token"))
		return false
	}
	sub, ok := s.workers[tok]
	if !ok {
		if _, isReader := s.readers[tok]; isReader {
			api.WriteProblem(w, api.NewProblem("forbidden", "revocation readers may only call GET /v1/revocations"))
			return false
		}
		api.WriteProblem(w, api.NewProblem("unauthenticated", "unknown token"))
		return false
	}
	if !slices.Contains(workerSubjects, sub) {
		api.WriteProblem(w, api.NewProblem("forbidden", "subject not allowed"))
		return false
	}
	return true
}

// OrchestrationHandler serves :8480.
func (s *Server) OrchestrationHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })
	mux.HandleFunc("GET /readyz", s.readyz)
	mux.HandleFunc("GET /.well-known/jwks.json", s.jwks)
	mux.HandleFunc("GET /v1/revocations", s.revocations)
	mux.HandleFunc("/v1/runs", func(w http.ResponseWriter, r *http.Request) {
		if !s.authWorker(w, r) {
			return
		}
		switch r.Method {
		case http.MethodPost:
			s.createRun(w, r)
		case http.MethodGet:
			s.listRuns(w, r)
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	})
	mux.HandleFunc("/v1/runs/", func(w http.ResponseWriter, r *http.Request) {
		if !s.authWorker(w, r) {
			return
		}
		rest := strings.TrimPrefix(r.URL.Path, "/v1/runs/")
		parts := strings.Split(rest, "/")
		id, action, _ := strings.Cut(parts[0], ":")
		switch {
		case len(parts) == 1 && action == "" && r.Method == http.MethodGet:
			s.getRun(w, r, id)
		case len(parts) == 1 && action == "cancel" && r.Method == http.MethodPost:
			s.cancelRun(w, r, id)
		case len(parts) == 2 && parts[1] == "messages" && r.Method == http.MethodPost:
			s.postMessage(w, r, id)
		case len(parts) == 3 && parts[1] == "messages" && r.Method == http.MethodGet:
			s.getMessage(w, r, id, parts[2])
		default:
			api.WriteProblem(w, api.NewProblem("run_not_found", "no such route"))
		}
	})
	mux.HandleFunc("POST /internal/v1/runs/", func(w http.ResponseWriter, r *http.Request) {
		// The controller is in-process locally; this path exists for parity and accepts worker keys.
		if !s.authWorker(w, r) {
			return
		}
		id, action, _ := strings.Cut(strings.TrimPrefix(r.URL.Path, "/internal/v1/runs/"), ":")
		if action != "revoke" {
			w.WriteHeader(404)
			return
		}
		rec, _ := s.Store.Get(id)
		if rec == nil {
			api.WriteProblem(w, api.NewProblem("run_not_found", id))
			return
		}
		var body struct {
			Reason string `json:"reason"`
		}
		_ = json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&body)
		s.Feed.Add(r.Context(), id, rec.AgentRun.Spec.JobID, body.Reason)
		w.WriteHeader(204)
	})
	return s.logMW("orchestration", mux)
}

func (s *Server) logMW(listener string, h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sw := &statusWriter{ResponseWriter: w, code: 200}
		start := time.Now()
		h.ServeHTTP(sw, r)
		if r.URL.Path != "/healthz" && r.URL.Path != "/readyz" {
			s.Log.Info("http", "listener", listener, "method", r.Method, "path", r.URL.Path, "status", sw.code, "ms", time.Since(start).Milliseconds())
		}
	})
}

type statusWriter struct {
	http.ResponseWriter
	code int
}

func (s *statusWriter) WriteHeader(c int) { s.code = c; s.ResponseWriter.WriteHeader(c) }
func (s *statusWriter) Flush() {
	if f, ok := s.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func (s *Server) jwks(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "max-age=600")
	api.WriteJSON(w, 200, jose.JWKS{Keys: []jose.JWK{s.Signer.PublicJWK()}})
}

func (s *Server) readyz(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 1800*time.Millisecond)
	defer cancel()
	var problems []string
	if _, err := s.Engine.Version(ctx); err != nil {
		problems = append(problems, "engine: "+err.Error())
	}
	if err := s.S3.HeadBucket(ctx, s.Cfg.RegistryBucket); err != nil {
		problems = append(problems, "s3: "+err.Error())
	}
	if s.Cfg.ReadyRequireTemporal {
		if err := s.Notifier.Ready(ctx); err != nil {
			problems = append(problems, "temporal: "+err.Error())
		}
	}
	if err := touch(s.Cfg.StateDir); err != nil {
		problems = append(problems, "state dir: "+err.Error())
	}
	if len(problems) > 0 {
		api.WriteJSON(w, 503, map[string]any{"ready": false, "problems": problems})
		return
	}
	api.WriteJSON(w, 200, map[string]any{"ready": true})
}

func (s *Server) revocations(w http.ResponseWriter, r *http.Request) {
	tok := httpx.Bearer(r)
	if tok == "" {
		api.WriteProblem(w, api.NewProblem("unauthenticated", "missing bearer token"))
		return
	}
	if _, ok := s.readers[tok]; !ok {
		if _, isWorker := s.workers[tok]; isWorker {
			api.WriteProblem(w, api.NewProblem("forbidden", "not a revocation reader"))
			return
		}
		api.WriteProblem(w, api.NewProblem("unauthenticated", "unknown token"))
		return
	}
	api.WriteJSON(w, 200, s.Feed.Since(r.URL.Query().Get("since")))
}

// parseIdemKey parses the RFC 9651 sf-string.
func parseIdemKey(h string) (string, error) {
	h = strings.TrimSpace(h)
	if len(h) < 2 || h[0] != '"' || h[len(h)-1] != '"' {
		return "", errors.New("Idempotency-Key must be an RFC 9651 sf-string (quoted)")
	}
	var b strings.Builder
	in := h[1 : len(h)-1]
	for i := 0; i < len(in); i++ {
		c := in[i]
		if c == '\\' {
			if i+1 >= len(in) || (in[i+1] != '"' && in[i+1] != '\\') {
				return "", errors.New("bad escape in sf-string")
			}
			i++
			c = in[i]
		} else if c == '"' || c < 0x20 || c > 0x7e {
			return "", errors.New("bad character in sf-string")
		}
		b.WriteByte(c)
	}
	k := b.String()
	if !ids.IdemKeyRe.MatchString(k) || len(k) > 255 {
		return "", errors.New("idempotency key does not match ik1 grammar")
	}
	return k, nil
}

func violationsOf(err error) []api.Violation {
	var se *schemas.Error
	if errors.As(err, &se) {
		out := make([]api.Violation, 0, len(se.Violations))
		for _, v := range se.Violations {
			out = append(out, api.Violation{Field: v.Field, Rule: v.Rule, Message: v.Message})
		}
		return out
	}
	return []api.Violation{{Field: "", Rule: "invalid", Message: err.Error()}}
}

func badRequest(w http.ResponseWriter, field, rule, msg string) {
	p := api.NewProblem("invalid_request", msg)
	p.Violations = []api.Violation{{Field: field, Rule: rule, Message: msg}}
	api.WriteProblem(w, p)
}

func (s *Server) createRun(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20+1))
	if err != nil || len(body) > 1<<20 {
		badRequest(w, "", "size", "body too large or unreadable")
		return
	}
	// A-1: schema (additionalProperties false).
	if err := schemas.Validate(schemas.RunSpec, body); err != nil {
		p := api.NewProblem("invalid_request", "RunSpec does not validate")
		p.Violations = violationsOf(err)
		api.WriteProblem(w, p)
		return
	}
	var rs api.RunSpec
	if err := json.Unmarshal(body, &rs); err != nil {
		badRequest(w, "", "json", err.Error())
		return
	}
	hk := r.Header.Get("Idempotency-Key")
	if hk == "" {
		api.WriteProblem(w, api.NewProblem("missing_idempotency_key", "Idempotency-Key header required"))
		return
	}
	key, err := parseIdemKey(hk)
	if err != nil {
		badRequest(w, "Idempotency-Key", "sf-string", err.Error())
		return
	}
	if key != "ik1:"+rs.WorkflowID+":"+rs.LogicalActivityID {
		badRequest(w, "Idempotency-Key", "match", "key must equal ik1:<workflow_id>:<logical_activity_id>")
		return
	}
	// A-2.
	if rs.Callback.WorkflowID != rs.WorkflowID {
		badRequest(w, "callback.workflow_id", "equal", "callback.workflow_id must equal workflow_id")
		return
	}
	if !strings.HasPrefix(rs.WorkflowID, rs.JobID) {
		badRequest(w, "workflow_id", "prefix", "workflow_id must start with job_id")
		return
	}
	if rs.Callback.Namespace != "replicant-"+s.Cfg.Env {
		// runner-service.md A-2 says 400; test-strategy RC-ADM-05 expects 422 (DEVIATIONS RUNNER-D06).
		s.admissionDenied(w, "callback.namespace", "callback namespace must be replicant-"+s.Cfg.Env)
		return
	}
	runID := ids.Derived("run", rs.TenantID, key)
	digest, err := jcsx.SpecDigest(body)
	if err != nil {
		badRequest(w, "", "jcs", err.Error())
		return
	}
	// RS-10 / A-5b: verify the grant (also on a replay, where the retry's grant is verified then discarded).
	g, gerr := s.Grants.Verify(ctx, rs.RunGrant)
	if gerr == nil {
		gerr = grantMatches(g, &rs, runID, digest)
	}

	s.createMu.Lock()
	defer s.createMu.Unlock()
	existing, err := s.Store.Get(runID)
	if err != nil {
		api.WriteProblem(w, api.NewProblem("internal", err.Error()))
		return
	}
	if existing != nil {
		if gerr != nil {
			s.grantProblem(w, gerr)
			return
		}
		if existing.Local.SpecDigest != digest {
			p := api.NewProblem("idempotency_key_reuse", "a run with this key exists with a different spec")
			p.RunID = runID
			api.WriteProblem(w, p)
			return
		}
		api.WriteJSON(w, 200, toStatus(existing))
		return
	}
	// Execution evidence (RS-3): never execute a run_id twice.
	if expired, err := s.evidence(ctx, &rs); err != nil {
		p := api.NewProblem("runner_unavailable", "evidence check: "+err.Error())
		p.RetryAfterSeconds = 10
		api.WriteProblem(w, p)
		return
	} else if expired {
		p := api.NewProblem("run_expired", "run already executed and its record was collected")
		p.RunID = runID
		api.WriteProblem(w, p)
		return
	}
	// A-3 canonical prefixes.
	for field, pair := range map[string][2]string{
		"artifact_prefixes.artifacts":   {rs.ArtifactPrefixes.Artifacts, "artifacts"},
		"artifact_prefixes.transcripts": {rs.ArtifactPrefixes.Transcripts, "transcripts"},
		"artifact_prefixes.session":     {rs.ArtifactPrefixes.Session, "session"},
	} {
		want := fmt.Sprintf("s3://%s/tenants/%s/jobs/%s/runs/%s/", s3x.Bucket(s.Cfg.Org, s.Cfg.Env, pair[1]), rs.TenantID, rs.JobID, runID)
		if pair[0] != want {
			s.admissionDenied(w, field, "prefix must be "+want)
			return
		}
	}
	// A-4 inputs manifest.
	if !strings.HasPrefix(rs.Inputs.ManifestURI, rs.ArtifactPrefixes.Artifacts+"inputs/") {
		s.admissionDenied(w, "inputs.manifest_uri", "must be under <artifacts>inputs/")
		return
	}
	bkt, k, _ := s3x.ParseURI(rs.Inputs.ManifestURI)
	mb, _, err := s.S3.Get(ctx, bkt, k)
	switch {
	case errors.Is(err, s3x.ErrNotFound):
		s.admissionDenied(w, "inputs.manifest_uri", "inputs manifest not found")
		return
	case err != nil:
		p := api.NewProblem("runner_unavailable", "inputs manifest: "+err.Error())
		p.RetryAfterSeconds = 10
		api.WriteProblem(w, p)
		return
	case len(mb) > 1<<20:
		s.admissionDenied(w, "inputs.manifest_uri", "inputs manifest larger than 1 MiB")
		return
	case s3x.Sha256Hex(mb) != rs.Inputs.ManifestSHA256:
		s.admissionDenied(w, "inputs.manifest_sha256", "inputs manifest digest mismatch")
		return
	}
	// A-5 kill switches.
	repos := []string{rs.Repo.Host + "/" + rs.Repo.Owner + "/" + rs.Repo.Name}
	for _, ar := range rs.AdditionalRepos {
		repos = append(repos, ar.Host+"/"+ar.Owner+"/"+ar.Name)
	}
	if field, err := s.Kill.Check(ctx, rs.TenantID, repos, rs.ReleaseBundleID); err != nil {
		p := api.NewProblem("runner_unavailable", err.Error())
		p.RetryAfterSeconds = 30
		api.WriteProblem(w, p)
		return
	} else if field != "" {
		s.admissionDenied(w, field, "kill switch engaged (starts)")
		return
	}
	// A-5b.
	if gerr != nil {
		s.grantProblem(w, gerr)
		return
	}
	// A-6 .. A-8.
	res, err := s.Registry.Resolve(ctx, &rs)
	if err != nil {
		var nr *ErrNotResolvable
		var ad *AdmissionErr
		switch {
		case errors.As(err, &nr):
			p := api.NewProblem("definition_not_resolvable", nr.Error())
			p.Violations = []api.Violation{{Field: nr.Field, Rule: "resolvable", Message: nr.Msg}}
			api.WriteProblem(w, p)
		case errors.As(err, &ad):
			s.admissionDenied(w, ad.Field, ad.Msg)
		default:
			p := api.NewProblem("runner_unavailable", err.Error())
			p.RetryAfterSeconds = 10
			api.WriteProblem(w, p)
		}
		return
	}
	if res.Agent.CLIImage.Image == "" || res.Env.Image.Resolved.Image == "" {
		p := api.NewProblem("definition_not_resolvable", "definition has no image")
		api.WriteProblem(w, p)
		return
	}
	// A-10 network profile (lenient when the bundle carries no network_profiles policy).
	if pol, ok := res.Policies["network_profiles"]; ok && !strings.Contains(string(pol), `"`+rs.NetworkProfile+`"`) {
		s.admissionDenied(w, "network_profile", "network profile not defined by the bundle policy")
		return
	}
	// A-15 active-run limit.
	if used := s.activeRuns(rs.TenantID); used >= s.Cfg.MaxConcurrent {
		p := api.NewProblem("tenant_quota_exceeded", "active-run limit reached")
		p.RetryAfterSeconds, p.QuotaUsed, p.QuotaLimit = 30, ptr(used), ptr(s.Cfg.MaxConcurrent)
		api.WriteProblem(w, p)
		return
	}
	rec := s.newRecord(&rs, runID, key, digest, g, res, r.Header.Get("traceparent"))
	if err := s.Store.Create(rec); err != nil {
		if errors.Is(err, ErrExists) {
			cur, _ := s.Store.Get(runID)
			if cur != nil && cur.Local.SpecDigest == digest {
				api.WriteJSON(w, 200, toStatus(cur))
				return
			}
			api.WriteProblem(w, api.NewProblem("idempotency_key_reuse", "concurrent create with a different spec"))
			return
		}
		api.WriteProblem(w, api.NewProblem("internal", err.Error()))
		return
	}
	s.Log.Info("run created", "run_id", runID, "job_id", rs.JobID, "role", rs.Role, "tenant", rs.TenantID)
	s.kick(runID)
	w.Header().Set("Location", "/v1/runs/"+runID)
	api.WriteJSON(w, 201, toStatus(rec))
}

func (s *Server) admissionDenied(w http.ResponseWriter, field, msg string) {
	p := api.NewProblem("admission_denied", field+": "+msg)
	p.Violations = []api.Violation{{Field: field, Rule: "admission", Message: msg}}
	api.WriteProblem(w, p)
}

func (s *Server) grantProblem(w http.ResponseWriter, err error) {
	p := api.NewProblem("run_grant_invalid", err.Error())
	api.WriteProblem(w, p)
}

func grantMatches(g *api.RunGrantClaims, rs *api.RunSpec, runID, digest string) error {
	switch {
	case g.SpecDigest != digest:
		return fmt.Errorf("grant spec_digest %s != %s", g.SpecDigest, digest)
	case g.RunID != runID:
		return fmt.Errorf("grant run_id mismatch")
	case g.TenantID != rs.TenantID || g.JobID != rs.JobID || g.Role != rs.Role:
		return fmt.Errorf("grant tenant/job/role mismatch")
	}
	need := []string{rs.Repo.RepoID}
	for _, a := range rs.AdditionalRepos {
		need = append(need, a.RepoID)
	}
	for _, id := range need {
		if !slices.Contains(g.RepoIDs, id) {
			return fmt.Errorf("grant repo_ids do not cover %s", id)
		}
	}
	return nil
}

func (s *Server) evidence(ctx context.Context, rs *api.RunSpec) (bool, error) {
	ab, ak, _ := s3x.ParseURI(rs.ArtifactPrefixes.Artifacts)
	for _, k := range []string{ak + "agentrun_record/agentrun.json", ak + "manifest.json"} {
		ok, _, err := s.S3.Head(ctx, ab, k)
		if err != nil {
			return false, err
		}
		if ok {
			return true, nil
		}
	}
	tb, tk, _ := s3x.ParseURI(rs.ArtifactPrefixes.Transcripts)
	keys, err := s.S3.List(ctx, tb, tk, 1)
	if err != nil {
		return false, err
	}
	return len(keys) > 0, nil
}

func (s *Server) activeRuns(tenant string) int {
	recs, _ := s.Store.List()
	n := 0
	for _, r := range recs {
		if r.AgentRun.Spec.TenantID == tenant && !r.AgentRun.Status.Phase.IsTerminal() {
			n++
		}
	}
	return n
}

func (s *Server) newRecord(rs *api.RunSpec, runID, key, digest string, g *api.RunGrantClaims, res *Resolved, traceparent string) *Record {
	now := time.Now().UTC()
	body := ids.Body(runID)
	fetch := rs.Repo.FetchDepth
	if fetch == nil {
		fetch = res.Env.Repo.FetchDepth
	}
	toRef := func(r api.Repo, fd *int32) v1.RepoRef {
		return v1.RepoRef{Provider: v1.RepoProvider(r.Provider), Host: r.Host, Owner: r.Owner, Name: r.Name, RepoID: r.RepoID, BaseRef: r.BaseRef, BaseSHA: r.BaseSHA, FetchDepth: fd}
	}
	ar := v1.AgentRun{
		TypeMeta: metav1.TypeMeta{APIVersion: "replicant.example.com/v1alpha1", Kind: "AgentRun"},
		ObjectMeta: metav1.ObjectMeta{
			Name: "ar-" + body, Namespace: "sbx-" + rs.TenantID, CreationTimestamp: metav1.NewTime(now), Generation: 1,
			Labels: map[string]string{
				v1.LabelTenant: rs.TenantID, v1.LabelJobID: rs.JobID, v1.LabelRunID: runID, v1.LabelRole: rs.Role,
				v1.LabelWorkloadClass: rs.WorkloadClass, v1.LabelBundle: rs.ReleaseBundleID[:min(30, len(rs.ReleaseBundleID))],
			},
			Annotations: map[string]string{v1.AnnotationSpecDigest: digest, v1.AnnotationIdemKey: key},
			Finalizers:  []string{v1.FinalizerCleanup},
		},
		Spec: v1.AgentRunSpec{
			TenantID: rs.TenantID, JobID: rs.JobID, RunID: runID, WorkflowID: rs.WorkflowID, LogicalActivityID: rs.LogicalActivityID,
			IdempotencyKey: key, Role: v1.RunRole(rs.Role), WorkloadClass: v1.WorkloadClass(rs.WorkloadClass),
			WorkflowType: v1.WorkflowType(rs.WorkflowType), ReleaseBundleID: rs.ReleaseBundleID,
			AgentDefinition:       v1.DefinitionRef{Name: rs.AgentDefinition.Name, Version: rs.AgentDefinition.Version, Digest: rs.AgentDefinition.Digest},
			EnvironmentDefinition: v1.DefinitionRef{Name: rs.EnvironmentDefinition.Name, Version: rs.EnvironmentDefinition.Version, Digest: rs.EnvironmentDefinition.Digest},
			ResourceProfile:       rs.ResourceProfileOrDefault(),
			Repo:                  toRef(rs.Repo, fetch),
			Inputs:                v1.RunInputs{ManifestURI: rs.Inputs.ManifestURI, ManifestSHA256: rs.Inputs.ManifestSHA256},
			Artifacts: v1.RunArtifacts{ArtifactsPrefix: rs.ArtifactPrefixes.Artifacts, TranscriptsPrefix: rs.ArtifactPrefixes.Transcripts,
				SessionPrefix: rs.ArtifactPrefixes.Session, MaxOutputBytes: 4 << 30},
			RuntimeClassName: runtimeClassFor(rs.IsolationHint),
			Resources: v1.RunResources{
				Requests: v1.ResourceQuantities{CPU: res.Profile.Requests.CPU, Memory: res.Profile.Requests.Memory, EphemeralStorage: res.Profile.Requests.EphemeralStorage},
				Limits:   v1.ResourceQuantities{CPU: res.Profile.Limits.CPU, Memory: res.Profile.Limits.Memory, EphemeralStorage: res.Profile.Limits.EphemeralStorage},
			},
			Workspace:         v1.RunWorkspace{SizeLimit: res.Profile.Workspace.SizeLimit, Backing: v1.BackingEmptyDir, Enforcement: v1.EnforceWatchdog},
			NetworkProfile:    rs.NetworkProfile,
			PriorityClassName: priorityClass(rs),
			Timeouts:          v1.RunTimeouts{StartupSeconds: rs.Timeouts.StartupSeconds, ActiveDeadlineSeconds: rs.Timeouts.ActiveDeadlineSeconds, FinalizeSeconds: rs.Timeouts.FinalizeSeconds},
			Callback:          v1.RunCallback{Namespace: rs.Callback.Namespace, WorkflowID: rs.Callback.WorkflowID, SignalName: "run.status_changed"},
			RunGrant:          rs.RunGrant,
		},
	}
	for _, a := range rs.AdditionalRepos {
		ar.Spec.AdditionalRepos = append(ar.Spec.AdditionalRepos, toRef(a, fetch))
	}
	if rs.Limits != nil {
		ar.Spec.Limits = &v1.RunLimits{}
		if rs.Limits.MaxCostUSD != nil {
			ar.Spec.Limits.MaxCostMicrosUSD = ptr(int64(*rs.Limits.MaxCostUSD * 1e6))
		}
		if rs.Limits.MaxOutputBytes != nil {
			ar.Spec.Artifacts.MaxOutputBytes = *rs.Limits.MaxOutputBytes
		}
	}
	steering := false
	if rs.Steering != nil {
		steering = rs.Steering.Enabled && res.Agent.Steering.Live
		ar.Spec.Steering = &v1.RunSteering{Enabled: steering}
	}
	if rs.TraceContext != nil {
		ar.Spec.TraceContext = &v1.TraceContext{Traceparent: rs.TraceContext.Traceparent, Tracestate: rs.TraceContext.Tracestate}
	}
	if rs.WorkflowType != "" {
		ar.Labels[v1.LabelWorkflowType] = rs.WorkflowType
	}
	ar.Status.Phase = v1.PhasePending
	ar.Status.ObservedGeneration = 1
	setCond(&ar, v1.CondSucceeded, metav1.ConditionUnknown, "InProgress", "", now)
	setCond(&ar, v1.CondCallbackDelivered, metav1.ConditionUnknown, "DeliveryPending", "", now)
	cpus := parseCPU(res.Profile.Limits.CPU)
	if cpus <= 0 || cpus > s.Cfg.MaxCPUs {
		cpus = s.Cfg.MaxCPUs
	}
	mem := parseBytes(res.Profile.Limits.Memory)
	if mem <= 0 || mem > s.Cfg.MaxMemoryBytes {
		mem = s.Cfg.MaxMemoryBytes
	}
	return &Record{AgentRun: ar, Local: Local{
		SpecDigest: digest, IdempotencyKey: key, Grant: *g, Isolation: string(s.Cfg.Runtime), Runtime: s.Cfg.Runtime,
		AgentImage: res.Env.Image.Resolved.Image, CLIImage: res.Agent.CLIImage.Image, CPUs: cpus, MemoryBytes: mem,
		WorkspaceBytes: parseBytes(res.Profile.Workspace.SizeLimit), SteeringLive: steering, Repo: rs.Repo,
		EnvDefaults: res.Env.Env, Generation: 1, Stage: "accepted", TraceParent: traceparent,
	}}
}

func runtimeClassFor(hint string) v1.RuntimeClassName {
	switch hint {
	case "gvisor":
		return v1.RuntimeGVisor
	case "kata-fc", "kata-clh":
		return v1.RuntimeKataCLH // kata-fc is never selectable (R1-280)
	}
	return v1.RuntimeKataCLH
}

func priorityClass(rs *api.RunSpec) v1.PriorityClassName {
	if rs.Role == "eval" || rs.TenantID == "replicant-eval" {
		return "replicant-eval"
	}
	return v1.PriorityClassName("replicant-run-" + rs.Priority)
}

func (s *Server) getRun(w http.ResponseWriter, r *http.Request, id string) {
	if !ids.RunIDRe.MatchString(id) {
		api.WriteProblem(w, api.NewProblem("run_not_found", "malformed run_id"))
		return
	}
	rec, err := s.Store.Get(id)
	if err != nil {
		api.WriteProblem(w, api.NewProblem("internal", err.Error()))
		return
	}
	if rec == nil {
		api.WriteProblem(w, api.NewProblem("run_not_found", id))
		return
	}
	api.WriteJSON(w, 200, toStatus(rec))
}

func (s *Server) listRuns(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	tenant, job, phase := q.Get("tenant_id"), q.Get("job_id"), q.Get("phase")
	if tenant == "" && job == "" {
		badRequest(w, "job_id", "required", "job_id or tenant_id required")
		return
	}
	size := 100
	if v := q.Get("page_size"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 || n > 500 {
			badRequest(w, "page_size", "range", "page_size must be 1..500")
			return
		}
		size = n
	}
	start := 0
	if pt := q.Get("page_token"); pt != "" {
		n, ok := s.verifyPage(pt)
		if !ok {
			badRequest(w, "page_token", "invalid", "bad page token")
			return
		}
		start = n
	}
	recs, _ := s.Store.List()
	var matched []api.RunStatus
	for _, rec := range recs {
		st := toStatus(rec)
		if (tenant != "" && st.TenantID != tenant) || (job != "" && st.JobID != job) || (phase != "" && st.Phase != phase) {
			continue
		}
		if t := q.Get("terminal"); t != "" && strconv.FormatBool(st.Terminal) != t {
			continue
		}
		matched = append(matched, st)
	}
	out := api.RunList{Runs: []api.RunStatus{}}
	if start < len(matched) {
		end := min(start+size, len(matched))
		out.Runs = matched[start:end]
		if end < len(matched) {
			out.NextPageToken = s.signPage(end)
		}
	}
	api.WriteJSON(w, 200, out)
}

func (s *Server) signPage(n int) string {
	m := hmac.New(sha256.New, s.pageKey)
	v := strconv.Itoa(n)
	m.Write([]byte(v))
	return v + "." + base64.RawURLEncoding.EncodeToString(m.Sum(nil))
}

func (s *Server) verifyPage(t string) (int, bool) {
	v, _, ok := strings.Cut(t, ".")
	if !ok || !hmac.Equal([]byte(s.signPage(atoiDef(v, -1))), []byte(t)) {
		return 0, false
	}
	return atoiDef(v, 0), true
}

func (s *Server) cancelRun(w http.ResponseWriter, r *http.Request, id string) {
	var cr api.CancelRequest
	b, _ := io.ReadAll(io.LimitReader(r.Body, 64<<10))
	if err := json.Unmarshal(b, &cr); err != nil {
		badRequest(w, "", "json", "invalid body")
		return
	}
	switch cr.Reason {
	case "job_cancelled", "superseded", "orchestrator_deadline", "operator", "takeover", "budget_exhausted", "security":
	default:
		badRequest(w, "reason", "enum", "unknown cancel reason")
		return
	}
	rec, _ := s.Store.Get(id)
	if rec == nil {
		api.WriteProblem(w, api.NewProblem("run_not_found", id))
		return
	}
	if rec.AgentRun.Status.Phase.IsTerminal() {
		api.WriteJSON(w, 200, toStatus(rec))
		return
	}
	rec, err := s.Store.Update(id, func(rr *Record) error {
		if !rr.AgentRun.Spec.CancelRequested {
			rr.AgentRun.Spec.CancelRequested = true
			rr.AgentRun.Spec.CancelReason = v1.CancelReason(cr.Reason)
			rr.AgentRun.Generation++
			rr.Local.Generation = rr.AgentRun.Generation
		}
		return nil
	})
	if err != nil {
		api.WriteProblem(w, api.NewProblem("internal", err.Error()))
		return
	}
	if err := s.Inbox.WriteControl(r.Context(), rec.AgentRun.Spec.Artifacts.ArtifactsPrefix, rec.AgentRun.Spec.TenantID, cr.Reason); err != nil {
		s.Log.Warn("control key write failed", "run_id", id, "err", err)
	} else {
		now := time.Now()
		rec, _ = s.Store.Update(id, func(rr *Record) error { rr.Local.ControlAt = &now; return nil })
	}
	s.Log.Info("cancel requested", "run_id", id, "reason", cr.Reason, "requested_by", cr.RequestedBy, "detail", truncate(cr.Detail, 1024))
	s.kick(id)
	api.WriteJSON(w, 202, toStatus(rec))
}

func (s *Server) postMessage(w http.ResponseWriter, r *http.Request, id string) {
	var m api.RunMessage
	b, _ := io.ReadAll(io.LimitReader(r.Body, 64<<10))
	if err := json.Unmarshal(b, &m); err != nil || !ids.MessageIDRe.MatchString(m.MessageID) || m.Text == "" || len(m.Text) > 16000 {
		badRequest(w, "", "invalid", "invalid RunMessage")
		return
	}
	rec, _ := s.Store.Get(id)
	if rec == nil {
		api.WriteProblem(w, api.NewProblem("run_not_found", id))
		return
	}
	if rec.AgentRun.Spec.Steering == nil || !rec.AgentRun.Spec.Steering.Enabled {
		api.WriteProblem(w, api.NewProblem("steering_not_supported", "steering not effective for this run"))
		return
	}
	switch rec.AgentRun.Status.Phase {
	case v1.PhasePending, v1.PhaseProvisioning, v1.PhaseRunning:
	default:
		api.WriteProblem(w, api.NewProblem("run_not_active", string(rec.AgentRun.Status.Phase)))
		return
	}
	st, created, err := s.Inbox.Post(r.Context(), rec.AgentRun.Spec.Artifacts.ArtifactsPrefix, id, rec.AgentRun.Spec.TenantID, m)
	if err != nil {
		p := api.NewProblem("runner_unavailable", err.Error())
		p.RetryAfterSeconds = 5
		api.WriteProblem(w, p)
		return
	}
	if created {
		api.WriteJSON(w, 202, st)
	} else {
		api.WriteJSON(w, 200, st)
	}
}

func (s *Server) getMessage(w http.ResponseWriter, r *http.Request, id, mid string) {
	rec, _ := s.Store.Get(id)
	if rec == nil {
		api.WriteProblem(w, api.NewProblem("run_not_found", id))
		return
	}
	st, err := s.Inbox.Status(r.Context(), rec.AgentRun.Spec.Artifacts.ArtifactsPrefix, id, mid, rec.AgentRun.Status.Phase.IsTerminal())
	if err != nil {
		api.WriteProblem(w, api.NewProblem("runner_unavailable", err.Error()))
		return
	}
	if st == nil {
		api.WriteProblem(w, api.NewProblem("run_not_found", "message not found"))
		return
	}
	api.WriteJSON(w, 200, st)
}

func (s *Server) kick(runID string) {
	select {
	case s.wake <- runID:
	default:
	}
}
