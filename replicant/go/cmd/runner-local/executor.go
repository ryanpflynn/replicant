package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"example.com/replicant/internal/runner/controller"
	"example.com/replicant/internal/runner/engine"
	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/s3x"
	"example.com/replicant/internal/runner/sandbox"
	"example.com/replicant/internal/runner/schemas"
	v1 "example.com/replicant/internal/runner/v1alpha1"
)

// Container names (C-IDS §8) in pod-group order (local-runner.md §1.2).
const (
	cPause      = "pause"
	cVolumeInit = "volume-init" // local-only fsGroup emulation, removed after it exits (DEVIATIONS)
	cGuard      = controller.CWorkspaceGuard
	cAgentd     = controller.CAgentd
	cWatchdog   = controller.CWatchdog
	cPrepare    = controller.CPrepare
	cAgent      = controller.CAgent
	cUploader   = controller.CUploader
)

var volumeNames = []string{"workspace", "home", "tools", "managed", "inputs", "out", "spool"}
var tmpfsVolumes = map[string]string{"ipc": "size=1m,mode=1777", "uploader-ipc": "size=64k,mode=1777", "agentd-state": "size=1m,mode=1777"}

const termPath = "/replicant/out/.termination/agent"

// Executor runs one goroutine per non-terminal run.
type Executor struct {
	s       *Server
	mu      sync.Mutex
	running map[string]bool
}

func (s *Server) runExecutor(ctx context.Context) {
	ex := &Executor{s: s, running: map[string]bool{}}
	start := func(id string) {
		ex.mu.Lock()
		defer ex.mu.Unlock()
		if ex.running[id] {
			return
		}
		ex.running[id] = true
		go func() {
			defer func() {
				ex.mu.Lock()
				delete(ex.running, id)
				ex.mu.Unlock()
			}()
			ex.loop(ctx, id)
		}()
	}
	// Restart recovery: resume every record that is not fully done.
	if recs, err := s.Store.List(); err == nil {
		for _, r := range recs {
			if !r.Local.ContainersGCed {
				start(r.AgentRun.Spec.RunID)
			}
		}
	}
	gc := time.NewTicker(time.Minute)
	defer gc.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case id := <-s.wake:
			start(id)
		case <-gc.C:
			s.gcRecords()
			if recs, err := s.Store.List(); err == nil {
				for _, r := range recs {
					if !r.Local.ContainersGCed {
						start(r.AgentRun.Spec.RunID)
					}
				}
			}
		}
	}
}

func (ex *Executor) loop(ctx context.Context, runID string) {
	log := ex.s.Log.With("run_id", runID)
	for {
		done, err := ex.s.reconcile(ctx, runID)
		if err != nil {
			log.Warn("reconcile error", "err", err)
		}
		if done {
			return
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Second):
		}
	}
}

func cname(r *Record, c string) string { return r.AgentRun.Name + "-" + c }

func (s *Server) labels(r *Record) map[string]string {
	ar := &r.AgentRun
	l := map[string]string{
		"com.docker.compose.project":         "replicant",
		v1.LabelManagedBy:                    v1.ManagedByValue,
		v1.LabelTenant:                       ar.Spec.TenantID,
		v1.LabelJobID:                        ar.Spec.JobID,
		v1.LabelRunID:                        ar.Spec.RunID,
		v1.LabelRole:                         string(ar.Spec.Role),
		v1.LabelWorkloadClass:                string(ar.Spec.WorkloadClass),
		v1.LabelIsolation:                    r.Local.Runtime,
		v1.LabelBundle:                       ar.Labels[v1.LabelBundle],
		v1.LabelNetworkProfile:               ar.Spec.NetworkProfile,
		v1.LabelActingFor:                    r.Local.Grant.ActingFor,
		v1.LabelWorkspaceMode:                string(ar.Spec.Workspace.Enforcement),
		v1.LabelWorkspaceBytes:               strconv.FormatInt(r.Local.WorkspaceBytes, 10),
		"replicant.example.com/local-runner": "true",
	}
	if ar.Spec.WorkflowType != "" {
		l[v1.LabelWorkflowType] = string(ar.Spec.WorkflowType)
	}
	return l
}

func (s *Server) stateMount(runID, sub, target string, ro bool) engine.Mount {
	if s.Cfg.StateHostDir != "" {
		return engine.Mount{Type: "bind", Source: filepath.Join(s.Cfg.StateHostDir, "runs", runID, sub), Target: target, ReadOnly: ro}
	}
	return engine.Mount{Type: "volume", Source: s.Cfg.StateVolume, Target: target, ReadOnly: ro,
		VolumeOptions: &engine.VolumeOptions{Subpath: "runs/" + runID + "/" + sub}}
}

func vol(r *Record, name, target string, ro bool) engine.Mount {
	return engine.Mount{Type: "volume", Source: r.AgentRun.Name + "-" + name, Target: target, ReadOnly: ro}
}

func volSub(r *Record, name, sub, target string, ro bool) engine.Mount {
	return engine.Mount{Type: "volume", Source: r.AgentRun.Name + "-" + name, Target: target, ReadOnly: ro,
		VolumeOptions: &engine.VolumeOptions{Subpath: sub}}
}

func (s *Server) traceparent(r *Record) (string, string) {
	if tc := r.AgentRun.Spec.TraceContext; tc != nil {
		return tc.Traceparent, tc.Tracestate
	}
	span := strings.TrimPrefix(ids.Derived("x", r.AgentRun.Spec.RunID, "sandbox"), "x_")
	return "00-" + ids.JobTraceID(r.AgentRun.Spec.JobID) + "-" + fmt.Sprintf("%016x", hash64(span)) + "-01", ""
}

func hash64(s string) uint64 {
	var h uint64 = 1469598103934665603
	for i := 0; i < len(s); i++ {
		h ^= uint64(s[i])
		h *= 1099511628211
	}
	if h == 0 {
		h = 1
	}
	return h
}

