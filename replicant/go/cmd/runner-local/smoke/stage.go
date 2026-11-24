package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/httpx"
	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/jcsx"
	"example.com/replicant/internal/runner/s3x"
	"example.com/replicant/internal/runner/schemas"
)

func sha256sum(b []byte) []byte { d := sha256.Sum256(b); return d[:] }

func createBucket(ctx context.Context, c *s3x.Client, b string) error {
	_, err := c.S3.CreateBucket(ctx, &s3.CreateBucketInput{Bucket: aws.String(b)})
	return err
}

func agentSpec() map[string]any {
	return map[string]any{
		"roles": []string{"implementer", "repair"}, "workloadClass": "coding", "cli": "claude-code",
		"cliImage": map[string]any{"image": "localhost/replicant/cli-claude-code:dev", "version": "2.1.310", "entrypointVersion": "1.1.0"},
		"model": map[string]any{"primary": "anthropic/claude-sonnet-5-5", "fallbacks": []string{"anthropic/claude-opus-5-5"},
			"small": "anthropic/claude-haiku-5-5", "parameters": map[string]any{"maxOutputTokens": 32000, "thinkingTokens": 8000}},
		"prompts": map[string]any{"role": map[string]any{"ref": "prompt/implementer-role@label:production", "mode": "append"},
			"constraints": "prompt/replicant-constraints@label:production", "task": "prompt/implementer-task@label:production",
			"finalize": "prompt/finalize-handoff@label:production"},
		"tools": map[string]any{"read": true, "edit": true,
			"shell": map[string]any{"mode": "any", "deny": []string{"git push:*", "git remote add:*", "curl:*", "wget:*", "ssh:*", "docker:*"},
				"defaultTimeoutSeconds": 300, "maxTimeoutSeconds": 1800, "maxOutputChars": 30000},
			"web": map[string]any{"fetch": false, "search": false}, "subagents": map[string]any{"enabled": true},
			"denyTools": []string{"WebFetch", "WebSearch", "NotebookEdit", "EnterWorktree"}},
		"mcpServers": []any{},
		"limits": map[string]any{"maxTurns": 50, "maxWallSeconds": 1800, "maxCostUsd": 5, "maxModelCalls": 200, "startupSeconds": 300,
			"finalizeSeconds": 60, "cliGraceSeconds": 10},
		"gateway": map[string]any{"protocol": "anthropic_messages",
			"retry":                 map[string]any{"maxAttempts": 3, "maxCumulativeWaitSeconds": 60, "baseDelayMs": 500, "maxDelayMs": 5000},
			"requestTimeoutSeconds": 300, "softBudgetFraction": 0.9, "unavailableAfterSeconds": 120},
		"env":      map[string]string{"CI": "true", "TZ": "UTC"},
		"steering": map[string]any{"live": true},
		"session":  map[string]any{"capture": "never", "resumable": false},
		"outputContract": map[string]any{"roleResultSchema": "replicant.role-result.implementer/v1", "structuredResult": "cli_schema",
			"outputs": map[string]string{"diff_bundle": "required", "handoff": "required", "session_state": "optional", "memory_candidates": "optional",
				"plan": "forbidden", "review_report": "forbidden", "workspace_archive": "optional"},
			"diffPolicy": map[string]any{"forbiddenPaths": []string{".github/workflows/**", "**/*.pem", "**/.env"}, "onForbiddenPath": "strip_and_report",
				"maxFilesChanged": 300, "maxPatchBytes": 33554432, "allowBinary": false, "allowSubmoduleChanges": false, "allowModeChanges": true},
			"workspaceIntegrity": "off"},
		"claudeCode": map[string]any{"repoInstructionFiles": "ignore", "includePartialMessages": false, "propagateTraceparent": true,
			"telemetry": map[string]any{"metrics": false, "logs": false, "traces": false, "logToolDetails": false}},
	}
}

