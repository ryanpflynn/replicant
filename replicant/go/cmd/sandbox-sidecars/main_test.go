package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"example.com/replicant/internal/runner/sandbox"
)

func volumes(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	for _, v := range []string{"workspace", "home", "tools", "managed", "inputs", "out", "spool"} {
		if err := os.Mkdir(filepath.Join(root, v), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func fakeChown(t *testing.T, fail bool) *[]string {
	t.Helper()
	var calls []string
	chown = func(p string, uid, gid int) error {
		calls = append(calls, p)
		if fail {
			return errors.New("EPERM")
		}
		return nil
	}
	t.Cleanup(func() { chown = os.Chown })
	return &calls
}

// sandbox-init sets ownership first and only then runs the guard: an ownership failure exits 1 before the guard's
// 84 could fire; with ownership set, a hard limit that does not match the volume exits 84.
func TestSandboxInitOrder(t *testing.T) {
	root := volumes(t)
	calls := fakeChown(t, true)
	if rc := sandboxInit(root, "loopback", 1); rc != 1 {
		t.Fatalf("ownership failure: rc %d, want 1", rc)
	}
	if len(*calls) == 0 {
		t.Fatal("chown never called")
	}
	calls = fakeChown(t, false)
	if rc := sandboxInit(root, "loopback", 1); rc != 84 {
		t.Fatalf("hard limit mismatch: rc %d, want 84", rc)
	}
	if !contains(*calls, filepath.Join(root, "workspace")) || !contains(*calls, filepath.Join(root, "managed", "replicant")) {
		t.Fatalf("chown calls %v", *calls)
	}
	// setgid really set (os.Chmod ignores a raw 0o2000 bit): files created in the volumes get group 10002
	for v, want := range map[string]os.FileMode{"workspace": 0o775, "home": 0o770, "tools": 0o755, "out": 0o775} {
		fi, _ := os.Stat(filepath.Join(root, v))
		if fi.Mode().Perm() != want || fi.Mode()&os.ModeSetgid == 0 {
			t.Fatalf("%s mode %v, want setgid %v", v, fi.Mode(), want)
		}
	}
}

// Under watchdog enforcement (local default) the guard checks writability only.
func TestSandboxInitWatchdogEnforcement(t *testing.T) {
	root := volumes(t)
	fakeChown(t, false)
	if rc := sandboxInit(root, "watchdog", 1); rc != 0 {
		t.Fatalf("rc %d, want 0", rc)
	}
	if _, err := os.Stat(filepath.Join(root, "workspace", ".guard-probe")); err == nil {
		t.Fatal("guard probe left behind")
	}
	if rc := guard(filepath.Join(root, "missing"), "watchdog", 1); rc != 84 {
		t.Fatalf("unwritable workspace: rc %d, want 84", rc)
	}
}

func TestWatchdogMarkers(t *testing.T) {
	ws, status := t.TempDir(), filepath.Join(t.TempDir(), "status")
	if err := os.MkdirAll(status, 0o777); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(ws, "f"), make([]byte, 64<<10), 0o644); err != nil {
		t.Fatal(err)
	}
	used := sandbox.DiskUsage(ws)
	read := func(p string) string {
		b, _ := os.ReadFile(filepath.Join(status, filepath.Base(p)))
		return string(b)
	}
	exists := func(p string) bool { _, err := os.Stat(filepath.Join(status, filepath.Base(p))); return err == nil }

	peak := watchdogSample(ws, status, used*10, 0)
	if read(sandbox.DiskState) != "ok" || exists(sandbox.MarkerDiskWarn) || exists(sandbox.MarkerDiskExceeded) || peak != used {
		t.Fatalf("ok: state %q peak %d", read(sandbox.DiskState), peak)
	}
	watchdogSample(ws, status, used*100/90, peak)
	if read(sandbox.DiskState) != "warn" || !exists(sandbox.MarkerDiskWarn) || exists(sandbox.MarkerDiskExceeded) {
		t.Fatalf("warn: state %q", read(sandbox.DiskState))
	}
	watchdogSample(ws, status, used, peak)
	if read(sandbox.DiskState) != "exceeded" || !exists(sandbox.MarkerDiskExceeded) {
		t.Fatalf("exceeded: state %q", read(sandbox.DiskState))
	}
	if read(sandbox.DiskUsed) == "" || read(sandbox.DiskLimit) == "" || read(sandbox.DiskPeak) == "" {
		t.Fatal("disk-used/-limit/-peak not written")
	}
}

// The uploader's watchdog goroutine creates the shared status directory and stops with its context.
func TestWatchdogLoop(t *testing.T) {
	ws, status := t.TempDir(), filepath.Join(t.TempDir(), "status")
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { watchdog(ctx, ws, status, 1<<30, 10*time.Millisecond); close(done) }()
	deadline := time.Now().Add(5 * time.Second)
	for {
		if b, err := os.ReadFile(filepath.Join(status, "disk-state")); err == nil && strings.TrimSpace(string(b)) == "ok" {
			if fi, _ := os.Stat(status); fi.Mode()&os.ModeSticky == 0 || fi.Mode().Perm() != 0o777 {
				t.Fatalf("status dir mode %v, want sticky 0777", fi.Mode())
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("no disk-state written")
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("watchdog did not stop")
	}
}

func contains(ss []string, s string) bool {
	for _, x := range ss {
		if x == s {
			return true
		}
	}
	return false
}
