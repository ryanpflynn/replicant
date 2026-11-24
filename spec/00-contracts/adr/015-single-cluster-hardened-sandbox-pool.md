# ADR-015: Single cluster, hardened sandbox pool

- **Status:** Accepted (SIMPLIFY Phase 6, 2026-11-23; replicant/docs/v0/DEVIATIONS.md SIMPLIFY-D50..D53).
  **Supersedes** [ADR-007](007-separate-execution-cluster.md) (separate execution cluster),
  [ADR-09-001](../../09-security/adr/001-confirm-adr-007-exec-cluster-separate-account.md) (exec cluster in its own
  AWS account) and [ADR-09-005](../../09-security/adr/005-run-grants-bound-exec-side-minting.md) (core-signed run
  grants). Applied in C-RUNNER 4.0.0 and C-CRD 4.0.0.
- **Constraints:** D-04, D-06, D-21, D-22

## Context

ADR-007 put sandboxes, the Runner, the controller, the notifier and repo-mirror in a second EKS cluster, and
ADR-09-001 moved that cluster to a second AWS account. Because the Runner then lived beside the sandboxes, the most
exposed place in the system, ADR-09-005 added a core-signed run grant: the worker signs what a run may do, the Runner
must embed it in every token, and every verifier checks two signatures and two revocation lists.

The v0 build showed the cost of that shape:

- two clusters, two accounts, PrivateLink endpoint services in both directions, a KMS key class only for grants, a
  grants listener in core, and a JWKS and revocation list that every verifier polls twice;
- a second control plane and duplicated add-ons for what is, by run count, a small pool of sandbox nodes;
- most of what the second cluster protected has since left the exec side: repo-mirror is replaced by a git bundle
  staged in the run inputs (SIMPLIFY-D40, ADR-011 note), so sandboxes reach no git endpoint at all; the notifier
  lives inside the Runner; AgentRun is a bare Pod plus a write-once S3 record (controller.md).

What actually separates untrusted code from everything else is the node, not the cluster: the runtime boundary
(gVisor or Kata), then the node's credentials, network position and host access. Those can be hardened per node pool.

## Options

1. **Keep ADR-007/09-001/09-005.** Strongest blast-radius story; highest cost; the grant machinery exists mainly
   because the Runner sits next to the sandboxes.
2. **One cluster, sandboxes on a hardened node pool, Runner on core nodes, no run grants (chosen).**
3. **One cluster, keep run grants.** Grants bound a compromised *Runner*. With the Runner on core nodes its
   compromise is a core compromise, which grants do not bound either (the worker holds the grant key). Cost without
   benefit.

## Decision

Option 2.

### The four-endpoint invariant

A sandbox pod reaches exactly four named endpoints, and nothing else:

| Name (prod `<env>.replicant.internal`; local `*.sbx.local`) | What | Authenticated by |
|---|---|---|
| egress proxy (`egress`, SNI allowlist) | package registries and other allowlisted HTTPS by name | SNI allowlist, no credentials |
| runner sandbox listener (`runner-sandbox`) | token refresh, per-run S3 credentials, steering inbox, heartbeat | run token, aud `runner-sandbox` |
| gateway-shim (`ai-gateway`) | the only model path | run token, aud `ai-gateway` |
| memory/MCP port (`memory-mcp`, later `context-mcp`) | memory and context reads | run token, aud `memory-mcp` / `context-mcp` |

Plus the run's own S3 prefixes through the per-run session credentials (no network identity of its own). DNS
inside the sandbox resolves only these names (sbx-edge locally, SIMPLIFY-D30/D31; a Cilium FQDN policy in a
cluster, [ADR-02-005](../../02-execution/adr/02-005-egress-cilium-fqdn.md)); everything else is NXDOMAIN and direct-IP egress is dropped.

Because every crossing is a **name**, splitting back to two clusters (or accounts) later is "put PrivateLink behind
the same four names": no contract, token or code change. Any new sandbox dependency must be one of these four or
amend this ADR.

### Sandbox node-pool hardening

