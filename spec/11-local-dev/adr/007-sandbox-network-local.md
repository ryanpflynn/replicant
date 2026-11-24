# ADR-11-007: Local sandbox network = internal Docker network + sbx-edge

- **Status:** Accepted (WS-11, 2026-10-24); amended by SIMPLIFY-D30 (2026-11-23): sbx-dns and the egress proxy
  are gone, sbx-edge does both jobs
- **Constraints:** D-21 (default-deny egress, mirror/proxy for packages), C-ARCH §2 invariants 2 and 3

## Context

Production sandboxes reach exactly: the AI gateway, memory MCP, context MCP, repo-mirror, the Runner
sandbox listener, S3 run prefixes (VPC endpoint), and the package mirror. Locally, a Docker bridge
network gives every container on it reachability to every other container and, by default, to the
internet and to the host.

## Decision

- Sandboxes attach only to `replicant-sbx`, a Docker network with `internal: true` (no route off-host).
- The only member besides sandboxes is `sbx-edge` (nginx, fixed address `172.31.250.10`):
  - HTTP virtual hosts `gateway` (the WS-03 shim), `runner-sandbox-k8s` (the kind runner-api's sandbox
    listener), `memory-mcp`, `context-mcp`, `runner-sandbox`, `git`, `s3`, `otlp` `.sbx.local`, each exposing
    only the paths the prod NLB would;
  - a `stream` server on :443 that reads the TLS SNI (`ssl_preread`) and passes the session, unterminated, to
    that name only if it is on the allowlist (v0: `proxy.golang.org`, `sum.golang.org`); anything else is
    closed. sbx-edge reaches those hosts over `replicant-egress`.
- Name resolution has no server. runner-local gives the pause container (which owns the run's network
  namespace, `/etc/hosts` and `resolv.conf`; Docker and Podman hand them to every container that joins it and
  reject host entries on those) one host entry per `*.sbx.local` name and per allowlisted egress name, all
  pointing at sbx-edge, and sbx-edge as the only nameserver. sbx-edge serves no DNS, so every other name
  fails immediately: no lookup leaves the sandbox (DNS tunnelling, SEC-EGRESS-05).
- Sandboxes get no proxy variables: clients connect to the allowlisted host by name, which lands on sbx-edge.
- runner-local also sets `--cap-drop ALL`, `--security-opt no-new-privileges`, a read-only root filesystem,
  a non-root UID, and a PID limit.
- No control-plane service (Postgres, Temporal, control-plane-api, the broker) is on `replicant-sbx`.

## Consequences

- The security tests SEC-EGRESS-01..11 (test-strategy.md §8; 09..11 from SIMPLIFY-D30) run against
  runner-local; the kind profile (CiliumNetworkPolicy) answers the same `*.sbx.local` names from cluster
  CoreDNS `hosts` entries.
- Adding an egress destination means one line in sbx-edge's `stream` map and the same name in runner-local's
  `RUNNER_SANDBOX_EGRESS_HOSTS`. Only TLS on 443 can be allowlisted; there is no plain-HTTP egress.
- An SNI allowlist checks the name the client claims; the session still goes to the real allowlisted host
  (sbx-edge resolves it), so a lying client reaches only that host. Domain fronting through an allowlisted
  CDN front is the residual risk, as it was with the CONNECT allowlist.
- On Docker, the embedded resolver (127.0.0.11) stays in `resolv.conf` and answers container names on
  `replicant-sbx` (sbx-edge and other sandboxes, which are reachable by address anyway); any other name it
  forwards, if at all (recent engines do not forward from internal networks), to sbx-edge, which refuses. Podman's network has DNS disabled.
- The host's Docker socket is mounted into runner-local and Alloy. That socket never exists in
  production (parity.md P-08).