func envSpec(image string) map[string]any {
	return map[string]any{
		"codebaseType":   "go_service",
		"image":          map[string]any{"resolved": map[string]any{"image": image, "buildSpecDigest": "sha256:" + strings.Repeat("3", 64)}},
		"repo":           map[string]any{"fetchDepth": 50},
		"env":            map[string]string{"CGO_ENABLED": "0", "GOFLAGS": "-mod=readonly"},
		"packageMirrors": []string{"goproxy"},
		"bootstrap": map[string]any{"forRoles": []string{"implementer", "repair", "reviewer", "qa"},
			"steps": []map[string]any{{"name": "go-mod-download", "run": "go mod download", "when": map[string]any{"pathsExist": []string{"go.mod"}}, "timeoutSeconds": 600}}},
		"caches": []map[string]any{{"name": "go-build", "path": "/workspace/.cache/go-build", "kind": "workspace", "env": "GOCACHE"},
			{"name": "go-mod", "path": "/workspace/.cache/go-mod", "kind": "workspace", "env": "GOMODCACHE"}},
		"resourceProfiles": map[string]any{
			"default": map[string]any{"requests": map[string]string{"cpu": "1", "memory": "1Gi", "ephemeralStorage": "4Gi"},
				"limits": map[string]string{"cpu": "2", "memory": "2Gi", "ephemeralStorage": "8Gi"}, "memoryRequestPolicy": "median_peak",
				"workspace": map[string]any{"sizeLimit": "4Gi", "overflow": map[string]string{"policy": "local_only"}}},
			"qa": map[string]any{"requests": map[string]string{"cpu": "1", "memory": "1Gi", "ephemeralStorage": "4Gi"},
				"limits": map[string]string{"cpu": "2", "memory": "2Gi", "ephemeralStorage": "8Gi"}, "memoryRequestPolicy": "match_limit",
				"workspace": map[string]any{"sizeLimit": "4Gi", "overflow": map[string]string{"policy": "local_only"}}},
		},
		"profileByRole":  map[string]string{"qa": "qa"},
		"isolationHint":  map[string]string{"coding": "kata-clh", "qa_build": "kata-clh"},
		"networkProfile": map[string]string{"coding": "coding-default", "qa_build": "qa-with-mirror"},
	}
}

func promptText(t string) map[string]any {
	return map[string]any{"text": t, "sha256": s3x.Sha256Hex([]byte(t))}
}

// runOpts tweaks a staged run.
type runOpts struct {
	tenant, role, lai, workflowType string
	jobID                           string
	baseSHA                         string
	// bundleBaseSHA is the commit the staged repo bundle carries as refs/replicant/base (default baseSHA); a
	// different value stages a bundle that does not hold the plan's base_sha (RC-RUN-09)
	bundleBaseSHA             string
	envRef                    *api.DefinitionRef
	budget                    float64
	active, finalize, startup int32
	scriptKey, scriptSet      string
	corruptInput              bool
	extraObjects              []extraObj
	steering                  bool
	agentRole                 string
	// repo onboarding (§8c): run-plan repo_config, config_base_sha and mcp_auto_allow
	repoConfig, configBaseSHA string
	mcpAutoAllow              []map[string]any
}

type extraObj struct {
	name, typ, place, path, ctype string
	body                          []byte
}

type staged struct {
	runID, key, jobID, wf string
	spec                  map[string]any
	body                  []byte
	digest                string
	scope                 api.RunScope
	prefixes              api.ArtifactPrefixes
}