- **Placement** (refines [ADR-02-007](../../02-execution/adr/02-007-sandbox-node-pools.md) for a shared cluster).
  A dedicated node pool with a `replicant.example.com/sandbox=true:NoSchedule` taint; only AgentRun
  pods tolerate it (admission policy `vap-sandbox-pods`), and nothing else is scheduled there (no Runner, no core
  workloads, no DaemonSets beyond CNI, log shipping and the runtime installer).
- **Network.** Own subnets with no default route and their own security groups: ingress none; egress only to the four
  endpoints' addresses. Cilium host firewall (`CiliumClusterwideNetworkPolicy` with `nodeSelector`) on the sandbox
  nodes blocks node-originated traffic to everything but the API server, the CNI and the registry endpoint.
- **Credentials.** IMDS blocked from pods (hop limit 1 plus a host-firewall drop of 169.254.169.254); the node role
  can only pull from ECR (`AmazonEC2ContainerRegistryPullOnly`) and register the node; **no EKS Pod Identity
  agent and no Pod Identity associations** on sandbox nodes, so no pod there can obtain an AWS role. Per-run S3 access
  comes only from the Runner's STS session credentials, scoped to the run's prefixes.
- **Runtime.** Every sandbox pod runs under a sandboxing RuntimeClass, gVisor or Kata
  ([ADR-02-001](../../02-execution/adr/02-001-default-runtime-per-workload-class.md) chooses per workload class); runc is refused by admission. NodeRestriction limits the kubelet credential to the node's pods.
- **Secrets.** The sandbox nodes hold no core secrets: no GitHub App key, no Postgres, no Temporal credentials, no
  signing keys. The Runner, its signing key and the notifier run on core nodes.

### Run authorization without grants

- The Runner signs run tokens with its own key and serves its JWKS (`/.well-known/jwks.json`, both listeners) and
  its revocation feed (`GET /v1/revocations`). Verifiers (gateway-shim, memory/MCP, the sandbox listener) use only
  these: one key source, one revocation source.
- **Mitigation for a compromised worker.** The worker sends `RunSpec.run_scope` (repo_ids, audiences, scopes,
  acting_for). Before accepting a run the Runner asks core, with its own caller identity,
  `GET /internal/v1/jobs/{job_id}/run-scope?tenant_id=…` and requires: the job exists for that tenant and is not
  revoked; `run_scope.repo_ids` ⊆ the job's targets; the run's repos ⊆ `run_scope.repo_ids`; `acting_for` equals the
  job's requester. A compromised worker can therefore not mint tokens for a repo or tenant outside a job core
  accepted, nor act for another principal.
- **Revocation.** The Runner polls core's `GET /internal/v1/revocations` (cancelled, failed and taken-over jobs;
  cancelled and security-failed runs) and copies every listed run of its own into its feed (reason `job_revoked`);
  verifiers close every run of a `job_revoked` job.

## Consequences

- One EKS control plane, one account, no PrivateLink between core and exec, no `run-grant` KMS key class, no grants
  listener, no second JWKS or revocation list. C-RUNNER 4.0.0 and C-CRD 4.0.0 are breaking changes (run_grant →
  run_scope).
- **Accepted risks** (security review owns them; threat-model §7 needs a pass):
  - a sandbox escape lands on a node in the same cluster and VPC as core. The hardening above is what stands between
    it and core; its credentials are a pull-only node role and a kubelet limited by NodeRestriction;
  - a compromised **Runner** (core node) can mint tokens for any tenant, as any core service could act for any
    tenant. Grants never bounded this case once the Runner moved to core;
  - a compromised **worker** can still start runs for any *existing, live* job of any tenant (it knows job ids), on
    that job's repos and for that job's requester. Grants did not bound this either (the worker held the grant key).
    Closing it needs core to bind runs to workflow ownership (for example a per-job capability minted at intake);
    revisit if a tenant requires it.
- The local stacks already match: runner-local is the Runner, notifier included, and the compose sandbox network
  exposes the four names through sbx-edge.

## Revisit when

- A regulated tenant requires account-level separation: split by putting PrivateLink behind the four names (and the
  Runner orchestration listener) and move the sandbox pool to its own account; the contracts do not change.
- Concurrent runs exceed about 1,000, or the sandbox pool needs its own control-plane scaling.
