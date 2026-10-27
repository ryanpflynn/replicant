# ADR-05-002: Dependencies of the app under test — declared sidecar services and contract stubs; no Docker API in v1

- **Status:** Accepted (WS-05, 2026-10-24); revised the same day after WS-02 (Kata CLH default, no `subPath`), WS-03
  (`spec.services[]` schema), and WS-09 (admission policy forbids privileged containers; control review G-25)
- **Constraints:** D-16 (required decision; explicit answer for Docker-socket approaches), D-06,
  D-07, D-12, D-13, D-21

## Context

Apps under test need databases, queues, object stores, and other teams' services. Options listed by
the brief: sidecar containers in the AgentRun pod, services started inside the sandbox, contract
stubs generated from extracted contracts, ephemeral managed resources. Testcontainers needs a Docker
API. Verified facts that shape the decision:

- Native sidecars are stable since Kubernetes 1.33, start before the main container, and do not
  block Job completion [verified: https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/,
  2026-10-24].
- Kata runs all of a pod's containers in one VM [verified: Kata architecture README, 2026-10-24];
  privileged containers stay inside the guest when `privileged_without_host_devices = true`
  [verified: Kata privileged.md, 2026-10-24].
- Testcontainers works with Podman by setting `DOCKER_HOST` to the Podman socket and, in rootless
  mode, `TESTCONTAINERS_RYUK_DISABLED=true`; alternative runtimes "are not actively tested"
  [verified: https://java.testcontainers.org/supported_docker_environment/, 2026-10-24].
- Docker inside gVisor needs handler flags (`--net-raw`, `--allow-packet-socket-write`) and cannot
  publish ports with iptables disabled [verified: https://gvisor.dev/docs/tutorials/docker-in-gvisor/,
  2026-10-24].
- Sandboxes have no Kubernetes API access (C-CRD §6) and no credentials beyond the run (D-21).

## Options

| Option | Pros | Cons |
|---|---|---|
| A. Native sidecars declared in the environment definition | Real engines; pinned by digest; per-container resources; node image cache; no privileges; works under Kata and gVisor | Fixed at pod creation; needs controller support for rendering |
| B. Processes inside the main container | Simple; identical locally | Couples service versions to toolchain images; no per-service accounting |
| C. Contract stubs from extracted contracts | Deterministic, cheap, no credentials | Contract-level fidelity only |
| D. Docker API in the sandbox (rootless Podman in the Kata guest) | Testcontainers works unchanged | Privileged-in-guest; pulls each run; Kata only |
| E. Remote Docker / Testcontainers Cloud | Full Testcontainers | Breaks default-deny egress and tenant isolation |
| F. kubedock | Testcontainers over Kubernetes | Needs Kubernetes API from the sandbox |
| G. Ephemeral managed resources | Highest fidelity | Credentials, network paths, cost, ## Decision

1. **A** for infrastructure dependencies, using WS-03's `spec.services[]` (with `qa ∈ forRoles`);
   QA adds `qa.serviceOptions[]` (log wrapping via `replicant-svc-wrap`, readiness timeout, reset
   between passes).
2. **C** for downstream services: Prism 5.16.0 (OpenAPI), GripMock v3.23.4 (gRPC), WireMock 3.13.2
   (GraphQL/mappings), AsyncAPI examples seeded into the Kafka sidecar; contracts from WS-04's index
   at `deployed_prod` (default) or `default_branch`.
3. **D (Docker API / Testcontainers) is not offered in v1.** It needs a writable `/dev/net/tun` for
   published ports (hence `privileged` or a device plugin into the Kata guest), plus `seccomp:
   Unconfined`, SETUID/SETGID and `allowPrivilegeEscalation: true`; WS-09's admission policy forbids
   `privileged`, and WS-03 already chose "sidecars only". The full capability list is in
   [dependencies.md §5](../dependencies.md#5-testcontainers-and-docker-api-clients-d-16-explicit-answer)
   (answer to WS-09 G-25).
4. **B** only for embedded dependencies already in the toolchain image.
5. **E, F, G** rejected for v1.

An earlier draft of this ADR (same day) chose rootless Podman privileged inside the Kata guest for
Testcontainers, arguing that the VM is the boundary. WS-09's policy makes no exception for
`privileged` and WS-09 would have to widen admission for one feature; WS-05 accepts that position for
v1 and records the disagreement only as a revisit condition, not a challenge.

## Consequences

- WS-02 renders QA services, stubs and the builder as native sidecars without `subPath`, all after
  `prepare` (WS-02 controller.md §4.2a, XD-FIX-R1-045), so `replicant-svc-wrap` runs from the `tools`
  volume at `/opt/replicant/bin` and logs to `/var/log/qa-services`; wrapped services use
  `replicant/svc-<name>` images and unwrapped ones `mirror/<upstream path>` (XD-05-20 ruling). WS-02
  keeps no `startupProbe` on QA services; the harness owns readiness. Service data is declared as
  `data {env, subdir?, sizeEstimate}` (XD-FIX-R1-074).
- A Postgres + Kafka service fits the Kata profile: pod ≈ 5.4 CPU / 8.9 GiB requests, 14.95 CPU /
  17.4 GiB limits, Kata VM ≈ 17.7 GiB, with a 40 GiB workspace (dependencies.md §7, re-based on
  WS-02's pod in wave 1).
- Testcontainers-based suites must be adapted to declared services; until then they surface as
  `environment` (`docker_api_unavailable`) with remediation text, not as regressions.
- Service data lives under `/workspace/.services/<name>` inside the enforcing workspace limit; no `dataVolume` (OQ-05-18 decided, R1-114). p95 workspace use with service data ≈ 23.5 GiB of 40 GiB.

environments (out of scope in v1).

## Revisit when

- WS-09 admits a device path for `/dev/net/tun` into Kata guests without `privileged` (device plugin), and WS-02's
  benchmark shows rootless Podman with that device works; then offer option D per codebase, Kata only.
- Remote dev/staging environments come into scope (option G).
