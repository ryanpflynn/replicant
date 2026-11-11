package sandbox

import "testing"

func TestClassifyFailure(t *testing.T) {
	for _, c := range []struct {
		name      string
		code      int
		timedOut  bool
		out       string
		cause, dt string
	}{
		{"bash not found", 127, false, "bash: line 1: pnpm: command not found\n", CauseMissingTool, "pnpm"},
		{"dash not found", 127, false, "/bin/sh: 1: vitest: not found\n", CauseMissingTool, "vitest"},
		{"npm script", 1, false, "> test\n> vitest run\n\nsh: 1: vitest: not found\n", CauseMissingTool, "vitest"},
		{"uv spawn", 2, false, "error: Failed to spawn: `pytest`\n  Caused by: No such file or directory (os error 2)\n", CauseMissingTool, "pytest"},
		{"pnpm exec", 1, false, `ERR_PNPM_RECURSIVE_EXEC_FIRST_FAIL  Command "tsc" not found`, CauseMissingTool, "tsc"},
		{"bare 127", 127, false, "", CauseMissingTool, ""},
		{"dns", 1, false, "curl: (6) Could not resolve host: registry.npmjs.org\n", CauseNetwork, "registry.npmjs.org"},
		{"npm dns", 1, false, "npm error getaddrinfo ENOTFOUND registry.npmjs.org\n", CauseNetwork, "registry.npmjs.org"},
		{"refused", 1, false, "dial tcp 127.0.0.1:5432: connect: connection refused\n", CauseNetwork, ""},
		{"timeout", -1, true, "", CauseTimeout, ""},
		{"test failure", 1, false, "FAILED tests/test_a.py::test_x - assert 1 == 2\n1 failed, 3 passed\n", "", ""},
		{"build failure", 2, false, "./a.go:3:1: syntax error: non-declaration statement outside function body\n", "", ""},
	} {
		cause, detail := ClassifyFailure(c.code, c.timedOut, []byte(c.out))
		if cause != c.cause || detail != c.dt {
			t.Errorf("%s: got (%q, %q), want (%q, %q)", c.name, cause, detail, c.cause, c.dt)
		}
	}
}
