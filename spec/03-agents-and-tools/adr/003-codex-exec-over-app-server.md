# ADR-03-003: Codex via `codex exec --json`; app-server not used in promotable bundles

- **Status:** Accepted (WS-03, 2026-10-24). Resolves OQ-00-06.
- **Constraints:** D-10, ADR-010 (steering via inbox)

## Context

`codex exec` reads its prompt once and has no mid-run input; `--json` emits JSONL events [verified:
https://learn.chatgpt.com/docs/non-interactive-mode, 2026-10-24]. Codex app-server supports `turn/steer` to append input
to an active turn, but its documentation states "The app-server command and WebSocket transport are experimental and
aren't supported for production workloads" and recommends the Codex SDK for CI automation [verified:
https://learn.chatgpt.com/docs/app-server, 2026-10-24]. Codex is used for reviewer runs (median well under 15 minutes).

## Options

1. **exec only (chosen):** `steering.live: false` for Codex agents; steering queues for the next node.
2. **app-server for steerable roles:** live steering, but an unsupported interface on the hot path.
3. **Codex SDK:** needs a Node/Python runtime in sandboxes and wraps the same app-server protocol.

## Decision

Option 1. The schema forces `steering.live: false` for `codex.mode: exec`; the bundle lint rejects `mode: app_server`
outside tenant `replicant-eval`, where it can be evaluated.

## Consequences

- Steering a running Codex reviewer returns `steering_not_supported`; the message is applied to the next node.
- If an implementer role ever runs on Codex, it loses live steering until this ADR is revisited.

## Revisit when

- OpenAI marks app-server (stdio transport) supported for production, or `codex exec` gains a stdin message protocol.
