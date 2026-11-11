package main

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/klauspost/compress/zstd"

	"example.com/replicant/internal/runner/httpx"
	"example.com/replicant/internal/runner/ipc"
	"example.com/replicant/internal/runner/s3x"
	"example.com/replicant/internal/runner/sandbox"
	"example.com/replicant/internal/runner/schemas"
)

type prepErr struct {
	code int
	msg  string
}

func (e *prepErr) Error() string { return e.msg }

func fail(code int, format string, a ...any) error { return &prepErr{code, fmt.Sprintf(format, a...)} }

func inputsGet(ctx context.Context, path string) ([]byte, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+sandbox.LoopbackInputs+path, nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 2<<30))
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("GET %s: HTTP %d %s", path, resp.StatusCode, strings.TrimSpace(string(b[:min(len(b), 200)])))
	}
	return b, err
}

// prepareMain implements §4.3 prepare and §4.4 steps 1–5 (exit 40–43).
func prepareMain() int {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()
	err := prepare(ctx)
	if err == nil {
		log.Info("prepared")
		return exitOK
	}
	code := exitInputs
	if pe, ok := err.(*prepErr); ok {
		code = pe.code
	}
	log.Error("prepare failed", "code", code, "err", err.Error())
	writeTermination(code, "", err.Error())
	return code
}

