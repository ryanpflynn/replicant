# ADR-007: Separate EKS cluster for execution

- **Status:** Accepted (WS-00, 2026-10-24). **Amended by CCR-09-07 (C-ARCH 1.1.0):** the exec
  cluster lives in its own AWS account. WS-09 [ADR-09-001](../../09-security/adr/001-confirm-adr-007-exec-cluster-separate-account.md)
  confirms this ADR with 2026 escape advisories for every candidate runtime, and strengthens it.
- **Constraints:** D-04, D-06, D-21, D-22

## Context

Sandboxes run untrusted code and prompt-injectable agents with a shell (D-21). The isolation
runtime (gVisor or Kata) is the first boundary. If it fails, an attacker controls a node. In a
shared cluster that node can:
- use the kubelet credential, which NodeRestriction limits to that node's pods;
- reach host-network paths to in-cluster services that pod-level NetworkPolicies do not cover;
- read metadata and IMDS unless blocked;
- sit on the same VPC subnets as Temporal, Postgres clients, and the GitHub token broker.

## Options

1. **One cluster**, with tainted sandbox node pools, Cilium host policies, and separate subnets.
2. **Separate exec cluster per environment (chosen).**
3. **Separate exec cluster per tenant group.** More isolation, 40 times the operational load. Not
   now.

## Decision

Option 2:
- `fct-<env>-core` hosts control plane, workers, Temporal, Langfuse, and Grafana.
- `fct-<env>-exec` hosts runner-api, agentrun-controller, run-notifier, repo-mirror, egress
  control, and sandbox pools.
- Cross-cluster paths are explicit internal NLBs with security groups:
  - core workers → runner-api;
  - run-notifier → Temporal frontend;
  - sandbox pools → memory-mcp, context-mcp, and the runner sandbox listener;
  - sandboxes → `replicant-gateway-shim` (`ai-gateway.<env>.replicant.internal`, a core PrivateLink endpoint service under the CCR-09-07 amendment) and the package mirror. Sandboxes never reach the enterprise AI gateway directly (CCR-03-03).
- The exec cluster holds no GitHub App key, no Postgres credentials, and no Temporal credentials
  except the notifier's Signal-only certificate.

### Amendment (CCR-09-07): separate AWS account

- The exec cluster lives in AWS account `replicant-<env>-exec`. Core stays in `replicant-<env>-core`.
- Cross-account paths are only:
  - PrivateLink endpoint services: exec exposes the Runner orchestration listener to core worker
    subnets; core exposes temporal-frontend (run-notifier subnet), memory-mcp, context-mcp, and
    replicant-gateway-shim (sandbox subnets), and github-token-broker (repo-mirror subnet).
  - Core-account S3 bucket policies and KMS key policies that name exactly four exec roles:
    - `sandbox-artifacts`;
    - `runner-api`;
    - `agentrun-controller`, added on CCR-02-04 and merged with CCR-09-08 and CCR-02-10:
      `s3:GetObject` on every object under the run's three prefixes (S3 authorizes HeadObject as
      GetObject, so this role can read run content), registry blob and pointer GETs, write-once
      `PutObject` plus `PutObjectTagging` on `agentrun_record/*` and `inbox/_control.json`, and KMS
      via S3. No list, delete, or bypass, and no `audit`, `memory`, or `eval` access;
    - `run-notifier`: tenant-key `kms:GenerateDataKey` only, with no Decrypt, to encode Signal
      payloads under the tenant codec. It has no S3 access.

    C-ARCH §4 has the exact list.
- Sandbox subnets have **no default route**. Only the repo-mirror subnet routes to GitHub, through a
  central egress VPC with AWS Network Firewall domain allowlisting.
- The exec account is SCP-restricted (WS-09 `policies/iam/scp.replicant-exec-account.json`).
- Reason: a separate cluster in the same account still shares node roles, VPC, endpoints, and IAM
  blast radius. A separate account makes every crossing explicit.

## Consequences

- A sandbox escape reaches a cluster, in an account with no core resources, whose most sensitive credentials are the Runner's KMS signing
  permission (run tokens) and its STS role for per-run prefixes. WS-09 sizes this blast radius.
- A second EKS control plane (about $0.10 per hour, roughly $73 per month, per cluster under standard support `[unverified: current EKS pricing; WS-10 verifies]`),
  duplicated add-ons, and cross-cluster observability (Alloy in both clusters).
- One more AWS account and PrivateLink endpoint hours `[unverified: current PrivateLink pricing; WS-10 estimates]`.
- The local `k8s` profile uses a single kind cluster. This is a documented parity gap (WS-11).

## Revisit when

- Concurrent runs exceed about 1,000 (shard exec clusters), or regulated tenants need dedicated
  exec clusters (option 3 for those tenants).
