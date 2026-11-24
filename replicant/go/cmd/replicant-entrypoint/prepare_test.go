package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"example.com/replicant/internal/runner/sandbox"
)

// prepare's post-checkout disk check exits like the agent does when the watchdog fires (30, WorkspaceDiskExceeded).
func TestCheckWorkspaceDisk(t *testing.T) {
	ws := t.TempDir()
	if err := os.WriteFile(filepath.Join(ws, "blob"), make([]byte, 256<<10), 0o644); err != nil {
		t.Fatal(err)
	}
	used := sandbox.DiskUsage(ws)
	for _, c := range []struct {
		name  string
		limit int64
		code  int
	}{
		{"no limit", 0, 0}, {"under", used * 2, 0}, {"warn only", used * 100 / 90, 0}, {"over", used, exitDisk}, {"far over", used / 2, exitDisk},
	} {
		err := checkWorkspaceDisk(ws, c.limit)
		var pe *prepErr
		switch {
		case c.code == 0 && err != nil:
			t.Errorf("%s: unexpected %v", c.name, err)
		case c.code != 0 && (!errors.As(err, &pe) || pe.code != c.code):
			t.Errorf("%s: got %v, want exit %d", c.name, err, c.code)
		}
	}
}

// repoWithHistory makes an origin with two commits and returns (dir, first, second).
func repoWithHistory(t *testing.T) (string, string, string) {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not on PATH")
	}
	origin := t.TempDir()
	gitT(t, origin, "init", "-q")
	writeFiles(t, origin, map[string]string{"main.go": "package main\n"})
	gitT(t, origin, "add", "-A")
	gitT(t, origin, "commit", "-qm", "one")
	first := gitT(t, origin, "rev-parse", "HEAD")
	writeFiles(t, origin, map[string]string{"main.go": "package main // two\n", "lib/a.go": "package lib\n"})
	gitT(t, origin, "add", "-A")
	gitT(t, origin, "commit", "-qm", "two")
	return origin, first, gitT(t, origin, "rev-parse", "HEAD")
}

// prepare checks out base_sha from the repo bundle staged in the run inputs (SIMPLIFY-D40): HEAD at base, the job
// branch created there, full history (diffs and `git log` work), no remote, a clean tree.
func TestCheckoutFromBundle(t *testing.T) {
	origin, first, second := repoWithHistory(t)
	gitT(t, origin, "update-ref", bundleRefBase, second)
	bundle := filepath.Join(t.TempDir(), "repo.bundle")
	gitT(t, origin, "bundle", "create", "-q", bundle, bundleRefBase)
	repo := filepath.Join(t.TempDir(), "repo")
	if err := checkoutAt(context.Background(), repo, second, "replicant/job_x", bundle); err != nil {
		t.Fatal(err)
	}
	if got := gitT(t, repo, "rev-parse", "HEAD"); got != second {
		t.Fatalf("HEAD %s, want %s", got, second)
	}
	if got := gitT(t, repo, "rev-parse", "--abbrev-ref", "HEAD"); got != "replicant/job_x" {
		t.Fatalf("branch %s", got)
	}
	if got := gitT(t, repo, "rev-list", "--count", "HEAD"); got != "2" {
		t.Fatalf("history: %s commits", got)
	}
	if got := gitT(t, repo, "diff", "--name-only", first, second); got != "lib/a.go\nmain.go" {
		t.Fatalf("diff against history: %q", got)
	}
	if got := gitT(t, repo, "remote"); got != "" {
		t.Fatalf("remotes: %q", got)
	}
	if got := gitT(t, repo, "status", "--porcelain"); got != "" {
		t.Fatalf("dirty: %q", got)
	}
}

func TestCheckoutFromBundleFailures(t *testing.T) {
	origin, first, second := repoWithHistory(t)
	gitT(t, origin, "update-ref", bundleRefBase, second)
	bundle := filepath.Join(t.TempDir(), "repo.bundle")
	gitT(t, origin, "bundle", "create", "-q", bundle, bundleRefBase)
	garbage := filepath.Join(t.TempDir(), "garbage.bundle")
	_ = os.WriteFile(garbage, []byte("not a bundle"), 0o600)
	for name, c := range map[string]struct{ base, bundle string }{
		"bundle names another base": {first, bundle},
		"no bundle":                 {second, ""},
		"no base":                   {"", bundle},
		"not a bundle":              {second, garbage},
	} {
		err := checkoutAt(context.Background(), filepath.Join(t.TempDir(), "repo"), c.base, "b", c.bundle)
		var pe *prepErr
		if !errors.As(err, &pe) || pe.code != exitRepoFetch {
			t.Errorf("%s: got %v, want exit %d", name, err, exitRepoFetch)
		}
	}
}

func TestInputsDownload(t *testing.T) {
	body := []byte("bundle bytes")
	sum := sha256.Sum256(body)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/inputs/artifacts/k/inputs/repo.bundle" {
			http.NotFound(w, r)
			return
		}
		w.Write(body)
	}))
	defer srv.Close()
	old := inputsBase
	inputsBase = srv.URL
	defer func() { inputsBase = old }()
	dst := filepath.Join(t.TempDir(), "sub", "repo.bundle")
	ctx := context.Background()
	if err := inputsDownload(ctx, "/inputs/artifacts/k/inputs/repo.bundle", dst, hex.EncodeToString(sum[:]), int64(len(body))); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(dst); string(b) != string(body) {
		t.Fatalf("%q", b)
	}
	if err := inputsDownload(ctx, "/inputs/artifacts/k/inputs/repo.bundle", dst, hex.EncodeToString(sum[:]), int64(len(body))-1); err == nil {
		t.Fatal("size mismatch accepted")
	}
	if err := inputsDownload(ctx, "/inputs/artifacts/k/inputs/repo.bundle", dst, strings.Repeat("0", 64), int64(len(body))); err == nil {
		t.Fatal("digest mismatch accepted")
	}
	if err := inputsDownload(ctx, "/inputs/artifacts/other", dst, hex.EncodeToString(sum[:]), int64(len(body))); err == nil {
		t.Fatal("404 accepted")
	}
}

// The shared status directory is world-writable and sticky (each writer can only remove its own marker files).
func TestMkStatusDirSticky(t *testing.T) {
	d := filepath.Join(t.TempDir(), "ipc", "status")
	mkStatusDirAt(d)
	fi, err := os.Stat(d)
	if err != nil || fi.Mode()&os.ModeSticky == 0 || fi.Mode().Perm() != 0o777 {
		t.Fatalf("mode %v (%v), want sticky 0777", fi.Mode(), err)
	}
}
