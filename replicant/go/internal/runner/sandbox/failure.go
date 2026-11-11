package sandbox

import (
	"regexp"
	"strings"
)

// Failure causes shared by replicant-qa stages and repo hooks (onboarding-interfaces.md §3 probe_failure, §6).
const (
	CauseMissingTool = "missing_tool"
	CauseNetwork     = "network"
	CauseTimeout     = "timeout"
)

var (
	// "bash: line 1: pnpm: command not found", "/bin/sh: 1: vitest: not found", "sh: pytest: not found"
	notFoundRe = regexp.MustCompile(`(?m)(?:^|:\s)([A-Za-z0-9_.+/-]+): (?:command )?not found\s*$`)
	// uv: "Failed to spawn: `pytest`"; pnpm: `Command "vitest" not found`; env: "env: 'node': No such file or directory"
	spawnRe = []*regexp.Regexp{
		regexp.MustCompile("Failed to spawn: `([^`]+)`"),
		regexp.MustCompile(`Command "([^"]+)" not found`),
		regexp.MustCompile(`env: '?([^':\s]+)'?: No such file or directory`),
	}
	networkRe = regexp.MustCompile(`(?i)(could not resolve host:?\s*'?([A-Za-z0-9.-]+)|temporary failure in name resolution|name or service not known|` +
		`getaddrinfo (?:ENOTFOUND|EAI_AGAIN)\s*([A-Za-z0-9.-]+)?|no such host|connection refused|ECONNREFUSED|network is unreachable|` +
		`ENETUNREACH|failed to establish a new connection|could not connect to server|failed to connect to)`)
)

// ClassifyFailure returns an environment cause for a failed command (missing_tool, network or timeout) and a short
// detail (the missing binary or the unreachable host), or "" when the failure looks like the command's own (a test or
// build failure). Exit code 127 is the shell's "command not found".
func ClassifyFailure(exitCode int, timedOut bool, output []byte) (cause, detail string) {
	if timedOut {
		return CauseTimeout, ""
	}
	out := string(output)
	if len(out) > 256<<10 {
		out = out[len(out)-256<<10:]
	}
	if m := notFoundRe.FindAllStringSubmatch(out, -1); len(m) > 0 && exitCode != 0 {
		return CauseMissingTool, baseName(m[len(m)-1][1])
	}
	for _, re := range spawnRe {
		if m := re.FindStringSubmatch(out); m != nil {
			return CauseMissingTool, baseName(m[1])
		}
	}
	if exitCode == 127 {
		return CauseMissingTool, ""
	}
	if m := networkRe.FindStringSubmatch(out); m != nil {
		host := m[2]
		if host == "" {
			host = m[3]
		}
		return CauseNetwork, strings.TrimSuffix(host, ".")
	}
	return "", ""
}

func baseName(s string) string {
	if i := strings.LastIndexByte(s, '/'); i >= 0 {
		s = s[i+1:]
	}
	if len(s) > 128 {
		s = s[:128]
	}
	return s
}
