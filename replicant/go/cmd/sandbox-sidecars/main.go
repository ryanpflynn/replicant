// Command sandbox-sidecars is the WS-02 local sidecar image (local-runner.md §1.1 `sandbox-sidecars`):
// `uploader` (also ships spool chunks; no separate shipper), `watchdog`, `workspace-guard`, plus the
// local-only `volume-init` (fsGroup emulation) and `pause` (the pod-group network namespace holder).
package main

import (
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
			fmt.Fprintln(os.Stderr, "usage: sandbox-sidecars uploader|watchdog|workspace-guard|volume-init|pause|version")
			os.Exit(2)
		}
		cmd, args = args[0], args[1:]
	}
	switch cmd {
	case "uploader":
		os.Exit(uploaderMain())
	case "watchdog":
		os.Exit(watchdogMain())
	case "workspace-guard":
		os.Exit(guardMain())
	case "volume-init":
		os.Exit(volumeInit())
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

// volumeInit gives the run's volumes the ownership kubelet's fsGroup 10002 would (local only).
func volumeInit() int {
	type spec struct {
		uid, gid int
		mode     os.FileMode
	}
	m := map[string]spec{
		"workspace": {10001, 10002, 0o2775}, "home": {10001, 10002, 0o2770}, "tools": {10001, 10002, 0o2755},
		"managed": {10001, 10002, 0o2755}, "inputs": {10001, 10002, 0o2755}, "out": {10001, 10002, 0o2775},
		"spool": {10001, 10002, 0o2775},
	}
	rc := 0
	for name, s := range m {
		p := "/v/" + name
		if _, err := os.Stat(p); err != nil {
			continue
		}
		if err := os.Chown(p, s.uid, s.gid); err != nil {
			fmt.Fprintln(os.Stderr, "chown", p, err)
			rc = 1
		}
		if err := os.Chmod(p, s.mode); err != nil {
			fmt.Fprintln(os.Stderr, "chmod", p, err)
			rc = 1
		}
	}
	for _, d := range []string{"/v/managed/claude-code", "/v/managed/replicant"} {
		if _, err := os.Stat("/v/managed"); err != nil {
			break
		}
		_ = os.MkdirAll(d, 0o755)
		_ = os.Chown(d, 10001, 10002)
	}
	return rc
}

// guardMain is workspace-guard: fail closed (exit 84) when the hard limit is not enforced as declared.
func guardMain() int {
	enf := httpx.Env("REPLICANT_WORKSPACE_ENFORCEMENT", "watchdog")
	limit, _ := strconv.ParseInt(os.Getenv("REPLICANT_WORKSPACE_LIMIT_BYTES"), 10, 64)
	p := sandbox.WorkspaceDir
	probe := filepath.Join(p, ".guard-probe")
	if err := os.WriteFile(probe, []byte("ok"), 0o600); err != nil {
		fmt.Fprintln(os.Stderr, "workspace not writable:", err)
		return 84
	}
	_ = os.Remove(probe)
	if enf == "watchdog" {
		// macOS / runc: the watchdog stops the agent at 98 % (local-runner.md §1.3); nothing to check here.
		fmt.Printf(`{"msg":"workspace-guard ok","enforcement":"watchdog","limit_bytes":%d}`+"\n", limit)
		return 0
	}
	var st unix.Statfs_t
	if err := unix.Statfs(p, &st); err != nil {
		return 84
	}
	total := int64(st.Blocks) * int64(st.Bsize)
	if limit > 0 && (total < limit*99/100 || total > limit*101/100) {
		fmt.Fprintf(os.Stderr, "workspace size %d does not match hard limit %d\n", total, limit)
		return 84
	}
	return 0
}

// watchdogMain samples workspace usage every few seconds and writes the status files (controller.md §4.2).
func watchdogMain() int {
	limit, _ := strconv.ParseInt(os.Getenv("REPLICANT_WORKSPACE_LIMIT_BYTES"), 10, 64)
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, syscall.SIGINT)
	_ = os.MkdirAll(sandbox.StatusDir, 0o777)
	_ = os.Chmod(sandbox.StatusDir, 0o1777) // watchdog, entrypoint and agentd all write status files
	var peak int64
	interval := time.Duration(httpx.EnvInt("REPLICANT_WATCHDOG_INTERVAL_SECONDS", 3)) * time.Second
	for {
		used := du(sandbox.WorkspaceDir)
		if used > peak {
			peak = used
		}
		state := "ok"
		if limit > 0 {
			switch {
			case used*100 >= limit*98:
				state = "exceeded"
				touchFile(sandbox.MarkerDiskExceeded)
			case used*100 >= limit*85:
				state = "warn"
				touchFile(sandbox.MarkerDiskWarn)
			}
		}
		writeAtomic(sandbox.DiskState, state)
		writeAtomic(sandbox.DiskUsed, strconv.FormatInt(used, 10))
		writeAtomic(sandbox.DiskLimit, strconv.FormatInt(limit, 10))
		writeAtomic(sandbox.DiskPeak, strconv.FormatInt(peak, 10))
		select {
		case <-stop:
			return 0
		case <-time.After(interval):
		}
	}
}

func du(root string) int64 {
	var total int64
	seen := map[uint64]bool{}
	_ = filepath.Walk(root, func(p string, fi os.FileInfo, err error) error {
		if err != nil {
			return nil
		}
		if st, ok := fi.Sys().(*syscall.Stat_t); ok {
			if st.Nlink > 1 {
				if seen[st.Ino] {
					return nil
				}
				seen[st.Ino] = true
			}
			total += st.Blocks * 512
		} else {
			total += fi.Size()
		}
		return nil
	})
	return total
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