func (e *env) defaults(o runOpts) runOpts {
	if o.tenant == "" {
		o.tenant = "payments"
	}
	if o.role == "" {
		o.role = "implementer"
	}
	if o.lai == "" {
		o.lai = o.role + ".c0.n1.run"
	}
	if o.jobID == "" {
		o.jobID = ids.ULID("job")
	}
	if o.baseSHA == "" {
		o.baseSHA = e.gitea.baseSHA
	}
	if o.budget == 0 {
		o.budget = 3
	}
	if o.active == 0 {
		o.active = 900
	}
	if o.finalize == 0 {
		o.finalize = 60
	}
	if o.startup == 0 {
		o.startup = 300
	}
	if o.scriptSet == "" {
		o.scriptSet = "walkthrough-ledger-api"
	}
	if o.scriptKey == "" {
		o.scriptKey = o.role
	}
	return o
}

// stage writes the inputs (run plan, prompts, task packet, manifest) and builds the RunSpec and its run scope.
func (e *env) stage(ctx context.Context, o runOpts) (*staged, error) {
	o = e.defaults(o)
	wf := o.jobID
	key := "ik1:" + wf + ":" + o.lai
	runID := ids.Derived("run", o.tenant, key)
	rel := fmt.Sprintf("tenants/%s/jobs/%s/runs/%s/", o.tenant, o.jobID, runID)
	pre := api.ArtifactPrefixes{Artifacts: "s3://" + e.bucket("artifacts") + "/" + rel, Transcripts: "s3://" + e.bucket("transcripts") + "/" + rel,
		Session: "s3://" + e.bucket("session") + "/" + rel}
	envRef := e.envRef
	if o.envRef != nil {
		envRef = *o.envRef
	}
	scopes := []string{"credentials:aws", "gateway:invoke"}
	if o.steering {
		scopes = append(scopes, "inbox:read")
	}
	audiences := []string{"ai-gateway", "runner-sandbox"}
	agent := agentSpec()
	if o.role == "qa" {
		agent["roles"] = []string{"qa"}
		agent["workloadClass"] = "qa_build"
		agent["outputContract"].(map[string]any)["roleResultSchema"] = "replicant.role-result.qa/v1"
		agent["outputContract"].(map[string]any)["outputs"] = map[string]string{"diff_bundle": "forbidden", "handoff": "optional", "plan": "forbidden", "review_report": "forbidden"}
		agent["qaHarness"] = map[string]any{"version": "0.1.0", "agentTasks": false}
	}
	plan := map[string]any{
		"schema": "replicant.run-plan/v1", "run_id": runID, "role": o.role, "agent": agent,
		"effective": map[string]any{"budget_usd": o.budget, "max_turns": 50, "active_deadline_seconds": int(o.active), "finalize_seconds": int(o.finalize),
			"steering_live": o.steering, "models": []string{"anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5", "anthropic/claude-haiku-5-5"},
			"audiences": audiences, "scopes": scopes, "network_profile": "coding-default", "deterministic": true, "git_identity_date": "2026-10-24T00:00:00Z"},
		"prompts": map[string]any{
			"role":          promptText("You are the implementer for the Replicant job. Make the smallest correct change that resolves the task. Run the relevant tests."),
			"constraints":   promptText("Replicant constraints: never write agent configuration files into the repository; never print credentials."),
			"task_template": promptText("Task: {{ task.title }}"),
			"finalize":      promptText("Your budget or time is nearly exhausted. Stop making changes, write /replicant/out/handoff.md, then return your structured result."),
		},
		"workspace": map[string]any{"repo_path": "/workspace/repo", "base_sha": o.baseSHA, "branch_name": "replicant/" + o.jobID},
		"environment": map[string]any{"codebase_type": "go_service", "bootstrap_steps": e.envSpec["bootstrap"].(map[string]any)["steps"], "env": map[string]string{"CGO_ENABLED": "0", "GOFLAGS": "-mod=readonly"},
			"package_mirrors": []string{"goproxy"}, "caches": e.envSpec["caches"], "services": []any{}},
		"repo_instructions": "quote_as_context",
	}
	if o.repoConfig != "" {
		plan["repo_config"] = o.repoConfig
		if o.configBaseSHA != "" {
			plan["config_base_sha"] = o.configBaseSHA
		}
		if o.mcpAutoAllow != nil {
			plan["mcp_auto_allow"] = o.mcpAutoAllow
		}
	}
	planB, _ := json.MarshalIndent(plan, "", "  ")
	if err := schemas.LocalRelaxed(schemas.Validate(schemas.RunPlan, planB)); err != nil {
		return nil, fmt.Errorf("run plan invalid: %v", err)
	}
	sys := []byte("You are the implementer for the Replicant job. Make the smallest correct change that resolves the task. Run the relevant tests.\n\nReplicant constraints: never write agent configuration files into the repository; never print credentials.\n")
	first := []byte("Task: Refund ledger entries double-count FX fees for partial refunds\n\n<untrusted_input source=\"gitea\" id=\"1\">\nWhen a charge that carried an FX fee is partially refunded, ledger.ApplyRefund posts an fx_fee_reversal entry for the whole FX fee. Prorate it with ledger.Prorate.\n</untrusted_input>\n")
	task, _ := json.Marshal(map[string]any{"schema": "replicant.task-packet/v1", "goal": map[string]any{"summary": "Prorate the FX fee reversal on partial refunds",
		"acceptance_criteria": []string{"TestApplyRefund passes"}}})
	type obj struct {
		name, typ, place, path, ctype string
		body                          []byte
	}
	objs := []obj{{"run_plan", "run_inputs", "run_plan", "", "application/json", planB}, {"system_append", "agent_prompt", "system_append", "", "text/markdown", sys},
		{"first_message", "agent_prompt", "first_message", "", "text/markdown", first}, {"task_packet", "task_packet", "context_file", "task_packet.json", "application/json", task}}
	// the repository as a git bundle (SIMPLIFY-D40; replicant_worker/activities/repo_bundle.py): prepare's only source
	bundleBase := o.baseSHA
	if o.bundleBaseSHA != "" {
		bundleBase = o.bundleBaseSHA
	}
	refs := map[string]string{"refs/replicant/base": bundleBase}
	if o.configBaseSHA != "" && o.configBaseSHA != o.baseSHA {
		refs["refs/replicant/config"] = o.configBaseSHA
	}
	bundle, err := e.repoBundle(refs)
	if err != nil {
		return nil, fmt.Errorf("repo bundle: %v", err)
	}
	objs = append(objs, obj{"repo_bundle", "run_inputs", "repo_bundle", "", "application/x-git-bundle", bundle})
	for _, x := range o.extraObjects {
		objs = append(objs, obj{x.name, x.typ, x.place, x.path, x.ctype, x.body})
	}
	var mobjs []map[string]any
	for _, ob := range objs {
		k := rel + "inputs/" + fileName(ob.name, ob.path, ob.place)
		if _, err := e.s3.Put(ctx, e.bucket("artifacts"), k, ob.body, s3x.PutOptions{ContentType: ob.ctype}); err != nil {
			return nil, err
		}
		digest := s3x.Sha256Hex(ob.body)
		if o.corruptInput && ob.name == "task_packet" {
			digest = s3x.Sha256Hex([]byte("tampered"))
		}
		m := map[string]any{"name": ob.name, "type": ob.typ, "bucket_class": "artifacts", "key": k, "sha256": digest, "size_bytes": len(ob.body),
			"content_type": ob.ctype, "place": ob.place}
		if ob.path != "" {
			m["path"] = ob.path
		}
		mobjs = append(mobjs, m)
	}
	manifest := map[string]any{"schema": "replicant.run-inputs/v1", "tenant_id": o.tenant, "job_id": o.jobID, "run_id": runID, "role": o.role,
		"release_bundle_id": e.rb, "created_at": time.Now().UTC().Format(time.RFC3339),
		"agent_definition":       map[string]any{"name": e.agentRef.Name, "version": e.agentRef.Version, "digest": e.agentRef.Digest},
		"environment_definition": map[string]any{"name": envRef.Name, "version": envRef.Version, "digest": envRef.Digest},
		"memory_entries":         []string{}, "objects": mobjs}
	mb, _ := json.MarshalIndent(manifest, "", "  ")
	if err := schemas.Validate(schemas.RunInputs, mb); err != nil {
		return nil, fmt.Errorf("manifest invalid: %v", err)
	}
	if _, err := e.s3.Put(ctx, e.bucket("artifacts"), rel+"inputs/manifest.json", mb, s3x.PutOptions{ContentType: "application/json"}); err != nil {
		return nil, err
	}
	wc := "coding"
	if o.role == "qa" {
		wc = "qa_build"
	}
	spec := map[string]any{
		"schema": "replicant.run-spec/v1", "tenant_id": o.tenant, "job_id": o.jobID, "workflow_id": wf, "logical_activity_id": o.lai,
		"role": o.role, "workload_class": wc, "workflow_type": "bugfix", "release_bundle_id": e.rb,
		"agent_definition": e.agentRef, "environment_definition": envRef, "resource_profile": map[bool]string{true: "qa", false: "default"}[o.role == "qa"],
		"repo": map[string]any{"provider": "gitea", "host": "git.replicant.localhost:3300", "owner": e.gitea.owner, "name": e.gitea.name,
			"repo_id": fmt.Sprint(e.gitea.id), "base_ref": "main", "base_sha": o.baseSHA},
		"inputs":            map[string]any{"manifest_uri": pre.Artifacts + "inputs/manifest.json", "manifest_sha256": s3x.Sha256Hex(mb)},
		"artifact_prefixes": pre, "isolation_hint": "kata-clh", "network_profile": "coding-default",
		"timeouts": map[string]any{"startup_seconds": o.startup, "active_deadline_seconds": o.active, "finalize_seconds": o.finalize},
		"limits":   map[string]any{"max_cost_usd": o.budget}, "steering": map[string]any{"enabled": o.steering}, "priority": "normal",
		"trace_context": map[string]any{"traceparent": "00-" + ids.JobTraceID(o.jobID) + "-a3c1e5f7092b4d6e-01"},
		"callback":      map[string]any{"kind": "temporal_signal", "namespace": "replicant-local", "workflow_id": wf, "signal_name": "run.status_changed"},
	}
	st := &staged{runID: runID, key: key, jobID: o.jobID, wf: wf, spec: spec, prefixes: pre}
	if err := e.scope(st, o, audiences, scopes); err != nil {
		return nil, err
	}
	if err := e.registerPolicy(ctx, st, o); err != nil {
		fmt.Println("  (shim policy registration failed:", err, ")")
	}
	return st, nil
}

