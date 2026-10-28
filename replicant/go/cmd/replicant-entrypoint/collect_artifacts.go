package main

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/klauspost/compress/zstd"

	"example.com/replicant/internal/runner/sandbox"
)

// D-10 agent-config paths, always excluded from the diff (§4.8).
var agentConfigPaths = []string{"CLAUDE.md", "**/CLAUDE.md", "CLAUDE.local.md", "**/CLAUDE.local.md", "AGENTS.md", "**/AGENTS.md",
	"AGENTS.override.md", "**/AGENTS.override.md", ".claude/**", "**/.claude/**", ".codex/**", "**/.codex/**", ".mcp.json", "**/.mcp.json",
	".cursor/**", "**/.cursor/**"}

// collectOutputs is §4.8 for the roles v0 runs.
func (rc *runCtx) collectOutputs(ctx context.Context, col *collected, report map[string]any, code int, security bool) {
	p := rc.plan
	role := p.Role
	_ = os.MkdirAll(sandbox.OutDir, 0o775)
	if role == "implementer" || role == "repair" || role == "takeover" {
		if err := rc.collectDiff(ctx, col); err != nil {
			log.Error("diff collection failed", "err", err)
		}
	}
	if mode := p.Agent.OutputContract.WorkspaceIntegrity; (mode == "detect" || mode == "enforce") && rc.integrityPre != "" {
		after, _ := trackedTree(ctx)
		mod := after != rc.integrityPre
		integ := map[string]any{"mode": mode, "tracked_files_modified": mod, "tree_before": rc.integrityPre, "tree_after": after}
		if mod {
			out, _ := git(ctx, sandbox.RepoDir, gitEnv(), "diff", "--name-only", rc.integrityPre, after)
			integ["modified_paths"] = firstN(strings.Fields(out), 200)
		}
		col.integ = integ
	}
	if role == "reviewer" && report != nil {
		rv := map[string]any{"schema": "replicant.review-report/v1", "agent_report": report, "cli": rc.cliExecution(code)}
		if col.integ != nil {
			rv["integrity"] = col.integ
		}
		path := filepath.Join(sandbox.OutDir, "review_report", "review.json")
		if writeOut(path, rv) == nil {
			col.entries = append(col.entries, sandbox.OutputEntry{Type: "review_report", BucketClass: "artifacts", Path: path, Name: "review.json",
				ContentType: "application/json", Required: true, Redaction: "required", Producer: "entrypoint"})
			col.outputs["review_report"] = rc.artifactsKey("review_report", "review.json")
		}
	}
	if role == "planner" && report != nil {
		path := filepath.Join(sandbox.OutDir, "plan", "plan.json")
		if writeOut(path, report) == nil {
			col.entries = append(col.entries, sandbox.OutputEntry{Type: "plan", BucketClass: "artifacts", Path: path, Name: "plan.json",
				ContentType: "application/json", Required: true, Redaction: "required", Producer: "entrypoint"})
			col.outputs["plan"] = rc.artifactsKey("plan", "plan.json")
		}
		// plan.md: the agent's /replicant/out/plan.md, else rendered from the structured plan
		md, err := os.ReadFile(filepath.Join(sandbox.OutDir, "plan.md"))
		if err != nil || len(bytes.TrimSpace(md)) == 0 {
			md = renderPlanMD(report)
		}
		mdPath := filepath.Join(sandbox.OutDir, "plan", "plan.md")
		if os.WriteFile(mdPath, md, 0o644) == nil {
			col.entries = append(col.entries, sandbox.OutputEntry{Type: "plan", BucketClass: "artifacts", Path: mdPath, Name: "plan.md",
				ContentType: "text/markdown", Required: false, Redaction: "required", Producer: "entrypoint"})
		}
	}
	if role != "qa" && role != "eval" {
		rc.writeHandoff(col, report, code)
	}
	if b, err := os.ReadFile(filepath.Join(sandbox.OutDir, "memory_candidates.json")); err == nil && json.Valid(b) && !security {
		path := filepath.Join(sandbox.OutDir, "memory_candidates", "candidates.json")
		_ = os.MkdirAll(filepath.Dir(path), 0o755)
		if os.WriteFile(path, b, 0o644) == nil {
			col.entries = append(col.entries, sandbox.OutputEntry{Type: "memory_candidates", BucketClass: "artifacts", Path: path, Name: "candidates.json",
				ContentType: "application/json", Required: false, Redaction: "required", Producer: "entrypoint"})
			col.outputs["memory_candidates"] = rc.artifactsKey("memory_candidates", "candidates.json")
		}
	}
}

