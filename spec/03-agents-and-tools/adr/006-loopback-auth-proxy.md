# ADR-03-006: CLIs reach the gateway through agentd's loopback proxy, not CLI-native credential helpers

- **Status:** Accepted (WS-03, 2026-10-24)
- **Constraints:** D-11 (gateway credentials short-lived, one run), D-21, C-IDS §7 (attribution headers)

## Context

Run tokens live 60 minutes and must be refreshed for longer runs. Claude Code supports `apiKeyHelper` with
`CLAUDE_CODE_API_KEY_HELPER_TTL_MS` and `ANTHROPIC_CUSTOM_HEADERS` [verified: https://code.claude.com/docs/en/env-vars.md,
2026-10-24]; Codex supports `model_providers.<id>.auth.command` with `refresh_interval_ms` and `env_http_headers`
[verified: https://learn.chatgpt.com/docs/config-file/config-reference, 2026-10-24]. Both helpers execute as the agent's
uid in the agent's container. Codex's default `shell_environment_policy` passes secrets to tool subprocesses [verified:
same page, 2026-10-24].

## Options

1. **CLI-native helpers** reading the token file: token readable by the agent; two implementations of headers and
   retries.
2. **Loopback proxy in agentd (chosen):** CLIs use a non-secret placeholder (`ANTHROPIC_AUTH_TOKEN`; Codex sends no
   auth) against `http://127.0.0.1:7800`; agentd injects the token and `X-Replicant-*`, creates `traceparent` when
   missing, applies one retry policy for 429/529/5xx, counts usage, and enforces local call caps.

## Decision

Option 2.

## Consequences

- Uniform behavior for both CLIs and for future CLIs.
- agentd is on the request path; it is a native sidecar with automatic restart, and CLIs retry connection errors.
- Trusted usage numbers in `run_result` come from agentd, not from CLI self-reports.

## Revisit when

- Never expected; revisit only if a CLI stops supporting a custom base URL.
