# ADR-010: Steering messages delivered through a Runner-managed per-run inbox

- **Status:** Accepted (WS-00, 2026-10-24)
- **Constraints:** shared context §4 ("steer: send additional instructions to a running job"),
  D-04, D-21

## Context

Users steer running jobs. Messages either affect the next node (queued) or reach a running agent
live. The CLIs differ:

- Claude Code `-p` accepts `--input-format stream-json` and keeps queued user messages across turns
  [verified: https://code.claude.com/docs/en/cli-reference, 2026-10-24]. The Agent SDK streaming
  input mode supports queued messages and interrupts [verified:
  https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode, 2026-10-24].
- `codex exec` reads input only at start. Codex app-server supports `turn/steer`, which appends user
  input to the active turn [verified: https://learn.chatgpt.com/docs/app-server, 2026-10-24].

The sandbox must not accept inbound connections or hold Kubernetes API access.

## Options

1. **No live steering.** Next-node only. Simplest, and fails the UX requirement for long
   implementer runs.
2. **ConfigMap updates projected into the pod.** Slow kubelet sync, puts message text in etcd,
   unclear under Kata.
3. **Inbound endpoint in the sandbox.** Violates default-deny ingress and widens attack surface.
4. **Runner-managed inbox (chosen).** The Runner writes write-once inbox objects under the run's S3
   prefix. The entrypoint long-polls the Runner sandbox listener (`GET /sandbox/v1/messages`) with
   its run token and acknowledges each message.

## Decision

Option 4:
- Agent definitions declare `steering.live`. The Runner API returns 409 `steering_not_supported`
  otherwise, and the workflow falls back to queueing.
- The workflow is the authority over steering. Messages are Temporal Updates (`job.steer`). The
  inbox is a delivery copy.
- The entrypoint injects messages as user turns (Claude stream-json, Agent SDK, or Codex
  app-server). Message text is treated as user input, never as system instructions.

## Consequences

- Live delivery latency is at most about 20 s (long-poll).
- Choosing Codex `exec` versus app-server affects whether reviewer runs on Codex can be steered
  live. WS-03 decides.
- Queued messages are always included in the next node's inputs, so nothing is lost if live
  delivery fails.

## Revisit when

- The CLIs offer a supported local control socket that makes the inbox unnecessary. The
  inbox-to-entrypoint leg would still exist.