func fileName(name, path, place string) string {
	switch place {
	case "run_plan":
		return "run-plan.json"
	case "system_append":
		return "system-append.md"
	case "first_message":
		return "first-message.md"
	case "repo_bundle":
		return "repo.bundle"
	}
	if path != "" {
		return path
	}
	return name
}

// actingFor is the requester of every job the driver stages.
var actingFor = ids.Derived("usr", "replicant-dev-user", "alice")

// scope sets RunSpec.run_scope, records the job's intake record in core (the fake core in isolated mode; the live
// core through its dev-only route in v0 mode, SIMPLIFY-D54; the worker's jobs exist in core for real) and computes
// spec_digest over the body without run_scope/trace_context (C-RUNNER 4.0.0).
func (e *env) scope(st *staged, o runOpts, audiences, scopes []string) error {
	repo := fmt.Sprint(e.gitea.id)
	st.scope = api.RunScope{RepoIDs: []string{repo}, Audiences: audiences, Scopes: scopes, ActingFor: actingFor}
	st.spec["run_scope"] = st.scope
	e.core.addJob(o.jobID, o.tenant, []string{repo}, actingFor)
	if err := e.registerJob(o.jobID, o.tenant, []string{repo}, actingFor); err != nil {
		return err
	}
	b, _ := json.Marshal(st.spec)
	d, err := jcsx.SpecDigest(b)
	if err != nil {
		return err
	}
	st.digest = d
	st.body = b
	return nil
}

