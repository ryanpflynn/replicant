package main

import (
	"context"
	"fmt"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"example.com/replicant/internal/runner/engine"
	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/sandbox"
	v1 "example.com/replicant/internal/runner/v1alpha1"
)

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