func firstN(s []string, n int) []string {
	if len(s) > n {
		return s[:n]
	}
	if s == nil {
		return []string{}
	}
	return s
}

func writeOut(path string, v any) error {
	_ = os.MkdirAll(filepath.Dir(path), 0o755)
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, b, 0o644)
}

// trackedTree computes `git write-tree` over a temporary index of the tracked files (§6 step 3).
func trackedTree(ctx context.Context) (string, error) {
	idx := filepath.Join(os.TempDir(), fmt.Sprintf("integ-%d.index", time.Now().UnixNano()))
	defer os.Remove(idx)
	env := gitEnv("GIT_INDEX_FILE=" + idx)
	if _, err := git(ctx, sandbox.RepoDir, env, "read-tree", "HEAD"); err != nil {
		return "", err
	}
	if _, err := git(ctx, sandbox.RepoDir, env, "add", "-u", "--", "."); err != nil {
		return "", err
	}
	out, err := git(ctx, sandbox.RepoDir, env, "write-tree")
	return strings.TrimSpace(out), err
}

// collectDiff builds diff_bundle/changes.tar.zst against base_sha with a temporary index (§4.8).
func (rc *runCtx) collectDiff(ctx context.Context, col *collected) error {
	base := rc.plan.Workspace.BaseSHA
	if base == "" {
		base = rc.bs.Repo.BaseSHA
	}
	work := filepath.Join(sandbox.OutDir, ".cli")
	_ = os.MkdirAll(work, 0o755)
	idx := filepath.Join(work, "index")
	_ = os.Remove(idx)
	env := gitEnv("GIT_INDEX_FILE=" + idx)
	repo := sandbox.RepoDir
	if _, err := git(ctx, repo, env, "read-tree", base); err != nil {
		return err
	}
	// neutralize repo-declared filters
	var filterArgs []string
	if out, err := git(ctx, repo, gitEnv(), "config", "--file", ".git/config", "--get-regexp", `^filter\.`); err == nil {
		seen := map[string]bool{}
		for _, l := range strings.Split(out, "\n") {
			k := strings.Fields(l)
			if len(k) == 0 {
				continue
			}
			parts := strings.Split(k[0], ".")
			if len(parts) >= 3 && !seen[parts[1]] {
				seen[parts[1]] = true
				filterArgs = append(filterArgs, "-c", "filter."+parts[1]+".clean=", "-c", "filter."+parts[1]+".smudge=", "-c", "filter."+parts[1]+".process=")
			}
		}
	}
	// all changes (for the stripped list), then the excluded set
	addAll := append(append([]string{}, filterArgs...), "add", "-A", "--", ".")
	if _, err := git(ctx, repo, env, addAll...); err != nil {
		return err
	}
	allNames, _ := git(ctx, repo, env, "diff", "--cached", "--name-only", base)
	if _, err := git(ctx, repo, env, "read-tree", base); err != nil {
		return err
	}
	forbidden := append(append([]string{}, agentConfigPaths...), rc.plan.Agent.OutputContract.DiffPolicy.ForbiddenPaths...)
	args := append(append([]string{}, filterArgs...), "add", "-A", "--", ".")
	for _, f := range forbidden {
		args = append(args, ":(exclude,glob)"+f)
	}
	if _, err := git(ctx, repo, env, args...); err != nil {
		return err
	}
	keptNames, _ := git(ctx, repo, env, "diff", "--cached", "--name-only", base)
	kept := map[string]bool{}
	for _, n := range strings.Fields(keptNames) {
		kept[n] = true
	}
	stripped := []string{}
	for _, n := range strings.Fields(allNames) {
		if !kept[n] {
			stripped = append(stripped, n)
		}
	}
	patch, err := git(ctx, repo, env, "-c", "diff.external=", "-c", "core.pager=cat", "diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", base)
	if err != nil {
		return err
	}
	numstat, _ := git(ctx, repo, env, "diff", "--cached", "--numstat", base)
	ns, _ := git(ctx, repo, env, "diff", "--cached", "--name-status", "-M", base)
	ins, del, files, bin := 0, 0, 0, 0
	numByPath := map[string][2]int{}
	var paths []string
	for _, l := range strings.Split(strings.TrimSpace(numstat), "\n") {
		f := strings.Split(l, "\t")
		if len(f) < 3 {
			continue
		}
		files++
		if f[0] == "-" {
			bin++
			paths = append(paths, f[2])
			continue
		}
		a, _ := strconv.Atoi(f[0])
		d, _ := strconv.Atoi(f[1])
		ins, del = ins+a, del+d
		numByPath[f[2]] = [2]int{a, d}
		paths = append(paths, f[2])
	}
	for _, l := range strings.Split(strings.TrimSpace(ns), "\n") {
		f := strings.Split(l, "\t")
		if len(f) < 2 {
			continue
		}
		ft := map[string]any{"path": f[len(f)-1], "change": "modified"}
		switch f[0][0] {
		case 'A':
			ft["change"] = "added"
		case 'D':
			ft["change"] = "deleted"
		case 'R':
			ft["change"] = "renamed"
			ft["renamed_from"] = f[1]
		}
		if n, ok := numByPath[f[len(f)-1]]; ok {
			ft["insertions"], ft["deletions"] = n[0], n[1]
		}
		col.files = append(col.files, ft)
	}
	head, _ := git(ctx, repo, env, "write-tree")
	col.headTree = strings.TrimSpace(head)
	commits := 0
	var mbox string
	if out, err := git(ctx, repo, gitEnv(), "rev-list", "--count", base+"..HEAD"); err == nil {
		commits, _ = strconv.Atoi(strings.TrimSpace(out))
		if commits > 0 {
			mbox, _ = git(ctx, repo, gitEnv(), "format-patch", base+"..HEAD", "--stdout")
		}
	}
	meta := map[string]any{"base_sha": base, "files_changed": files, "insertions": ins, "deletions": del, "paths": firstN(paths, 500),
		"forbidden_paths_stripped": stripped, "created_by_run": runIDOf(rc)}
	var buf bytes.Buffer
	zw, _ := zstd.NewWriter(&buf)
	tw := tar.NewWriter(zw)
	add := func(name string, b []byte) {
		_ = tw.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(b)), ModTime: time.Unix(0, 0), Format: tar.FormatPAX})
		_, _ = tw.Write(b)
	}
	metaB, _ := json.MarshalIndent(meta, "", "  ")
	add("changes.patch", []byte(patch))
	add("metadata.json", metaB)
	if mbox != "" {
		add("commits.mbox", []byte(mbox))
	}
	_ = tw.Close()
	_ = zw.Close()
	path := filepath.Join(sandbox.OutDir, "diff_bundle", "changes.tar.zst")
	_ = os.MkdirAll(filepath.Dir(path), 0o755)
	if err := os.WriteFile(path, buf.Bytes(), 0o644); err != nil {
		return err
	}
	dsha := sha(buf.Bytes())
	col.empty = files == 0
	col.diffKey = rc.artifactsKey("diff_bundle", "changes.tar.zst")
	col.changes = map[string]any{"files_changed": files, "insertions": ins, "deletions": del, "binary_files": bin,
		"diff_sha256": dsha, "base_sha": base, "commits_by_agent": commits, "forbidden_paths_stripped": stripped, "paths": firstN(paths, 500)}
	col.entries = append(col.entries, sandbox.OutputEntry{Type: "diff_bundle", BucketClass: "artifacts", Path: path, Name: "changes.tar.zst",
		ContentType: "application/vnd.replicant.diff-bundle.v1+tar+zstd", SHA256: dsha, Required: true, Redaction: "scan-block", Producer: "entrypoint",
		Attributes: map[string]any{"files_changed": files, "insertions": ins, "deletions": del}})
	col.outputs["diff_bundle"] = col.diffKey
	return nil
}
