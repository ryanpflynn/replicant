# ADR-03-002: Claude Code CLI (`claude -p`) for every Claude role; Agent SDK reserved

- **Status:** Accepted (WS-03, 2026-10-24)
- **Constraints:** D-10 ("`claude -p` / Claude Agent SDK, or `codex exec`. No reimplemented coding harness")

## Context

The brief asks whether the Claude Agent SDK is a better fit than the CLI for some roles. Facts (2026-10-24): the
Python SDK 0.2.174 bundles CLI 2.1.310 and drives it over the stream-json protocol [verified:
https://pypi.org/project/claude-agent-sdk/0.2.174/, 2026-10-24]; the TS SDK 0.3.310 tracks CLI 2.1.310 and needs a
native binary [verified: https://registry.npmjs.org/@anthropic-ai/claude-agent-sdk/latest, 2026-10-24]. The SDK adds
in-process callbacks (`can_use_tool`, Python/TS hooks, in-process MCP tools via `create_sdk_mcp_server`) and its
`setting_sources` default loads user, project, and local settings [verified:
https://code.claude.com/docs/en/agent-sdk/python.md, 2026-10-24]. The CLI alone offers stream-json input and output,
`--json-schema`, `--max-budget-usd`, `--permission-mode dontAsk`, managed settings, command hooks, and session resume.

## Options

1. **CLI everywhere (chosen).**
2. **SDK for planner/reviewer** (structured output and tool vetoes in Python).
3. **SDK everywhere** (one Python runner in every sandbox).

## Decision

Option 1. Every capability Replicant needs is reachable through CLI flags, managed settings, and command hooks
implemented by the Go entrypoint. The SDK would require a Python or Node runtime in every toolchain image, add a
second configuration surface with different defaults, and change the pinning story (the SDK version and its bundled
CLI version would both need pinning). `cli: claude-agent-sdk` remains in the schema and bundle enum so a future
runner can be added as configuration plus a new CLI image, without a contract change.

## Consequences

- The entrypoint implements the stream-json protocol itself (about 600 lines of Go) and must track its changes; the
  adapter contract tests in the CLI image CI catch drift.
- Per-call dynamic permission decisions (for example "allow this Bash command only if the plan mentions it") are not
  available; static allow/deny rules plus the PreToolUse command hook cover Replicant's needs.

## Revisit when

- A role needs per-call programmatic tool vetoes that a command hook cannot express, or the evaluation harness wants
  in-process instrumentation.