func prepare(ctx context.Context) error {
	bs, err := sandbox.ReadBootstrap("")
	if err != nil {
		return fail(exitInputs, "bootstrap.json: %v", err)
	}
	agentd := ipc.New(sandbox.IPCSocket)
	wctx, wcancel := context.WithTimeout(ctx, 2*time.Minute)
	if err := agentd.WaitReady(wctx); err != nil {
		wcancel()
		return fail(exitInputs, "agentd not ready: %v", err)
	}
	wcancel()
	// 1. manifest
	mb, err := inputsGet(ctx, "/inputs/manifest")
	if err != nil {
		return fail(exitInputs, "inputs manifest: %v", err)
	}
	if s3x.Sha256Hex(mb) != bs.InputsManifestSHA256 {
		return fail(exitInputs, "inputs manifest digest mismatch")
	}
	if err := schemas.Validate(schemas.RunInputs, mb); err != nil {
		return fail(exitInputs, "unsupported_manifest_schema: %v", err)
	}
	var m sandbox.InputsManifest
	if err := json.Unmarshal(mb, &m); err != nil {
		return fail(exitInputs, "manifest: %v", err)
	}
	if m.RunID != bs.RunID {
		return fail(exitInputs, "manifest run_id %s != %s", m.RunID, bs.RunID)
	}
	in := sandbox.InputsDir
	_ = os.MkdirAll(in, 0o755)
	if err := os.WriteFile(filepath.Join(in, "manifest.json"), mb, 0o444); err != nil {
		return fail(exitInputs, "write manifest: %v", err)
	}
	// 2. every object
	var diffs [][]byte
	var plan *sandbox.RunPlan
	for _, o := range m.Objects {
		b, err := inputsGet(ctx, "/inputs/"+o.BucketClass+"/"+o.Key)
		if err != nil {
			return fail(exitInputs, "input %s: %v", o.Name, err)
		}
		if s3x.Sha256Hex(b) != o.SHA256 || int64(len(b)) != o.SizeBytes {
			return fail(exitInputs, "input %s digest or size mismatch", o.Name)
		}
		dst := ""
		switch o.Place {
		case "run_plan":
			if err := schemas.Validate(schemas.RunPlan, b); err != nil && (bs.Env != "local" || schemas.LocalRelaxed(err) != nil) {
				return fail(exitInputs, "run plan does not validate: %v", err)
			}
			var p sandbox.RunPlan
			if err := json.Unmarshal(b, &p); err != nil {
				return fail(exitInputs, "run plan: %v", err)
			}
			plan = &p
			dst = "run-plan.json"
		case "system_append":
			dst = "system-append.md"
		case "system_append_memory":
			dst = "memory-injection.json"
		case "first_message":
			dst = "first-message.md"
		case "context_file":
			dst = filepath.Join("context", o.Path)
		case "qa_file":
			dst = filepath.Join("qa", o.Path)
		case "apply_diff":
			diffs = append(diffs, b)
		case "restore_session":
			// SR-1 restore is not implemented in v0 (DEVIATIONS); the run starts fresh with the handoff.
			log.Warn("restore_session input ignored (v0): fresh session", "name", o.Name)
		case "none":
		}
		if dst != "" {
			p := filepath.Join(in, dst)
			if !strings.HasPrefix(filepath.Clean(p), in+"/") {
				return fail(exitInputs, "input path escapes /replicant/inputs")
			}
			_ = os.MkdirAll(filepath.Dir(p), 0o755)
			if err := os.WriteFile(p, b, 0o444); err != nil {
				return fail(exitInputs, "write %s: %v", dst, err)
			}
		}
	}
	if plan == nil {
		return fail(exitInputs, "no run_plan in manifest")
	}
	// 3. CLI check
	if plan.Role != "qa" || plan.Agent.CLI != "" {
		if plan.Agent.CLI != "claude-code" {
			return fail(exitInputs, "cli_mismatch: image ships claude-code, plan wants %s", plan.Agent.CLI)
		}
		if v := cliVersion(); plan.Agent.CLIImage.Version != v {
			return fail(exitInputs, "cli_mismatch: image ships %s, plan wants %s", v, plan.Agent.CLIImage.Version)
		}
	}
	if plan.Role == "qa" {
		if _, err := os.Stat("/opt/replicant/bin/replicant-qa"); err != nil {
			return fail(exitInputs, "cli_mismatch: image has no replicant-qa")
		}
	}
	// 4. prompt digests
	for name, p := range plan.Prompts {
		if p.SHA256 != "" && p.Text != "" && s3x.Sha256Hex([]byte(p.Text)) != p.SHA256 {
			return fail(exitInputs, "prompt %s sha256 mismatch", name)
		}
	}
	// git checkout through agentd's loopback git route
	if err := checkout(ctx, bs, plan); err != nil {
		return err
	}
	// repo_config use (§8c): the repo's agent configuration is trusted only as it is on the default branch
	if plan.RepoConfigUse() && plan.ConfigBaseSHA != "" && plan.Role != "qa" {
		if err := restoreBaseConfig(ctx, sandbox.RepoDir, plan.ConfigBaseSHA); err != nil {
			return fail(exitRepoFetch, "restore repo configuration from %s: %v", plan.ConfigBaseSHA, err)
		}
	}
	// repo instruction files at the base commit, before any prior diff is applied or the agent edits anything
	if plan.RepoInstructions == "quote_as_context" && plan.Role != "qa" {
		if q := quoteRepoInstructions(ctx, sandbox.RepoDir, quotedInstructionFiles(plan), patternRedactor); q != "" {
			if err := os.WriteFile(filepath.Join(in, repoInstructionsFile), []byte(q), 0o444); err != nil {
				return fail(exitInputs, "write %s: %v", repoInstructionsFile, err)
			}
		}
	}
	for _, d := range diffs {
		if err := applyDiff(ctx, d); err != nil {
			return fail(exitDiffApply, "%v", err)
		}
	}
	// 5. security layer into the managed volume
	managed := httpx.Env("REPLICANT_MANAGED_OUT", "/replicant/managed")
	if err := renderManaged(managed, plan, bs); err != nil {
		return fail(exitInputs, "render managed settings: %v", err)
	}
	// copy /opt/replicant into the tools volume
	if err := copyTree("/opt/replicant", httpx.Env("REPLICANT_TOOLS_OUT", "/replicant/tools")); err != nil {
		return fail(exitInputs, "copy tools: %v", err)
	}
	if err := agentd.Status(ctx, "prepared"); err != nil {
		log.Warn("prepared report failed", "err", err)
	}
	return nil
}

// gitEnv is the hardened git environment of §4.8.
func gitEnv(extra ...string) []string {
	env := []string{"HOME=" + sandbox.HomeDir, "PATH=" + os.Getenv("PATH"), "GIT_CONFIG_NOSYSTEM=1", "GIT_ATTR_NOSYSTEM=1",
		"GIT_TERMINAL_PROMPT=0", "GIT_CONFIG_GLOBAL=" + gitGlobal()}
	for _, k := range []string{"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"} {
		if v := os.Getenv(k); v != "" {
			env = append(env, k+"="+v)
		}
	}
	return append(env, extra...)
}