// baseEnv is the C-IDS §7 sandbox set plus the runner-local.yaml sandbox.env.
func (s *Server) baseEnv(r *Record) []string {
	ar := &r.AgentRun
	tp, ts := s.traceparent(r)
	env := map[string]string{
		"REPLICANT_ENV": s.Cfg.Env, "REPLICANT_TENANT_ID": ar.Spec.TenantID, "REPLICANT_JOB_ID": ar.Spec.JobID,
		"REPLICANT_RUN_ID": ar.Spec.RunID, "REPLICANT_ROLE": string(ar.Spec.Role), "REPLICANT_RELEASE_BUNDLE_ID": ar.Spec.ReleaseBundleID,
		"REPLICANT_ARTIFACT_PREFIX": ar.Spec.Artifacts.ArtifactsPrefix, "REPLICANT_INPUTS_URI": ar.Spec.Inputs.ManifestURI,
		"TRACEPARENT": tp, "REPLICANT_WORKSPACE_DIR": sandbox.WorkspaceDir,
		"REPLICANT_WORKSPACE_LIMIT_BYTES": strconv.FormatInt(r.Local.WorkspaceBytes, 10),
		"REPLICANT_WORKFLOW_TYPE":         string(ar.Spec.WorkflowType), "REPLICANT_WORKFLOW_ID": ar.Spec.WorkflowID,
		"REPLICANT_LOGICAL_ACTIVITY_ID": ar.Spec.LogicalActivityID,
		// C-IDS §5 sandbox value (RC-ENV-02): service.name, env, tenant, job, run, role, bundle.
		"OTEL_RESOURCE_ATTRIBUTES": fmt.Sprintf("service.name=replicant-sandbox,replicant.env=%s,replicant.tenant_id=%s,replicant.job_id=%s,replicant.run_id=%s,replicant.role=%s,replicant.release_bundle_id=%s",
			s.Cfg.Env, ar.Spec.TenantID, ar.Spec.JobID, ar.Spec.RunID, ar.Spec.Role, ar.Spec.ReleaseBundleID),
	}
	if ts != "" {
		env["TRACESTATE"] = ts
	}
	if s.Cfg.EPOTLP != "" {
		env["OTEL_EXPORTER_OTLP_ENDPOINT"] = s.Cfg.EPOTLP
	}
	for k, v := range s.Cfg.SandboxEnv {
		env[k] = v
	}
	if np, ok := env["NO_PROXY"]; !ok || !strings.Contains(np, "127.0.0.1") {
		env["NO_PROXY"] = strings.Trim(np+",127.0.0.1,localhost", ",")
	}
	out := make([]string, 0, len(env))
	for k, v := range env {
		out = append(out, k+"="+v)
	}
	return out
}

func boolp(b bool) *bool { return &b }

// selinuxLevel gives every container of one pod group the same MCS level (as a Kubernetes pod shares one
// SELinux context), so the group's shared tmpfs volumes (ipc, uploader-ipc) are usable across its containers
// while other runs stay separated.
func selinuxLevel(r *Record) string {
	h := hash64(r.AgentRun.Spec.RunID)
	a, b := int(h%1024), int((h>>10)%1024)
	if a == b {
		b = (b + 1) % 1024
	}
	if a > b {
		a, b = b, a
	}
	return fmt.Sprintf("label=level:s0:c%d,c%d", a, b)
}

func (s *Server) secOpts(r *Record) []string {
	if s.SELinux {
		return []string{"no-new-privileges", selinuxLevel(r)}
	}
	return []string{"no-new-privileges"}
}

func (s *Server) hostConfig(r *Record, user string, mounts []engine.Mount) engine.HostConfig {
	hc := engine.HostConfig{
		NetworkMode:    "container:" + cname(r, cPause),
		Mounts:         mounts,
		ReadonlyRootfs: true,
		CapDrop:        []string{"ALL"},
		SecurityOpt:    s.secOpts(r),
		PidsLimit:      4096,
		GroupAdd:       []string{"10002"},
	}
	if s.Cfg.Runtime != "" && s.Cfg.Runtime != "runc" {
		hc.Runtime = s.Cfg.Runtime
	}
	return hc
}

