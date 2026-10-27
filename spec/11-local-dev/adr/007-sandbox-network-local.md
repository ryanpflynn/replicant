# ADR-11-007: Local sandbox network = internal Docker network + sbx-edge + sbx-dns + egress proxy

- **Status:** Accepted (WS-11, 2026-10-24)
- **Constraints:** D-21 (default-deny egress, mirror/proxy for packages), C-ARCH §2 invariants 2 and 3

## Context

Production sandboxes reach exactly: the AI gateway, memory MCP, context MCP, repo-mirror, the Runner
sandbox listener, S3 run prefixes (VPC endpoint), and the package mirror. Locally, a Docker bridge
network gives every container on it reachability to every other container and, by default, to the
internet and to the host.

## Decision

- Sandboxes attach only to `replicant-sbx`, a Docker network with `internal: true` (no route off-host).
- The only members besides sandboxes: `sbx-edge` (nginx; virtual hosts `gateway` (the WS-03 shim), `runner-sandbox-k8s` (the kind runner-api's sandbox listener), `memory-mcp`,
  `context-mcp`, `runner-sandbox`, `git`, `s3`, `otlp` `.sbx.local`, each exposing only the paths the prod
  NLB would), `sbx-dns` (CoreDNS answering only `*.sbx.local`, NXDOMAIN otherwise, no forwarder),
  `egress-proxy` (Squid; sandboxes may only CONNECT to `.sbx.local`), and the three package mirrors
  (Verdaccio for npm, Athens for Go modules, proxpi for PyPI), which themselves reach upstreams on the
  `replicant-egress` network.
- runner-local sets `--dns 172.31.250.2`, the proxy variables, `--cap-drop ALL`,
  `--security-opt no-new-privileges`, a read-only root filesystem, a non-root UID, and a PID limit.
- No control-plane service (Postgres, Temporal, control-plane-api, the broker) is on `replicant-sbx`.

## Consequences

- The security tests SEC-EGRESS-01..08 (test-strategy.md §8) run identically against runner-local and
  the kind profile (where CiliumNetworkPolicy enforces the same allowlist).
- Docker's embedded DNS (127.0.0.11) remains inside each container's resolv.conf search path only if
  `--dns` is not set; runner-local always sets it. Whether Docker's embedded resolver forwards external
  queries on an `internal` network is `[unverified]`; sbx-dns makes the answer irrelevant.
- The host's Docker socket is mounted into runner-local and Alloy. That socket never exists in
  production (parity.md P-08).
