# ADR-03-001: Entrypoint and agentd split; credentials never enter the agent container

- **Status:** Accepted (WS-03, 2026-10-24). Requires WS-02 pod template support (XD-03-01).
- **Constraints:** D-10, D-21, C-CRD §6, C-RUNNER sandbox listener

## Context

The sandbox needs a run token (gateway, memory-mcp, context-mcp, repo-mirror, runner-sandbox) and run-scoped STS
credentials (inputs, uploads). The agent runs arbitrary shell commands and is prompt-injectable (D-21). D-21 says every
credential in a sandbox is "assumed exfiltratable", which bounds the damage but does not argue for exposing it: a run
token copied out of one tenant's sandbox could be replayed from any other sandbox (all sandbox pools reach the same
NLBs) against that tenant's memory MCP until it expires. C-ARCH recommends a static Go entrypoint.

## Options

1. **One entrypoint process holding the token, CLI as its child, same uid.** Simple; the agent can read
   `$REPLICANT_RUN_TOKEN_FILE` or `/proc/<ppid>/environ`.
2. **One container, entrypoint as a different uid, CLI via setuid.** Needs `CAP_SETUID`/`CAP_SETGID` and a root start,
   conflicting with `runAsNonRoot` and `drop: [ALL]`.
3. **Two containers (chosen):** `replicant-agentd` native sidecar (uid 10000) is the only mount point of the token Secret
   and the only holder of STS credentials it fetches; `replicant-entrypoint` (uid 10001) runs the CLI with no credential,
   reaching services through agentd's loopback proxy. No shared PID namespace.

## Decision

Option 3. Both binaries are static Go, shipped in the pinned CLI image (`/opt/replicant/bin/`), so their behavior is
pinned by `cliImage` digest together with the CLI they drive.

Amended 2026-10-24 for the accepted CCR-09-01/02/03: agentd holds the **per-audience token set**
(seeded from the projected Secret at `/run/replicant/token-seed`, `REPLICANT_RUN_TOKEN_SEED_DIR`; live copies on agentd's tmpfs at `/run/replicant/tokens/<audience>`, `REPLICANT_RUN_TOKEN_DIR`, C-IDS §8), injects exactly one token per loopback route
(cli-invocation.md §5.1 1a), refreshes the set with one call, and checks that all tokens carry the same run grant and
`acting_for`. WS-09's control review (G-01) adopts this custody model for every container in the pod, including the
WS-05 QA harness.

## Consequences

- The agent can still *use* the services through loopback (as the CLI must), but cannot carry the credential out.
- `prepare` (repo fetch, inputs) also goes through agentd's short-lived routes, closed once prepare reports done.
- WS-02 must render agentd as a native sidecar with the Secret mounted only there, and an `ipc` emptyDir shared with
  the agent container.
- One more container per pod (about 20 MiB RSS, negligible CPU).

## Revisit when

- Kubernetes or the runtime offers per-container credential injection that makes a sidecar unnecessary, or the
  services move to workload identity that cannot be replayed outside the pod (for example SPIFFE with per-pod
  attestation).