// registerJob records, in v0 mode, a job id the driver has not recorded yet in the live core's intake
// (POST /v1/admin/dev/job-requests, dev seed token, SIMPLIFY-D54) so runner-local's run-scope admission finds it.
// Like the fake core's addJob, the first tenant a job id is staged for owns it (RC-SCOPE-05 stages the same job id
// for another tenant and expects 422).
func (e *env) registerJob(jobID, tenant string, repoIDs []string, actingFor string) error {
	if e.target != "v0" {
		return nil
	}
	e.mu.Lock()
	done := e.registered[jobID]
	e.mu.Unlock()
	if done {
		return nil
	}
	body := map[string]any{"job_id": jobID, "tenant_id": tenant, "repo_ids": repoIDs, "requester_principal_id": actingFor}
	if strings.HasPrefix(e.rb, "rb_") {
		body["release_bundle_id"] = e.rb
	}
	b, _ := json.Marshal(body)
	req, _ := http.NewRequest(http.MethodPost, e.coreURL+"/v1/admin/dev/job-requests", bytes.NewReader(b))
	req.Header.Set("Authorization", "Bearer "+e.seedToken)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("register job %s in core: %v", jobID, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 && resp.StatusCode != 201 {
		rb, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("register job %s in core: %d %s", jobID, resp.StatusCode, rb)
	}
	e.mu.Lock()
	if e.registered == nil {
		e.registered = map[string]bool{}
	}
	e.registered[jobID] = true
	e.mu.Unlock()
	return nil
}

// rescope rebuilds the body after a test changed st.spec (the scope itself is unchanged).
func (e *env) rescope(st *staged, o runOpts) {
	o = e.defaults(o)
	scopes := []string{"credentials:aws", "gateway:invoke"}
	if o.steering {
		scopes = append(scopes, "inbox:read")
	}
	_ = e.scope(st, o, []string{"ai-gateway", "runner-sandbox"}, scopes)
}

func (e *env) registerPolicy(ctx context.Context, st *staged, o runOpts) error {
	if e.shimToken == "" {
		return fmt.Errorf("no shim token")
	}
	put := func(path string, body any, idem string) error {
		b, _ := json.Marshal(body)
		req, _ := http.NewRequestWithContext(ctx, http.MethodPut, e.shim+path, bytes.NewReader(b))
		req.Header.Set("Authorization", "Bearer "+e.shimToken)
		req.Header.Set("Content-Type", "application/json")
		if idem != "" {
			req.Header.Set("Idempotency-Key", idem)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return err
		}
		defer resp.Body.Close()
		if resp.StatusCode >= 300 && resp.StatusCode != 409 {
			rb, _ := io.ReadAll(resp.Body)
			return fmt.Errorf("PUT %s: %d %s", path, resp.StatusCode, rb)
		}
		return nil
	}
	if err := put("/internal/v1/jobs/"+st.jobID+"/budget", map[string]any{"tenant_id": o.tenant, "job_budget_usd": 10}, ids.ULID("idk")); err != nil {
		return err
	}
	pol := map[string]any{"tenant_id": o.tenant, "job_id": st.jobID, "role": o.role, "release_bundle_id": e.rb,
		"models": []string{"anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5", "anthropic/claude-haiku-5-5"}, "budget_usd": o.budget,
		"max_concurrency": 2, "expires_at": time.Now().Add(2 * time.Hour).UTC().Format(time.RFC3339),
		"stub": map[string]string{"root": "scripts", "set": o.scriptSet, "key": o.scriptKey}}
	return put("/internal/v1/runs/"+st.runID+"/policy", pol, "")
}

func (e *env) create(st *staged) (int, map[string]any) {
	code, _, out := e.do(http.MethodPost, "/v1/runs", e.workerToken, `"`+st.key+`"`, st.body)
	return code, out
}

// wait polls GET /v1/runs/{id} until terminal and returns the observed phase sequence.
func (e *env) wait(st *staged, timeout time.Duration) (map[string]any, []string, error) {
	deadline := time.Now().Add(timeout)
	var phases []string
	var last map[string]any
	for time.Now().Before(deadline) {
		code, _, out := e.do(http.MethodGet, "/v1/runs/"+st.runID, e.workerToken, "", nil)
		if code == 200 {
			last = out
			ph, _ := out["phase"].(string)
			if len(phases) == 0 || phases[len(phases)-1] != ph {
				phases = append(phases, ph)
				fmt.Printf("  phase %s\n", ph)
			}
			if t, _ := out["terminal"].(bool); t {
				return out, phases, nil
			}
		}
		time.Sleep(time.Second)
	}
	return last, phases, fmt.Errorf("timeout; phases %v", phases)
}

func failureReason(st map[string]any) string {
	if f, ok := st["failure"].(map[string]any); ok {
		return fmt.Sprint(f["reason"])
	}
	return ""
}

// repoBundle builds the run's repo bundle as the worker's context.stage_inputs does: the wanted commits fetched
// from the SCM (the fake Gitea's bare repository; the real Gitea in v0 mode) under refs/replicant/*, then
// `git bundle create`. Cached per commit set.
func (e *env) repoBundle(refs map[string]string) ([]byte, error) {
	keys := make([]string, 0, len(refs))
	for r, sha := range refs {
		keys = append(keys, r+"="+sha)
	}
	sort.Strings(keys)
	ck := strings.Join(keys, ",")
	e.mu.Lock()
	if b, ok := e.bundles[ck]; ok {
		e.mu.Unlock()
		return b, nil
	}
	e.mu.Unlock()
	src := filepath.Join(e.gitea.root, e.gitea.owner, e.gitea.name+".git")
	auth := []string{"-c", "uploadpack.allowAnySHA1InWant=true"}
	if e.target == "v0" {
		pw, err := httpx.ReadToken(httpx.Env("GITEA_ADMIN_PASSWORD_FILE", ""))
		if err != nil {
			return nil, err
		}
		src = httpx.Env("GITEA_URL", "http://gitea:3300") + "/" + e.gitea.owner + "/" + e.gitea.name + ".git"
		auth = []string{"-c", "http.extraHeader=Authorization: Basic " + base64.StdEncoding.EncodeToString([]byte("gitea-admin:"+pw))}
	}
	dir, err := os.MkdirTemp("", "smoke-bundle-")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(dir)
	git := func(args ...string) error {
		cmd := exec.Command("git", args...)
		cmd.Dir = dir
		cmd.Env = append(os.Environ(), "GIT_TERMINAL_PROMPT=0")
		if out, err := cmd.CombinedOutput(); err != nil {
			return fmt.Errorf("git %s: %v %s", args[0], err, out)
		}
		return nil
	}
	fetch := append(append([]string{}, auth...), "fetch", "-q", "--no-tags", src)
	var names []string
	for r, sha := range refs {
		fetch = append(fetch, sha+":"+r)
		names = append(names, r)
	}
	sort.Strings(names)
	out := filepath.Join(dir, "repo.bundle")
	for _, step := range [][]string{{"init", "-q", "--bare"}, fetch, append([]string{"bundle", "create", "-q", out}, names...)} {
		if err := git(step...); err != nil {
			return nil, err
		}
	}
	b, err := os.ReadFile(out)
	if err != nil {
		return nil, err
	}
	e.mu.Lock()
	if e.bundles == nil {
		e.bundles = map[string][]byte{}
	}
	e.bundles[ck] = b
	e.mu.Unlock()
	return b, nil
}