// containerSpec renders one container of the pod group.
func (s *Server) containerSpec(r *Record, c string) (string, engine.ContainerConfig) {
	ar := &r.AgentRun
	runID := ar.Spec.RunID
	env := s.baseEnv(r)
	cfgMount := s.stateMount(runID, "config", "/etc/replicant", true)
	ipc := vol(r, "ipc", sandbox.IPCDir, false)
	upIPC := vol(r, "uploader-ipc", "/run/replicant/uploader-ipc", false)
	stop := int(ar.Spec.Timeouts.FinalizeSeconds) + s.Cfg.StopGraceExtra
	cc := engine.ContainerConfig{Labels: s.labels(r), Env: env, StopTimeout: &stop}
	switch c {
	case cPause:
		cc.Image = s.Cfg.PauseImage
		cc.Hostname = ar.Name
		cc.HostConfig = engine.HostConfig{NetworkMode: s.Cfg.SandboxNetwork, DNS: s.Cfg.SandboxDNS, ReadonlyRootfs: true,
			CapDrop: []string{"ALL"}, SecurityOpt: s.secOpts(r), PidsLimit: 16}
		cc.Env = nil
		return cc.Image, cc
	case cVolumeInit:
		cc.Image = s.Cfg.SidecarsImage
		cc.User = "0:0"
		cc.Cmd = []string{"/usr/local/bin/sandbox-sidecars", "volume-init"}
		var mounts []engine.Mount
		for _, v := range volumeNames {
			mounts = append(mounts, vol(r, v, "/v/"+v, false))
		}
		cc.HostConfig = engine.HostConfig{NetworkMode: "none", Mounts: mounts, CapDrop: []string{"ALL"},
			CapAdd: []string{"CHOWN", "FOWNER", "DAC_OVERRIDE"}, SecurityOpt: s.secOpts(r), ReadonlyRootfs: true}
		cc.Env = nil
		return cc.Image, cc
	case cGuard:
		cc.Image = s.Cfg.SidecarsImage
		cc.User = "10001:10001"
		cc.Cmd = []string{"/usr/local/bin/sandbox-sidecars", "workspace-guard"}
		cc.Env = append(cc.Env, "REPLICANT_WORKSPACE_ENFORCEMENT="+string(ar.Spec.Workspace.Enforcement))
		cc.HostConfig = s.hostConfig(r, cc.User, []engine.Mount{vol(r, "workspace", sandbox.WorkspaceDir, false)})
	case cAgentd:
		cc.Image = r.Local.CLIImage
		cc.User = "10000:10000"
		cc.Cmd = []string{"/opt/replicant/bin/replicant-agentd"}
		cc.Env = append(cc.Env, "REPLICANT_RUN_TOKEN_SEED_DIR="+sandbox.TokenSeedDir, "REPLICANT_RUN_TOKEN_DIR="+sandbox.TokenDir,
			"REPLICANT_RUN_TOKEN_FILE="+sandbox.TokenDir+"/runner-sandbox", "REPLICANT_UPLOADER_SOCKET="+sandbox.UploaderSocket)
		cc.HostConfig = s.hostConfig(r, cc.User, []engine.Mount{
			s.stateMount(runID, "tokens", sandbox.TokenSeedDir, true),
			vol(r, "agentd-state", sandbox.TokenDir, false), ipc, upIPC, cfgMount})
		cc.HostConfig.RestartPolicy = &engine.RestartPolicy{Name: "on-failure", MaximumRetryCount: 5}
		cc.HostConfig.Memory = 512 << 20
	case cWatchdog:
		cc.Image = s.Cfg.SidecarsImage
		cc.User = "10003:10003"
		cc.Cmd = []string{"/usr/local/bin/sandbox-sidecars", "watchdog"}
		cc.HostConfig = s.hostConfig(r, cc.User, []engine.Mount{vol(r, "workspace", sandbox.WorkspaceDir, true), ipc})
		cc.HostConfig.RestartPolicy = &engine.RestartPolicy{Name: "on-failure", MaximumRetryCount: 5}
	case cPrepare:
		cc.Image = r.Local.CLIImage
		cc.User = "10001:10001"
		cc.Cmd = []string{"/opt/replicant/bin/replicant-entrypoint", "prepare"}
		cc.Env = append(cc.Env, "HOME="+sandbox.HomeDir, "REPLICANT_TOOLS_OUT=/replicant/tools", "REPLICANT_MANAGED_OUT=/replicant/managed",
			"REPLICANT_CLI_IMAGE="+r.Local.CLIImage)
		cc.HostConfig = s.hostConfig(r, cc.User, []engine.Mount{
			vol(r, "tools", "/replicant/tools", false), vol(r, "inputs", sandbox.InputsDir, false),
			vol(r, "managed", "/replicant/managed", false), vol(r, "home", sandbox.HomeDir, false),
			vol(r, "workspace", sandbox.WorkspaceDir, false), ipc, cfgMount})
		cc.HostConfig.Tmpfs = map[string]string{"/tmp": "rw,size=512m,mode=1777"}
	case cAgent:
		cc.Image = r.Local.AgentImage
		cc.User = "10001:10001"
		cc.Entrypoint = []string{"/opt/replicant/bin/replicant-entrypoint"}
		cc.Cmd = []string{"run"}
		cc.WorkingDir = sandbox.RepoDir
		cc.Env = append(cc.Env, "HOME="+sandbox.HomeDir, "REPLICANT_TERMINATION_MESSAGE_PATH="+termPath)
		for k, v := range r.Local.EnvDefaults {
			cc.Env = append(cc.Env, k+"="+v)
		}
		cc.HostConfig = s.hostConfig(r, cc.User, []engine.Mount{
			vol(r, "tools", sandbox.ToolsDir, true),
			volSub(r, "managed", "claude-code", sandbox.ManagedClaudeDir, true),
			volSub(r, "managed", "replicant", "/etc/replicant", true),
			vol(r, "inputs", sandbox.InputsDir, true), vol(r, "home", sandbox.HomeDir, false),
			s.stateMount(runID, "canary", sandbox.HomeDir+"/.aws", true), // decoy credentials (security.go)
			vol(r, "workspace", sandbox.WorkspaceDir, false), vol(r, "out", sandbox.OutDir, false),
			vol(r, "spool", sandbox.SpoolDir, false), ipc,
		})
		// prompt: agent-only memory-backed volume (cli-invocation.md §1.1), never uploaded
		cc.HostConfig.Tmpfs = map[string]string{"/tmp": "rw,size=1g,mode=1777", sandbox.PromptDir: "rw,size=4m,mode=1777"}
		cc.HostConfig.Memory = r.Local.MemoryBytes
		cc.HostConfig.NanoCpus = int64(r.Local.CPUs * 1e9)
		cc.StopSignal = "SIGTERM"
	case cUploader:
		cc.Image = s.Cfg.SidecarsImage
		cc.User = "10003:10003"
		cc.Cmd = []string{"/usr/local/bin/sandbox-sidecars", "uploader"}
		cc.Env = append(cc.Env, "REPLICANT_UPLOADER_SOCKET="+sandbox.UploaderSocket, "REPLICANT_S3_VERIFY_READBACK=true")
		cc.HostConfig = s.hostConfig(r, cc.User, []engine.Mount{
			vol(r, "out", sandbox.OutDir, true), vol(r, "spool", sandbox.SpoolDir, true), ipc, upIPC, cfgMount})
		cc.HostConfig.Memory = 512 << 20
	}
	return cc.Image, cc
}

// bootstrap renders bootstrap.json (replicant.sandbox-bootstrap/v1 + the ws02 section).
func (s *Server) bootstrap(r *Record) sandbox.Bootstrap {
	ar := &r.AgentRun
	fd := 0
	if ar.Spec.Repo.FetchDepth != nil {
		fd = int(*ar.Spec.Repo.FetchDepth)
	}
	b := sandbox.Bootstrap{
		Schema: "replicant.sandbox-bootstrap/v1", Env: s.Cfg.Env, RunID: ar.Spec.RunID,
		InputsManifestURI: ar.Spec.Inputs.ManifestURI, InputsManifestSHA256: ar.Spec.Inputs.ManifestSHA256,
		Endpoints: sandbox.Endpoints{RunnerSandbox: s.Cfg.EPRunnerSandbox, AIGateway: s.Cfg.EPAIGateway,
			MemoryMCP: s.Cfg.EPMemoryMCP, ContextMCP: s.Cfg.EPContextMCP, CodeSearchMCP: strings.TrimSuffix(s.Cfg.EPContextMCP, "/") + "/code-search",
			RepoMirror: s.Cfg.EPRepoMirror, OTLP: s.Cfg.EPOTLP, LogTransport: "stdout"},
		Repo: sandbox.BootstrapRepo{RepoID: ar.Spec.Repo.RepoID, BaseSHA: ar.Spec.Repo.BaseSHA, FetchDepth: fd,
			Provider: string(ar.Spec.Repo.Provider), Host: ar.Spec.Repo.Host, Owner: ar.Spec.Repo.Owner, Name: ar.Spec.Repo.Name},
		Local: &sandbox.LocalBootstrap{S3Endpoint: s.Cfg.S3PublicEndpoint, S3Region: s.Cfg.S3Region, VerifyReadback: true, SendChecksum: s.Cfg.S3SendChecksum},
	}
	if v, ok := s.Cfg.SandboxEnv["GOPROXY"]; ok {
		b.PackageMirrors = map[string]string{"goproxy": v}
	}
	ws := &sandbox.WS02{MaxOutputBytes: ar.Spec.Artifacts.MaxOutputBytes, Role: string(ar.Spec.Role),
		TenantID: ar.Spec.TenantID, JobID: ar.Spec.JobID, ReleaseBundleID: ar.Spec.ReleaseBundleID, AgentRun: ar.Name}
	ws.ArtifactPrefixes.Artifacts = ar.Spec.Artifacts.ArtifactsPrefix
	ws.ArtifactPrefixes.Transcripts = ar.Spec.Artifacts.TranscriptsPrefix
	ws.ArtifactPrefixes.Session = ar.Spec.Artifacts.SessionPrefix
	ws.Workspace.LimitBytes = r.Local.WorkspaceBytes
	ws.Workspace.Enforcement = string(ar.Spec.Workspace.Enforcement)
	ws.Timeouts.StartupSeconds = int(ar.Spec.Timeouts.StartupSeconds)
	ws.Timeouts.ActiveDeadlineSeconds = int(ar.Spec.Timeouts.ActiveDeadlineSeconds)
	ws.Timeouts.FinalizeSeconds = int(ar.Spec.Timeouts.FinalizeSeconds)
	ws.RequiredOutputs = requiredOutputs(string(ar.Spec.Role))
	ws.Redaction.RulesetVersion = "redact-local-0"
	b.WS02 = ws
	return b
}

