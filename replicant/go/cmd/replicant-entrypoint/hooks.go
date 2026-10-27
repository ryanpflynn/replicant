package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
	"time"

	"example.com/replicant/internal/runner/ipc"
	"example.com/replicant/internal/runner/sandbox"
)

// hookMain implements the managed Claude Code hooks (§2.3): instructions-loaded, guard-paths, session-end.
func hookMain(name string) int {
	in, _ := io.ReadAll(io.LimitReader(os.Stdin, 1<<20))
	switch name {
	case "guard-paths":
		return guardPaths(in)
	case "instructions-loaded":
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		var ev map[string]any
		_ = json.Unmarshal(in, &ev)
		_ = ipc.New(sandbox.IPCSocket).Do(ctx, http.MethodPost, "/v1/report/instructions", nil, ev, nil)
		return 0
	case "session-end":
		return 0
	}
	return 0
}

var configNames = []string{"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENTS.override.md", ".mcp.json"}
var configDirs = []string{".claude", ".codex", ".cursor"}

// guardPaths blocks (exit 2) edits outside /workspace/ or into D-10 agent-config paths or forbiddenPaths.
func guardPaths(in []byte) int {
	var ev struct {
		ToolInput struct {
			FilePath     string `json:"file_path"`
			NotebookPath string `json:"notebook_path"`
		} `json:"tool_input"`
	}
	_ = json.Unmarshal(in, &ev)
	p := ev.ToolInput.FilePath
	if p == "" {
		p = ev.ToolInput.NotebookPath
	}
	if p == "" {
		return 0
	}
	if !filepath.IsAbs(p) {
		p = filepath.Join(sandbox.RepoDir, p)
	}
	resolved := resolve(filepath.Clean(p))
	if !strings.HasPrefix(resolved, sandbox.WorkspaceDir+"/") {
		fmt.Fprintf(os.Stderr, "replicant: edits are allowed only under /workspace (%s)\n", resolved)
		return 2
	}
	rel := strings.TrimPrefix(resolved, sandbox.RepoDir+"/")
	base := filepath.Base(resolved)
	for _, n := range configNames {
		if base == n {
			fmt.Fprintf(os.Stderr, "replicant: agent configuration files must not be written (%s)\n", rel)
			return 2
		}
	}
	for _, seg := range strings.Split(rel, "/") {
		for _, d := range configDirs {
			if seg == d {
				fmt.Fprintf(os.Stderr, "replicant: agent configuration directories must not be written (%s)\n", rel)
				return 2
			}
		}
	}
	if plan, _, err := readPlan(); err == nil {
		for _, g := range plan.Agent.OutputContract.DiffPolicy.ForbiddenPaths {
			if globMatch(g, rel) {
				fmt.Fprintf(os.Stderr, "replicant: path %s is forbidden by the agent definition (%s)\n", rel, g)
				return 2
			}
		}
	}
	return 0
}

// resolve follows symlinks of the longest existing prefix.
func resolve(p string) string {
	cur, rest := p, ""
	for cur != "/" {
		if r, err := filepath.EvalSymlinks(cur); err == nil {
			return filepath.Join(r, rest)
		}
		rest = filepath.Join(filepath.Base(cur), rest)
		cur = filepath.Dir(cur)
	}
	return p
}

// globMatch supports ** across segments.
func globMatch(pattern, name string) bool {
	if !strings.Contains(pattern, "**") {
		ok, _ := path.Match(pattern, name)
		return ok
	}
	parts := strings.SplitN(pattern, "**", 2)
	prefix, suffix := parts[0], strings.TrimPrefix(parts[1], "/")
	if !strings.HasPrefix(name, prefix) {
		return false
	}
	rest := strings.TrimPrefix(name, prefix)
	if suffix == "" {
		return true
	}
	segs := strings.Split(rest, "/")
	for i := range segs {
		if globMatch(suffix, strings.Join(segs[i:], "/")) {
			return true
		}
	}
	return false
}

// materializeMain is the CI dry-run (§4.1): print the managed settings, argv and env names for a plan.
func materializeMain(args []string) int {
	var planPath, out string
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--plan":
			i++
			planPath = args[i]
		case "--out":
			i++
			out = args[i]
		}
	}
	b, err := os.ReadFile(planPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	var p sandbox.RunPlan
	if err := json.Unmarshal(b, &p); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	schemaJSON, _, err := agentReportSchema(&p)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	bs := &sandbox.Bootstrap{}
	res := map[string]any{
		"argv":             cliArgv(&p, p.RunID, schemaJSON, "/run/replicant/prompt/system-append.md"),
		"managed_settings": managedSettings(&p, mcpServers(&p, bs)),
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	_ = enc.Encode(res)
	if out != "" {
		_ = renderManaged(out, &p, bs)
	}
	return 0
}