func gitGlobal() string {
	for _, p := range []string{"/opt/replicant/etc/gitconfig"} {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return "/dev/null"
}

func git(ctx context.Context, dir string, env []string, args ...string) (string, error) {
	full := append([]string{"-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "protocol.version=2"}, args...)
	cmd := exec.CommandContext(ctx, "git", full...)
	cmd.Dir = dir
	cmd.Env = env
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	err := cmd.Run()
	if err != nil {
		return out.String(), fmt.Errorf("git %s: %v: %s", strings.Join(args, " "), err, strings.TrimSpace(errb.String()))
	}
	return out.String(), nil
}

func checkout(ctx context.Context, bs *sandbox.Bootstrap, plan *sandbox.RunPlan) error {
	repo := sandbox.RepoDir
	base := bs.Repo.BaseSHA
	if base == "" {
		base = plan.Workspace.BaseSHA
	}
	if err := os.MkdirAll(repo, 0o755); err != nil {
		return fail(exitRepoFetch, "mkdir repo: %v", err)
	}
	env := gitEnv()
	format := "sha1"
	if len(base) == 64 {
		format = "sha256"
	}
	steps := [][]string{
		{"init", "-q", "--object-format=" + format},
		{"config", "fetch.negotiationAlgorithm", "skipping"},
		{"config", "user.name", "replicant-agent"},
		{"config", "user.email", "replicant-agent@replicant.invalid"},
	}
	if _, err := git(ctx, repo, env, "remote", "get-url", "origin"); err != nil {
		steps = append(steps, []string{"remote", "add", "origin", "http://" + sandbox.LoopbackGit + "/git/" + bs.Repo.RepoID})
	}
	for _, s := range steps {
		if _, err := git(ctx, repo, env, s...); err != nil {
			return fail(exitRepoFetch, "%v", err)
		}
	}
	fetch := []string{"fetch", "--no-tags", "--filter=blob:none"}
	if bs.Repo.FetchDepth > 0 {
		fetch = append(fetch, "--depth", strconv.Itoa(bs.Repo.FetchDepth))
	}
	fetch = append(fetch, "origin", base)
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		if _, lastErr = git(ctx, repo, env, fetch...); lastErr == nil {
			break
		}
		time.Sleep(time.Duration(2<<attempt) * time.Second)
	}
	if lastErr != nil {
		return fail(exitRepoFetch, "%v", lastErr)
	}
	if _, err := git(ctx, repo, env, "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", base); err != nil {
		return fail(exitRepoFetch, "%v", err)
	}
	head, err := git(ctx, repo, env, "rev-parse", "HEAD")
	if err != nil || strings.TrimSpace(head) != base {
		return fail(exitRepoFetch, "HEAD %s != base_sha %s", strings.TrimSpace(head), base)
	}
	if b := plan.Workspace.BranchName; b != "" {
		_, _ = git(ctx, repo, env, "checkout", "-q", "-B", b)
	}
	return nil
}

// applyDiff applies a prior diff bundle (changes.patch inside the tar.zst) to the checkout (exit 41).
func applyDiff(ctx context.Context, bundle []byte) error {
	patch, err := patchFromBundle(bundle)
	if err != nil {
		return err
	}
	if len(bytes.TrimSpace(patch)) == 0 {
		return nil
	}
	p := filepath.Join(os.TempDir(), "replicant-prior.patch")
	if err := os.WriteFile(p, patch, 0o600); err != nil {
		return err
	}
	_, err = git(ctx, sandbox.RepoDir, gitEnv(), "apply", "--index", "--binary", "--whitespace=nowarn", p)
	return err
}

func patchFromBundle(bundle []byte) ([]byte, error) {
	dec, err := zstd.NewReader(bytes.NewReader(bundle))
	if err != nil {
		return nil, err
	}
	defer dec.Close()
	tr := tar.NewReader(dec)
	for {
		h, err := tr.Next()
		if err == io.EOF {
			return nil, fmt.Errorf("changes.patch not in diff bundle")
		}
		if err != nil {
			return nil, err
		}
		if filepath.Base(h.Name) == "changes.patch" {
			return io.ReadAll(io.LimitReader(tr, 256<<20))
		}
	}
}

func copyTree(src, dst string) error {
	return filepath.Walk(src, func(p string, fi os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, p)
		t := filepath.Join(dst, rel)
		switch {
		case fi.IsDir():
			return os.MkdirAll(t, 0o755)
		case fi.Mode()&os.ModeSymlink != 0:
			l, err := os.Readlink(p)
			if err != nil {
				return err
			}
			_ = os.Remove(t)
			return os.Symlink(l, t)
		case fi.Mode().IsRegular():
			in, err := os.Open(p)
			if err != nil {
				return err
			}
			defer in.Close()
			_ = os.Remove(t)
			out, err := os.OpenFile(t, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, fi.Mode().Perm()|0o444)
			if err != nil {
				return err
			}
			if _, err := io.Copy(out, in); err != nil {
				out.Close()
				return err
			}
			return out.Close()
		}
		return nil
	})
}