func requiredOutputs(role string) []string {
	switch role {
	case "implementer", "repair":
		return []string{"diff_bundle"}
	case "qa":
		return []string{"qa_report"}
	case "planner":
		return []string{"plan"}
	case "reviewer":
		return []string{"review_report"}
	}
	return []string{}
}

// ensureImage implements pull_policy if-not-present.
func (s *Server) ensureImage(ctx context.Context, ref string) error {
	ok, err := s.Engine.ImageExists(ctx, ref)
	if err == nil && ok {
		return nil
	}
	if s.Cfg.PullPolicy == "never" {
		return fmt.Errorf("manifest unknown: image %s not present (pull policy never)", ref)
	}
	if err := s.Engine.Pull(ctx, ref); err != nil {
		msg := err.Error()
		if strings.Contains(msg, "manifest unknown") || strings.Contains(msg, "not found") || strings.Contains(msg, "name unknown") ||
			strings.HasPrefix(ref, "localhost/") {
			return fmt.Errorf("manifest unknown: %s: %s", ref, msg)
		}
		return fmt.Errorf("pull %s: %s", ref, msg)
	}
	return nil
}

func (s *Server) createStart(ctx context.Context, r *Record, c string) (string, error) {
	name := cname(r, c)
	if ins, err := s.Engine.InspectContainer(ctx, name); err != nil {
		return "", err
	} else if ins != nil {
		if ins.State.Status == "created" {
			return ins.ID, s.Engine.Start(ctx, ins.ID)
		}
		return ins.ID, nil
	}
	_, cfg := s.containerSpec(r, c)
	id, err := s.Engine.Create(ctx, name, cfg)
	if err != nil {
		if engine.IsConflict(err) {
			ins, ierr := s.Engine.InspectContainer(ctx, name)
			if ierr == nil && ins != nil {
				return ins.ID, nil
			}
		}
		return "", fmt.Errorf("create %s: %w", name, err)
	}
	return id, s.Engine.Start(ctx, id)
}

func (s *Server) inspect(ctx context.Context, r *Record, c string) *engine.Inspect {
	ins, _ := s.Engine.InspectContainer(ctx, cname(r, c))
	return ins
}

// reconcile performs one idempotent pass for a run; it returns true when the run needs no further passes.
func (s *Server) reconcile(ctx context.Context, runID string) (bool, error) {
	rec, err := s.Store.Get(runID)
	if err != nil || rec == nil {
		return true, err
	}
	if rec.AgentRun.Status.Phase.IsTerminal() {
		return s.reconcileTerminal(ctx, rec)
	}
	now := time.Now()
	ar := &rec.AgentRun
	// Accepted + credentials (controller.md §5).
	if rec.Local.Stage == "accepted" {
		if err := s.prepareRunState(rec); err != nil {
			return false, s.failNow(rec, v1.ReasonCredentialIssueFailed, err.Error())
		}
		rec, err = s.Store.Update(runID, func(rr *Record) error {
			setCond(&rr.AgentRun, v1.CondAccepted, metav1.ConditionTrue, "Accepted", "", now)
			setCond(&rr.AgentRun, v1.CondCredentialsIssued, metav1.ConditionTrue, "Issued", "", now)
			rr.AgentRun.Status.AcceptedAt = mt(now)
			rr.Local.Stage = "credentials"
			return nil
		})
		if err != nil {
			return false, err
		}
		s.queueNotification(rec, string(v1.PhasePending), "")
	}
	ar = &rec.AgentRun
	if !rec.Local.PodCreateMark && s.testHold(rec) {
		return s.deriveAndApply(ctx, rec)
	}
	// Pod creation (marker first, at-most-once).
	if !rec.Local.PodCreateMark && !ar.Spec.CancelRequested {
		for _, img := range []string{s.Cfg.PauseImage, s.Cfg.SidecarsImage, rec.Local.CLIImage, rec.Local.AgentImage} {
			if err := s.ensureImage(ctx, img); err != nil {
				_, _ = s.Store.Update(runID, func(rr *Record) error { rr.Local.PullError, rr.Local.PullContainer = err.Error(), cPrepare; return nil })
				rec, _ = s.Store.Get(runID)
				return s.deriveAndApply(ctx, rec)
			}
		}
		rec, err = s.Store.Update(runID, func(rr *Record) error {
			rr.Local.PodCreateMark, rr.Local.PullError = true, ""
			setCond(&rr.AgentRun, v1.CondScheduled, metav1.ConditionUnknown, "Waiting", "", now)
			return nil
		})
		if err != nil {
			return false, err
		}
		if err := s.createPodGroup(ctx, rec); err != nil {
			s.Log.Warn("pod group creation failed", "run_id", runID, "err", err)
			_, _ = s.Store.Update(runID, func(rr *Record) error { rr.Local.FailureMessage = "pod create: " + err.Error(); return nil })
			return false, s.failNow(rec, v1.ReasonControllerError, "sandbox creation failed: "+truncate(err.Error(), 500))
		}
		rec, _ = s.Store.Get(runID)
	}
	if rec.Local.PodCreateMark {
		if err := s.advancePodGroup(ctx, rec); err != nil {
			s.Log.Warn("advance failed", "run_id", runID, "err", err)
		}
		rec, _ = s.Store.Get(runID)
	}
	return s.deriveAndApply(ctx, rec)
}

