package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"example.com/replicant/internal/runner/sandbox"
)

// mcpShelf is where environment images install the MCP server packages the platform approved (design §8c,
// ONBOARD-D14): sandboxes have no package registry, so `npx -y pkg@1.2.0` or `uvx pkg` cannot fetch at run time.
// Layout: <shelf>/node_modules (npm install --prefix <shelf> of the packages), <shelf>/bin/<name> (Python tools).
var mcpShelf = "/opt/mcp"

// autoMCP is one auto-allowed repo server: as the repo's .mcp.json declares it, and as this sandbox starts it.
type autoMCP struct {
	orig, run sandbox.MCPAutoAllow
}

// resolveMCP maps an auto-allowed stdio server onto the shelf. Package runners (npx, uvx, ...) run the pre-installed
// package's own binary with the arguments after the package; any other command must be on PATH. ok=false: the
// server cannot start here (not_installed).
func resolveMCP(a sandbox.MCPAutoAllow) (sandbox.MCPAutoAllow, bool) {
	runner := filepath.Base(a.Command)
	switch runner {
	case "npx", "bunx", "pnpx", "uvx", "pipx":
		pkg, rest := "", []string{}
		for i, x := range a.Args {
			if x == "run" && runner == "pipx" && pkg == "" {
				continue
			}
			if !strings.HasPrefix(x, "-") {
				pkg, rest = x, a.Args[i+1:]
				break
			}
		}
		if pkg == "" {
			return a, false
		}
		name, ver := splitPackage(pkg)
		var bin string
		if runner == "uvx" || runner == "pipx" {
			bin = filepath.Join(mcpShelf, "bin", name)
		} else {
			bin = npmShelfBin(name, ver)
		}
		if bin == "" {
			return a, false
		}
		if st, err := os.Stat(bin); err != nil || st.IsDir() {
			return a, false
		}
		return sandbox.MCPAutoAllow{Name: a.Name, Command: bin, Args: append([]string{}, rest...)}, true
	default:
		if _, err := exec.LookPath(a.Command); err != nil {
			return a, false
		}
		return a, true
	}
}

// splitPackage: "@scope/name@1.2.0" -> ("@scope/name", "1.2.0"); "name==1.0" (pip style) -> ("name", "1.0").
func splitPackage(s string) (string, string) {
	if n, v, ok := strings.Cut(s, "=="); ok {
		return n, v
	}
	if i := strings.LastIndex(s, "@"); i > 0 {
		return s[:i], s[i+1:]
	}
	return s, ""
}

// npmShelfBin: the binary of an npm package installed on the shelf, when its version is the pinned one.
func npmShelfBin(name, ver string) string {
	b, err := os.ReadFile(filepath.Join(mcpShelf, "node_modules", name, "package.json"))
	if err != nil {
		return ""
	}
	var pj struct {
		Version string          `json:"version"`
		Bin     json.RawMessage `json:"bin"`
	}
	if json.Unmarshal(b, &pj) != nil || (ver != "" && ver != "latest" && pj.Version != ver) {
		return ""
	}
	short := name[strings.LastIndex(name, "/")+1:]
	binName := ""
	var one string
	var many map[string]string
	switch {
	case json.Unmarshal(pj.Bin, &one) == nil && one != "":
		binName = short
	case json.Unmarshal(pj.Bin, &many) == nil && len(many) > 0:
		if _, ok := many[short]; ok {
			binName = short
		} else if len(many) == 1 {
			for k := range many {
				binName = k
			}
		}
	}
	if binName == "" {
		return ""
	}
	return filepath.Join(mcpShelf, "node_modules", ".bin", binName)
}
