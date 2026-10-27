# ADR-04-007: Injection slots per CLI; disable CLI-native memory and repo instruction files

- **Status:** Accepted (WS-04, 2026-10-24); materialization owned by WS-03
- **Constraints:** D-10 (no agent files in repos; all instructions supplied externally), D-14, D-21

## Context

Claude Code loads user and project CLAUDE.md files and has an auto memory that writes under
`~/.claude/projects/<project>/memory/`; `claude -p` also honors project settings, hooks, and MCP
configuration unless told otherwise. Codex loads global and project `AGENTS.md`. Facts [verified:
https://code.claude.com/docs/en/memory, …/env-vars, …/cli-reference; Codex `core/src/agents_md.rs`;
2026-10-24]: `--setting-sources user`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`,
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `--append-system-prompt-file`, managed `managed-mcp.json`; Codex
`project_doc_max_bytes = 0` disables project docs.

## Options

1. Write memory into a user-scope CLAUDE.md / AGENTS.md and leave project files on.
2. **System-prompt append (Claude) / `$CODEX_HOME/AGENTS.md` (Codex) for Replicant text; disable
   project instruction files, project settings and hooks, and CLI-native memory (chosen).**

## Decision

Option 2 (retrieval-injection.md §5.2). Human-written repo docs (CONTRIBUTING.md, an existing
CLAUDE.md) can enter memory only through admin-approved `import` candidates.

## Consequences

- Replicant's memory is the only memory: scoped, versioned, auditable.
- Repos with useful hand-written agent docs lose them until imported.
- Security benefit: repo-supplied hooks and MCP servers never run (WS-03/WS-09 alignment).

## Revisit when

- A CLI stops honoring these switches, or `--bare` becomes the `-p` default with different semantics.

## Amendment (wave 2, 2026-10-24)

Materialization follows WS-03's XD-FIX-R1-068 and R1-092: MCP servers come only from
`/etc/claude-code/managed-mcp.json` pointing at agentd on loopback (`--strict-mcp-config` and
`--mcp-config` are never passed, because with managed-mcp.json either makes Claude Code exit), no
token enters the agent container, paths are under `/home/agent` and `/workspace/repo`, and the Agent
SDK is not used. The memory block stays in `memory-injection.json` and is appended by the entrypoint
(XD-W2-04-01).
