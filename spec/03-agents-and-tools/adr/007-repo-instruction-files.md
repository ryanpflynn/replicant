# ADR-03-007: Target-repo agent instruction files are not auto-loaded by default

- **Status:** Accepted (WS-03, 2026-10-24)
- **Constraints:** D-10 (no agent config committed to target repos; all instructions supplied by Replicant), D-21

## Context

Repositories may already contain `CLAUDE.md`, `AGENTS.md`, `.claude/settings.json` (with hooks), `.claude/rules/`,
`.mcp.json`, or `.codex/config.toml`. In `-p` mode, project `.mcp.json` servers load without a prompt [verified:
https://code.claude.com/docs/en/mcp.md, 2026-10-24]; Claude Code reads a repo's `AGENTS.md` when no `CLAUDE.md` exists
(2.1.277+) [verified: https://code.claude.com/docs/en/memory.md, 2026-10-24]; Codex reads project `AGENTS.md` unless
`project_doc_max_bytes = 0` or the project is explicitly untrusted [verified: openai/codex `core/src/agents_md.rs` @
rust-v0.164.0, 2026-10-24]. Repo content is untrusted input (D-21); a repo hook is code execution with the CLI's
privileges, and instruction files are presented to the model with instruction authority.

## Options

1. **Load them** (what a developer would get locally).
2. **Ignore them entirely.**
3. **Do not auto-load; quote as untrusted context (chosen default):** the context assembler (WS-04) may include their
   text in the briefing inside `<untrusted_input>` blocks, capped, per codebase binding `repoInstructions`.

## Decision

Option 3 by default (`repoInstructions: quote_as_context`), `ignore` available, `trusted_load` only for bindings with
`trustRepoInstructions: true` and agents opting in. Enforcement for Claude Code: managed
`pluginConfigs["cc-plugin-agents-md@builtin"].options.instructionFiles = "managed-only"`, `claudeMdExcludes`,
`--setting-sources user`, managed `/etc/claude-code/managed-mcp.json` (exclusive MCP control) with
`allowManagedMcpServersOnly`, `disabledMcpjsonServers: ["*"]`, `allowManagedPermissionRulesOnly`, `allowManagedHooksOnly`,
plus the InstructionsLoaded audit hook.
For Codex: `project_doc_max_bytes = 0`, project `trust_level = "untrusted"` (skips project `.codex/` config, hooks, and
rules), `--ignore-rules`. CI contract tests plant each file and assert it is not loaded.

## Consequences

- Repo conventions still reach the agent (quoted), without granting them instruction authority or code execution.
- Teams that rely on their `CLAUDE.md` see it as context rather than as system-level memory; Replicant's own
  constraints win conflicts.

## Revisit when

- Repos adopt a signed, reviewed instruction-file mechanism that Replicant can verify.