// prepareRunState mints the per-audience tokens and renders bootstrap.json into the state directory.
func (s *Server) prepareRunState(r *Record) error {
	runID := r.AgentRun.Spec.RunID
	set, err := signSet(s.Signer, s.Cfg.Issuer, r.AgentRun.Spec.RunGrant, &r.Local.Grant, r.Local.SteeringLive, time.Now(), nil)
	if err != nil {
		return err
	}
	if err := writeTokenFiles(s.Cfg.StateDir, runID, set); err != nil {
		return err
	}
	cfgDir := filepath.Join(s.Cfg.StateDir, "runs", runID, "config")
	if err := os.MkdirAll(cfgDir, 0o755); err != nil {
		return err
	}
	if err := writeCanary(s.Cfg.StateDir, runID); err != nil {
		return err
	}
	b := s.bootstrap(r)
	return sandbox.WriteJSONAtomic(filepath.Join(cfgDir, "bootstrap.json"), b, 0o644)
}

func (s *Server) createPodGroup(ctx context.Context, r *Record) error {
	lbl := s.labels(r)
	for _, v := range volumeNames {
		// explicit ownership: podman otherwise chowns an unowned volume to whichever container mounts it first
		if err := s.Engine.VolumeCreate(ctx, r.AgentRun.Name+"-"+v, lbl, map[string]string{"o": "uid=10001,gid=10002"}); err != nil {
			return fmt.Errorf("volume %s: %w", v, err)
		}
	}
	for v, o := range tmpfsVolumes {
		if s.SELinux {
			// a socket in a shared tmpfs volume needs the pod group's file context (container_t may not create
			// sock_file in the default tmpfs label)
			o += `,context="system_u:object_r:container_file_t:` + strings.TrimPrefix(selinuxLevel(r), "label=level:") + `"`
		}
		if err := s.Engine.VolumeCreate(ctx, r.AgentRun.Name+"-"+v, lbl, map[string]string{"type": "tmpfs", "device": "tmpfs", "o": o}); err != nil {
			return fmt.Errorf("volume %s: %w", v, err)
		}
	}
	// fsGroup emulation: chown the run's volumes, then remove the helper.
	id, err := s.createStart(ctx, r, cVolumeInit)
	if err != nil {
		return err
	}
	for i := 0; i < 120; i++ {
		ins, _ := s.Engine.InspectContainer(ctx, id)
		if ins == nil || exited(ins) {
			if ins != nil && ins.State.ExitCode != 0 {
				logs, _ := s.Engine.Logs(ctx, id, 20)
				return fmt.Errorf("volume-init exit %d: %s", ins.State.ExitCode, strings.TrimSpace(logs))
			}
			break
		}
		time.Sleep(250 * time.Millisecond)
	}
	_ = s.Engine.Remove(ctx, id)
	if _, err := s.createStart(ctx, r, cPause); err != nil {
		return err
	}
	now := time.Now()
	_, err = s.Store.Update(r.AgentRun.Spec.RunID, func(rr *Record) error {
		rr.AgentRun.Status.ScheduledAt = mt(now)
		setCond(&rr.AgentRun, v1.CondScheduled, metav1.ConditionTrue, "PodScheduled", "", now)
		rr.Local.Stage = "scheduled"
		return nil
	})
	return err
}

// exited: the container has terminated (podman reports "stopping" with Running=false while a stop is in progress).
func exited(ins *engine.Inspect) bool {
	if ins == nil || ins.State.Running {
		return false
	}
	switch ins.State.Status {
	case "exited", "stopped", "dead":
		return true
	}
	return false
}

// advancePodGroup starts the next containers in order (local-runner.md §1.2 ordering).
func (s *Server) advancePodGroup(ctx context.Context, r *Record) error {
	ar := &r.AgentRun
	runID := ar.Spec.RunID
	if ar.Spec.CancelRequested || r.Local.Intent != "" {
		return s.terminateIfNeeded(ctx, r)
	}
	switch r.Local.Stage {
	case "scheduled":
		if _, err := s.createStart(ctx, r, cGuard); err != nil {
			return err
		}
		_, err := s.Store.Update(runID, func(rr *Record) error { rr.Local.Stage = "guard"; return nil })
		return err
	case "guard":
		ins := s.inspect(ctx, r, cGuard)
		if !exited(ins) {
			return nil
		}
		code := ins.State.ExitCode
		_, _ = s.Store.Update(runID, func(rr *Record) error { rr.Local.GuardExit = &code; return nil })
		if code != 0 {
			return nil
		}
		for _, c := range []string{cAgentd, cWatchdog} {
			if _, err := s.createStart(ctx, r, c); err != nil {
				return err
			}
		}
		_, err := s.Store.Update(runID, func(rr *Record) error { rr.Local.Stage = "sidecars"; return nil })
		return err
	case "sidecars":
		ins := s.inspect(ctx, r, cAgentd)
		if ins == nil || !ins.State.Running {
			return nil
		}
		code, err := s.Engine.Exec(ctx, ins.ID, []string{"/opt/replicant/bin/replicant-agentd", "ready"}, "")
		if err != nil || code != 0 {
			return nil // startupProbe not passed yet
		}
		if _, err := s.createStart(ctx, r, cPrepare); err != nil {
			return err
		}
		_, err = s.Store.Update(runID, func(rr *Record) error { rr.Local.Stage = "prepare"; return nil })
		return err
	case "prepare":
		ins := s.inspect(ctx, r, cPrepare)
		if !exited(ins) {
			return nil
		}
		code := ins.State.ExitCode
		now := time.Now()
		msg := ""
		if code != 0 {
			logs, _ := s.Engine.Logs(ctx, ins.ID, 5)
			msg = lastLine(logs)
		}
		_, _ = s.Store.Update(runID, func(rr *Record) error {
			rr.Local.PrepareExit = &code
			if code == 0 {
				rr.AgentRun.Status.WorkspaceReadyAt = mt(now)
				setCond(&rr.AgentRun, v1.CondWorkspaceReady, metav1.ConditionTrue, "Prepared", "", now)
			} else {
				rr.Local.FailureMessage = truncate(msg, 1024)
			}
			return nil
		})
		if code != 0 {
			return nil
		}
		for _, c := range []string{cAgent, cUploader} {
			if _, err := s.createStart(ctx, r, c); err != nil {
				return err
			}
		}
		_, err := s.Store.Update(runID, func(rr *Record) error { rr.Local.Stage = "main"; return nil })
		return err
	case "main":
		if !r.Local.AgentStarted {
			ins := s.inspect(ctx, r, cAgent)
			if ins != nil && ins.State.Running {
				if code, err := s.Engine.Exec(ctx, ins.ID, []string{"test", "-f", sandbox.MarkerAgentStarted}, ""); err == nil && code == 0 {
					now := time.Now()
					_, _ = s.Store.Update(runID, func(rr *Record) error {
						rr.Local.AgentStarted = true
						rr.AgentRun.Status.AgentStartedAt = mt(now)
						setCond(&rr.AgentRun, v1.CondAgentRunning, metav1.ConditionTrue, "Started", "", now)
						return nil
					})
				}
			}
		}
		// Deadline (activeDeadlineSeconds from agent start).
		if ar.Status.AgentStartedAt != nil && ar.Status.AgentExitedAt == nil &&
			time.Since(ar.Status.AgentStartedAt.Time) > time.Duration(ar.Spec.Timeouts.ActiveDeadlineSeconds)*time.Second {
			now := time.Now()
			_ = s.Inbox.WriteControl(ctx, ar.Spec.Artifacts.ArtifactsPrefix, ar.Spec.TenantID, "deadline")
			r, _ = s.Store.Update(runID, func(rr *Record) error { rr.Local.Intent = "deadline"; rr.Local.IntentAt = &now; return nil })
			return s.terminateIfNeeded(ctx, r)
		}
		// Agent gone but the uploader still waits (an entrypoint that died writes no markers): tell it to finalize.
		if agent := s.inspect(ctx, r, cAgent); exited(agent) {
			if up := s.inspect(ctx, r, cUploader); up != nil && up.State.Running && time.Since(agent.State.FinishedTime()) > 20*time.Second {
				_ = s.Engine.Kill(ctx, up.ID, "SIGTERM")
			}
		}
		// Uploader done → stop the native sidecars (the pod completes).
		if up := s.inspect(ctx, r, cUploader); exited(up) && !r.Local.StopSent {
			go s.stopContainers(context.Background(), r, []string{cAgentd, cWatchdog, cPause}, 5)
			_, _ = s.Store.Update(runID, func(rr *Record) error { rr.Local.StopSent = true; return nil })
		}
	}
	return nil
}

