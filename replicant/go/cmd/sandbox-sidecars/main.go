// Command sandbox-sidecars is the WS-02 local sidecar image (local-runner.md §1.1 `sandbox-sidecars`):
// `uploader` (also ships spool chunks and runs the workspace disk watchdog; no separate shipper or watchdog
// container), the local-only `sandbox-init` (fsGroup emulation, then the workspace-guard check) and `pause`
// (the pod-group network namespace holder).
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"example.com/replicant/internal/runner/httpx"
	"example.com/replicant/internal/runner/sandbox"
)

func main() {
	cmd := filepath.Base(os.Args[0])
	args := os.Args[1:]
	if cmd == "sandbox-sidecars" || cmd == "main" {
		if len(args) == 0 {
			fmt.Fprintln(os.Stderr, "usage: sandbox-sidecars uploader|sandbox-init|pause|version")
			os.Exit(2)
		}
		cmd, args = args[0], args[1:]
	}
	switch cmd {
	case "uploader":
		os.Exit(uploaderMain())
	case "sandbox-init":
		limit, _ := strconv.ParseInt(os.Getenv("REPLICANT_WORKSPACE_LIMIT_BYTES"), 10, 64)
		os.Exit(sandboxInit("/v", httpx.Env("REPLICANT_WORKSPACE_ENFORCEMENT", "watchdog"), limit))
	case "pause":
		pause()
	case "version":
		fmt.Println("sandbox-sidecars 0.1.0")
	default:
		_ = args
		fmt.Fprintln(os.Stderr, "unknown command", cmd)
		os.Exit(2)
	}
}

func pause() {
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, syscall.SIGTERM, syscall.SIGINT)
	<-ch
}

var chown = os.Chown // replaced in tests

// sandboxInit is the one-shot sandbox-init container (no network; CHOWN, FOWNER, DAC_OVERRIDE): it gives the run's
// volumes (mounted at root/<name>) their ownership, then runs the workspace-guard check. Exit 1: ownership could
// not be set; exit 84: the workspace hard limit is not enforced as declared.
func sandboxInit(root, enforcement string, limit int64) int {
	if rc := volumeInit(root); rc != 0 {
		return rc
	}
	return guard(filepath.Join(root, "workspace"), enforcement, limit)
}

// volumeInit gives the run's volumes the ownership kubelet's fsGroup 10002 would (local only).
func volumeInit(root string) int {
	type spec struct {
		uid, gid int
		mode     uint32 // Unix permission bits, setgid included (sandbox.FileMode)
	}
	m := map[string]spec{
		"workspace": {10001, 10002, 0o2775}, "home": {10001, 10002, 0o2770}, "tools": {10001, 10002, 0o2755},
		"managed": {10001, 10002, 0o2755}, "inputs": {10001, 10002, 0o2755}, "out": {10001, 10002, 0o2775},
		"spool": {10001, 10002, 0o2775},
	}
	rc := 0
	for name, s := range m {
		p := filepath.Join(root, name)
		if _, err := os.Stat(p); err != nil {
			continue
		}
		if err := chown(p, s.uid, s.gid); err != nil {
			fmt.Fprintln(os.Stderr, "chown", p, err)
			rc = 1
		}
		if err := os.Chmod(p, sandbox.FileMode(s.mode)); err != nil {
			fmt.Fprintln(os.Stderr, "chmod", p, err)
			rc = 1
		}
	}
	managed := filepath.Join(root, "managed")
	if _, err := os.Stat(managed); err == nil {
		for _, d := range []string{"claude-code", "replicant"} {
			_ = os.MkdirAll(filepath.Join(managed, d), 0o755)
			_ = chown(filepath.Join(managed, d), 10001, 10002)
		}
	}
	return rc
}

// guard is the workspace-guard check: fail closed (exit 84) when the hard limit is not enforced as declared.
// Under watchdog enforcement only writability is checked (the uploader's watchdog stops the agent at 98 %).
func guard(ws, enforcement string, limit int64) int {
	probe := filepath.Join(ws, ".guard-probe")
	if err := os.WriteFile(probe, []byte("ok"), 0o600); err != nil {
		fmt.Fprintln(os.Stderr, "workspace not writable:", err)
		return 84
	}
	_ = os.Remove(probe)
	if enforcement == "watchdog" {
		// macOS / runc: the watchdog stops the agent at 98 % (local-runner.md §1.3); nothing to check here.
		fmt.Printf(`{"msg":"workspace-guard ok","enforcement":"watchdog","limit_bytes":%d}`+"\n", limit)
		return 0
	}
	var st unix.Statfs_t
	if err := unix.Statfs(ws, &st); err != nil {
		return 84
	}
	total := int64(st.Blocks) * int64(st.Bsize)
	if limit > 0 && (total < limit*99/100 || total > limit*101/100) {
		fmt.Fprintf(os.Stderr, "workspace size %d does not match hard limit %d\n", total, limit)
		return 84
	}
	return 0
}

// watchdog samples workspace usage every interval and writes the status files (controller.md §4.2) until ctx ends.
// It runs inside the uploader (same uid 10003; the workspace is mounted read-only there).
func watchdog(ctx context.Context, ws, statusDir string, limit int64, interval time.Duration) {
	_ = os.MkdirAll(statusDir, 0o777)
	_ = os.Chmod(statusDir, sandbox.FileMode(0o1777)) // uploader, entrypoint and agentd all write status files
	var peak int64
	for {
		peak = watchdogSample(ws, statusDir, limit, peak)
		select {
		case <-ctx.Done():
			return
		case <-time.After(interval):
		}
	}
}

// watchdogSample takes one sample, writes disk-state/-used/-limit/-peak and the warn/exceeded markers, and returns
// the new peak.
func watchdogSample(ws, statusDir string, limit, peak int64) int64 {
	used := sandbox.DiskUsage(ws)
	peak = max(peak, used)
	state := sandbox.DiskLevel(used, limit)
	switch state {
	case "exceeded":
		touchFile(filepath.Join(statusDir, filepath.Base(sandbox.MarkerDiskExceeded)))
	case "warn":
		touchFile(filepath.Join(statusDir, filepath.Base(sandbox.MarkerDiskWarn)))
	}
	for p, v := range map[string]string{sandbox.DiskState: state, sandbox.DiskUsed: strconv.FormatInt(used, 10),
		sandbox.DiskLimit: strconv.FormatInt(limit, 10), sandbox.DiskPeak: strconv.FormatInt(peak, 10)} {
		writeAtomic(filepath.Join(statusDir, filepath.Base(p)), v)
	}
	return peak
}

func touchFile(p string) {
	if _, err := os.Stat(p); err != nil {
		_ = os.WriteFile(p, []byte(time.Now().UTC().Format(time.RFC3339)), 0o644)
	}
}

func writeAtomic(p, v string) {
	tmp := p + ".tmp"
	if os.WriteFile(tmp, []byte(strings.TrimSpace(v)), 0o644) == nil {
		_ = os.Rename(tmp, p)
	}
}
