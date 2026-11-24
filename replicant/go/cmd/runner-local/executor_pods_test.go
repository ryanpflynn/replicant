package main

import (
	"slices"
	"strings"
	"testing"

	v1 "example.com/replicant/internal/runner/v1alpha1"
)

func specServer() (*Server, *Record) {
	s := &Server{Cfg: &Config{SidecarsImage: "localhost/replicant/sandbox-sidecars:dev", SandboxNetwork: "sbx"}}
	s.Cfg.PauseImage = s.Cfg.SidecarsImage
	r := &Record{}
	r.AgentRun.Name = "ar-0123456789abcdefghjkmnpqrs"
	r.AgentRun.Spec.RunID = "run_x"
	r.AgentRun.Spec.Workspace.Enforcement = v1.EnforceWatchdog
	r.Local.WorkspaceBytes = 4 << 30
	r.Local.CLIImage, r.Local.AgentImage = "cli", "agent"
	return s, r
}

// Six containers per run; sandbox-init replaces volume-init + workspace-guard and the uploader carries the watchdog.
func TestPodGroupContainers(t *testing.T) {
	if want := []string{"sandbox-init", "pause", "replicant-agentd", "prepare", "agent", "uploader"}; !slices.Equal(podContainers, want) {
		t.Fatalf("pod group %v, want %v", podContainers, want)
	}
	s, r := specServer()
	for _, c := range podContainers {
		if img, _ := s.containerSpec(r, c); img == "" {
			t.Errorf("%s: no image", c)
		}
	}
}

func TestSandboxInitSpec(t *testing.T) {
	s, r := specServer()
	_, cc := s.containerSpec(r, cSandboxInit)
	if cc.User != "0:0" || cc.HostConfig.NetworkMode != "none" || !slices.Equal(cc.Cmd, []string{"/usr/local/bin/sandbox-sidecars", "sandbox-init"}) {
		t.Fatalf("sandbox-init: user %s network %s cmd %v", cc.User, cc.HostConfig.NetworkMode, cc.Cmd)
	}
	if !slices.Equal(cc.HostConfig.CapAdd, []string{"CHOWN", "FOWNER", "DAC_OVERRIDE"}) || !slices.Equal(cc.HostConfig.CapDrop, []string{"ALL"}) {
		t.Fatalf("caps %v / %v", cc.HostConfig.CapAdd, cc.HostConfig.CapDrop)
	}
	if len(cc.HostConfig.Mounts) != len(volumeNames) {
		t.Fatalf("mounts %d, want %d", len(cc.HostConfig.Mounts), len(volumeNames))
	}
	if !slices.Contains(cc.Env, "REPLICANT_WORKSPACE_ENFORCEMENT=watchdog") || !slices.Contains(cc.Env, "REPLICANT_WORKSPACE_LIMIT_BYTES=4294967296") {
		t.Fatalf("env %v", cc.Env)
	}
}

func TestPauseFromSidecarsImage(t *testing.T) {
	s, r := specServer()
	_, cc := s.containerSpec(r, cPause)
	if cc.Image != s.Cfg.SidecarsImage || !slices.Equal(cc.Cmd, []string{"/usr/local/bin/sandbox-sidecars", "pause"}) || cc.User != "65535:65535" {
		t.Fatalf("pause: image %s cmd %v user %s", cc.Image, cc.Cmd, cc.User)
	}
	s.Cfg.PauseImage = "registry.k8s.io/pause:3.10"
	if _, cc = s.containerSpec(r, cPause); cc.Cmd != nil || cc.User != "" {
		t.Fatalf("dedicated pause image keeps its entrypoint: cmd %v user %s", cc.Cmd, cc.User)
	}
}

func TestUploaderWatchdogMounts(t *testing.T) {
	s, r := specServer()
	_, cc := s.containerSpec(r, cUploader)
	var ws, ipc bool
	for _, m := range cc.HostConfig.Mounts {
		switch {
		case m.Target == "/workspace":
			ws = m.ReadOnly && strings.HasSuffix(m.Source, "-workspace")
		case m.Target == "/run/replicant/ipc":
			ipc = !m.ReadOnly
		}
	}
	if !ws || !ipc || cc.User != "10003:10003" {
		t.Fatalf("uploader needs workspace read-only and ipc writable as 10003: ws %v ipc %v user %s", ws, ipc, cc.User)
	}
}

// SIMPLIFY-D30: the pause container (the group's netns, hosts and resolv.conf owner) maps the sbx-edge names and
// the egress allowlist to sbx-edge and has sbx-edge as its only nameserver; joined containers set neither.
func TestPauseSandboxHosts(t *testing.T) {
	s, r := specServer()
	if _, cc := s.containerSpec(r, cPause); cc.HostConfig.ExtraHosts != nil || cc.HostConfig.DNS != nil {
		t.Fatalf("no edge IP: hosts %v dns %v", cc.HostConfig.ExtraHosts, cc.HostConfig.DNS)
	}
	s.Cfg.SandboxEdgeIP = "172.31.250.10"
	s.Cfg.SandboxEdgeHosts = []string{"gateway.sbx.local", "s3.sbx.local"}
	s.Cfg.SandboxEgressHosts = []string{"proxy.golang.org"}
	_, cc := s.containerSpec(r, cPause)
	want := []string{"gateway.sbx.local:172.31.250.10", "s3.sbx.local:172.31.250.10", "proxy.golang.org:172.31.250.10"}
	if !slices.Equal(cc.HostConfig.ExtraHosts, want) || !slices.Equal(cc.HostConfig.DNS, []string{"172.31.250.10"}) {
		t.Fatalf("pause: hosts %v dns %v", cc.HostConfig.ExtraHosts, cc.HostConfig.DNS)
	}
	if _, cc = s.containerSpec(r, cAgent); cc.HostConfig.ExtraHosts != nil || cc.HostConfig.DNS != nil {
		t.Fatalf("agent joins the pause netns and must not set hosts/dns: %v %v", cc.HostConfig.ExtraHosts, cc.HostConfig.DNS)
	}
}