// terminateIfNeeded performs the graceful delete of controller.md §6.3 (cancel or deadline).
func (s *Server) terminateIfNeeded(ctx context.Context, r *Record) error {
	ar := &r.AgentRun
	if r.Local.StopSent {
		// After agent and uploader exited, stop the sidecars.
		agent, up := s.inspect(ctx, r, cAgent), s.inspect(ctx, r, cUploader)
		if (agent == nil || exited(agent)) && (up == nil || exited(up)) {
			go s.stopContainers(context.Background(), r, []string{cAgentd, cWatchdog, cPause}, 5)
		}
		return nil
	}
	if ar.Spec.CancelRequested && ar.Spec.CancelReason != v1.CancelReasonSecurity && r.Local.Intent != "deadline" {
		// 2a: wait for the reason to reach agentd (a lease renewal after the control write, or 35 s).
		if r.Local.ControlAt != nil && time.Since(*r.Local.ControlAt) < 35*time.Second &&
			!(r.Local.LeaseAt != nil && r.Local.LeaseAt.After(*r.Local.ControlAt)) && s.inspect(ctx, r, cAgent) != nil {
			return nil
		}
	}
	grace := int(ar.Spec.Timeouts.FinalizeSeconds) + s.Cfg.StopGraceExtra
	_, err := s.Store.Update(ar.Spec.RunID, func(rr *Record) error {
		rr.Local.StopSent = true
		if rr.Local.Intent == "" {
			rr.Local.Intent = "cancel"
			now := time.Now()
			rr.Local.IntentAt = &now
		}
		return nil
	})
	if err != nil {
		return err
	}
	go s.stopContainers(context.Background(), r, []string{cAgent, cUploader, cPrepare, cGuard}, grace)
	return nil
}

func (s *Server) stopContainers(ctx context.Context, r *Record, names []string, grace int) {
	var wg sync.WaitGroup
	for _, c := range names {
		wg.Add(1)
		go func(c string) {
			defer wg.Done()
			_ = s.Engine.Stop(ctx, cname(r, c), grace)
		}(c)
	}
	wg.Wait()
}

// observe builds the Derive observation from container state (the Pod is synthesized from the pod group).
func (s *Server) observe(ctx context.Context, r *Record) controller.Observation {
	ar := copyAR(&r.AgentRun)
	o := controller.Observation{Now: time.Now(), Run: ar, PodCreateMarked: r.Local.PodCreateMark}
	switch r.Local.Intent {
	case "deadline":
		o.Intent = controller.IntentDeadline
	case "cancel":
		o.Intent = controller.IntentCancel
	}
	if r.Local.AcceptFailure != "" {
		o.AcceptFailure = v1.FailureReason(r.Local.AcceptFailure)
	}
	pause := s.inspect(ctx, r, cPause)
	if !r.Local.PodCreateMark {
		if r.Local.PullError != "" {
			// Image pull failing before any container exists: present it as a waiting init container.
			pod := &corev1.Pod{}
			pod.Status.InitContainerStatuses = []corev1.ContainerStatus{{Name: cPrepare,
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: "ErrImagePull", Message: r.Local.PullError}}}}
			o.Pod = pod
		}
		return o
	}
	if pause == nil && r.Local.Stage != "credentials" {
		// The pod group vanished (or was never created after the marker).
		return o
	}
	pod := &corev1.Pod{}
	if pause != nil {
		pod.Spec.NodeName = "local"
	}
	status := func(name string) *corev1.ContainerStatus {
		ins := s.inspect(ctx, r, name)
		if ins == nil {
			return nil
		}
		cs := corev1.ContainerStatus{Name: name}
		switch {
		case ins.State.Running:
			cs.State.Running = &corev1.ContainerStateRunning{StartedAt: metav1.NewTime(ins.State.StartedTime())}
		case exited(ins):
			t := &corev1.ContainerStateTerminated{ExitCode: int32(ins.State.ExitCode), FinishedAt: metav1.NewTime(ins.State.FinishedTime())}
			if ins.State.OOMKilled {
				t.Reason = "OOMKilled"
			}
			cs.State.Terminated = t
		default:
			cs.State.Waiting = &corev1.ContainerStateWaiting{Reason: "ContainerCreating"}
		}
		return &cs
	}
	for _, c := range []string{cGuard, cPrepare} {
		if cs := status(c); cs != nil {
			pod.Status.InitContainerStatuses = append(pod.Status.InitContainerStatuses, *cs)
		}
	}
	for _, c := range []string{cAgent, cUploader} {
		if cs := status(c); cs != nil {
			if c == cAgent {
				cs.Started = boolp(r.Local.AgentStarted)
			}
			pod.Status.ContainerStatuses = append(pod.Status.ContainerStatuses, *cs)
		}
	}
	o.Pod = pod
	// Run lease (runner-service.md §7.3): Running/Finalizing with a stale lease while agentd still runs.
	if (ar.Status.Phase == v1.PhaseRunning || ar.Status.Phase == v1.PhaseFinalizing) && r.Local.LeaseAt != nil &&
		time.Since(*r.Local.LeaseAt) > time.Duration(s.Cfg.LeaseSeconds)*time.Second {
		if agentd := s.inspect(ctx, r, cAgentd); agentd != nil && agentd.State.Running {
			o.NodeLost = true
		}
	}
	if up := s.inspect(ctx, r, cUploader); exited(up) {
		s.observeManifest(ctx, r, &o)
	}
	return o
}

type manifestDoc struct {
	Producer struct {
		RunID string `json:"run_id"`
	} `json:"producer"`
	Partial bool `json:"partial"`
	Result  *struct {
		Key string `json:"key"`
	} `json:"result"`
	Artifacts []struct {
		BucketClass string `json:"bucket_class"`
		Key         string `json:"key"`
		SizeBytes   int64  `json:"size_bytes"`
	} `json:"artifacts"`
}

// observeManifest verifies the committed manifest (controller.md §9 "Verification").
func (s *Server) observeManifest(ctx context.Context, r *Record, o *controller.Observation) {
	ab, ak, _ := s3x.ParseURI(r.AgentRun.Spec.Artifacts.ArtifactsPrefix)
	body, _, err := s.S3.Get(ctx, ab, ak+"manifest.json")
	if err != nil {
		return
	}
	if err := schemas.Validate(schemas.RunManifest, body); err != nil {
		s.Log.Warn("manifest invalid", "run_id", r.AgentRun.Spec.RunID, "err", err)
		return
	}
	var m manifestDoc
	if json.Unmarshal(body, &m) != nil || m.Producer.RunID != r.AgentRun.Spec.RunID {
		return
	}
	for _, a := range m.Artifacts {
		b := s3x.Bucket(s.Cfg.Org, s.Cfg.Env, a.BucketClass)
		ok, size, err := s.S3.Head(ctx, b, a.Key)
		if err != nil || !ok || size != a.SizeBytes {
			s.Log.Warn("manifest object mismatch", "key", a.Key, "ok", ok, "size", size, "want", a.SizeBytes, "err", err)
			return
		}
	}
	o.ManifestPresent = true
	o.ManifestPartial = m.Partial
	if m.Result != nil {
		if ok, _, _ := s.S3.Head(ctx, ab, m.Result.Key); ok {
			o.ResultPresent = true
		}
	}
}

func lastLine(s string) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	return strings.TrimSpace(lines[len(lines)-1])
}

func (s *Server) failNow(r *Record, reason v1.FailureReason, msg string) error {
	_, err := s.Store.Update(r.AgentRun.Spec.RunID, func(rr *Record) error {
		rr.Local.AcceptFailure = string(reason)
		rr.Local.FailureMessage = msg
		return nil
	})
	return err
}

// deriveAndApply runs controller.Derive and applies the decision with one record update.
func (s *Server) deriveAndApply(ctx context.Context, r *Record) (bool, error) {
	o := s.observe(ctx, r)
	d := controller.Derive(o)
	runID := r.AgentRun.Spec.RunID
	// Agent termination message → failure message (C-CRD rule 7).
	termMsg := ""
	if d.Phase.IsTerminal() && d.Phase != v1.PhaseSucceeded {
		if b, err := s.Engine.ReadFile(ctx, cname(r, cAgent), termPath, 1024); err == nil {
			var tm struct {
				Message string `json:"message"`
			}
			if json.Unmarshal(b, &tm) == nil {
				termMsg = tm.Message
			}
		}
	}
	now := time.Now()
	prev := r.AgentRun.Status.Phase
	rec, err := s.Store.Update(runID, func(rr *Record) error {
		ar := &rr.AgentRun
		// agent exit bookkeeping
		if o.Pod != nil {
			for _, cs := range o.Pod.Status.ContainerStatuses {
				if cs.Name == cAgent && cs.State.Terminated != nil && ar.Status.AgentExitedAt == nil && ar.Status.AgentStartedAt != nil {
					ar.Status.AgentExitedAt = mt(now)
					setCond(ar, v1.CondAgentRunning, metav1.ConditionFalse, "Exited", "", now)
				}
			}
		}
		if rr.Local.PrepareExit != nil && *rr.Local.PrepareExit != 0 {
			setCond(ar, v1.CondWorkspaceReady, metav1.ConditionFalse, string(prepReason(*rr.Local.PrepareExit)), rr.Local.FailureMessage, now)
		}
		if d.Phase == prev {
			return nil
		}
		ar.Status.Phase = d.Phase
		ar.Status.ObservedGeneration = ar.Generation
		if d.Phase.IsTerminal() {
			ar.Status.FinishedAt = mt(now)
			if o.ManifestPresent {
				ar.Status.ArtifactManifestURI = ar.Spec.Artifacts.ArtifactsPrefix + "manifest.json"
				if o.ResultPresent {
					ar.Status.ResultURI = ar.Spec.Artifacts.ArtifactsPrefix + "run_result/result.json"
				}
				if o.ManifestPartial {
					setCond(ar, v1.CondArtifactsUploaded, metav1.ConditionTrue, "PartialUpload", "", now)
				} else {
					setCond(ar, v1.CondArtifactsUploaded, metav1.ConditionTrue, "Uploaded", "", now)
				}
			} else if up := findCS(o.Pod, cUploader); up != nil && up.State.Terminated != nil {
				setCond(ar, v1.CondArtifactsUploaded, metav1.ConditionFalse, "UploadFailed", "", now)
			} else {
				setCond(ar, v1.CondArtifactsUploaded, metav1.ConditionUnknown, "NotStarted", "", now)
			}
			if d.Phase == v1.PhaseSucceeded {
				setCond(ar, v1.CondSucceeded, metav1.ConditionTrue, "Completed", "", now)
			} else {
				msg := d.Message
				if termMsg != "" {
					msg = termMsg
				} else if rr.Local.FailureMessage != "" && msg == "" {
					msg = rr.Local.FailureMessage
				}
				reason := d.Reason
				if reason == "" {
					reason = v1.ReasonControllerError
				}
				setCond(ar, v1.CondSucceeded, metav1.ConditionFalse, string(reason), truncate(msg, 1024), now)
			}
		}
		return nil
	})
	if err != nil {
		return false, err
	}
	if d.Phase != prev {
		reason := ""
		if c := meta.FindStatusCondition(rec.AgentRun.Status.Conditions, v1.CondSucceeded); c != nil && d.Phase.IsTerminal() {
			reason = c.Reason
		}
		s.Log.Info("phase", "run_id", runID, "from", prev, "to", d.Phase, "reason", reason)
		s.queueNotification(rec, string(d.Phase), reason)
	}
	if d.Phase.IsTerminal() && (d.DeletePodGracefully || o.NodeLost) {
		go s.stopContainers(context.Background(), rec, []string{cAgent, cUploader, cPrepare, cAgentd, cWatchdog, cPause}, 0)
	} else if d.DeletePodGracefully {
		if rec.Local.Intent == "" && !rec.AgentRun.Spec.CancelRequested {
			// startup timeout or image pull: no finalize
			go s.stopContainers(context.Background(), rec, []string{cAgent, cUploader, cPrepare, cAgentd, cWatchdog, cPause}, 0)
		} else {
			_ = s.terminateIfNeeded(ctx, rec)
		}
	}
	s.deliverNotifications(ctx, runID)
	return false, nil
}

func findCS(p *corev1.Pod, name string) *corev1.ContainerStatus {
	if p == nil {
		return nil
	}
	for i := range p.Status.ContainerStatuses {
		if p.Status.ContainerStatuses[i].Name == name {
			return &p.Status.ContainerStatuses[i]
		}
	}
	return nil
}

func prepReason(code int) v1.FailureReason {
	switch code {
	case 40:
		return v1.ReasonInputsFetchFailed
	case 41:
		return v1.ReasonDiffApplyFailed
	case 42:
		return v1.ReasonSessionRestoreFailed
	case 43:
		return v1.ReasonRepoFetchFailed
	}
	return v1.ReasonControllerError
}

// reconcileTerminal: notify, revoke, delete credentials, agentrun_record, container GC (controller.md §7.1).
func (s *Server) reconcileTerminal(ctx context.Context, r *Record) (bool, error) {
	ar := &r.AgentRun
	runID := ar.Spec.RunID
	s.deliverNotifications(ctx, runID)
	if !r.Local.TerminalDone {
		reason := string(ar.Status.Phase)
		if c := meta.FindStatusCondition(ar.Status.Conditions, v1.CondSucceeded); c != nil && ar.Status.Phase != v1.PhaseSucceeded {
			reason = c.Reason
		}
		s.Feed.Add(ctx, runID, ar.Spec.JobID, reason)
		removeTokenFiles(s.Cfg.StateDir, runID)
		s.Creds.Forget(runID)
		if err := s.writeAgentRunRecord(ctx, r); err != nil {
			s.Log.Warn("agentrun_record write failed", "run_id", runID, "err", err)
		}
		now := time.Now()
		var err error
		r, err = s.Store.Update(runID, func(rr *Record) error {
			rr.Local.TerminalDone, rr.Local.Revoked, rr.Local.RecordWritten = true, true, true
			setCond(&rr.AgentRun, v1.CondCredentialsIssued, metav1.ConditionFalse, "Revoked", "", now)
			return nil
		})
		if err != nil {
			return false, err
		}
		// Containers still running when the run is terminal are stopped now (R1-040).
		go s.stopContainers(context.Background(), r, []string{cAgent, cUploader, cPrepare, cGuard, cAgentd, cWatchdog, cPause}, 2)
		// Per-run log flush (M3, B-LOG-01 local stand-in): every line of every sandbox container, pushed once the pod
		// group has stopped, so short-lived containers are never missed by the collector's tailer.
		go s.flushLogs(context.Background(), r)
	}
	fin := ar.Status.FinishedAt
	if fin != nil && time.Since(fin.Time) > s.Cfg.ContainerTTL && !r.Local.ContainersGCed {
		s.gcContainers(ctx, r)
		_, _ = s.Store.Update(runID, func(rr *Record) error { rr.Local.ContainersGCed = true; return nil })
		return true, nil
	}
	// keep polling while notifications are pending or GC is due
	if len(pendingNotes(r)) == 0 {
		if fin != nil {
			wait := time.Until(fin.Time.Add(s.Cfg.ContainerTTL))
			if wait > 30*time.Second {
				return true, nil // the minute GC tick resumes this run
			}
		}
	}
	return false, nil
}

func (s *Server) writeAgentRunRecord(ctx context.Context, r *Record) error {
	ar := copyAR(&r.AgentRun)
	ar.Spec.RunGrant = ""
	for i := range ar.Status.Conditions {
		ar.Status.Conditions[i].Message = truncate(ar.Status.Conditions[i].Message, 256)
	}
	body, _ := json.MarshalIndent(ar, "", " ")
	b, k, _ := s3x.ParseURI(ar.Spec.Artifacts.ArtifactsPrefix)
	return s.S3.PutWriteOnceIdempotent(ctx, b, k+"agentrun_record/agentrun.json", body, s3x.PutOptions{
		ContentType: "application/json", Tagging: s3x.Tagging("r2y", "agentrun_record", ar.Spec.TenantID)})
}

func (s *Server) gcContainers(ctx context.Context, r *Record) {
	for _, c := range []string{cAgent, cUploader, cPrepare, cGuard, cAgentd, cWatchdog, cVolumeInit, cPause} {
		_ = s.Engine.Remove(ctx, cname(r, c))
	}
	for _, v := range volumeNames {
		_ = s.Engine.VolumeRemove(ctx, r.AgentRun.Name+"-"+v)
	}
	for v := range tmpfsVolumes {
		_ = s.Engine.VolumeRemove(ctx, r.AgentRun.Name+"-"+v)
	}
	_ = os.RemoveAll(filepath.Join(s.Cfg.StateDir, "runs", r.AgentRun.Spec.RunID))
	s.Log.Info("sandbox containers removed", "run_id", r.AgentRun.Spec.RunID)
}

// gcRecords deletes records past retention (agentrun_retention).
func (s *Server) gcRecords() {
	recs, _ := s.Store.List()
	for _, r := range recs {
		if f := r.AgentRun.Status.FinishedAt; f != nil && time.Since(f.Time) > s.Cfg.RecordRetention && r.Local.RecordWritten {
			_ = s.Store.Delete(r.AgentRun.Spec.RunID)
		}
	}
}
