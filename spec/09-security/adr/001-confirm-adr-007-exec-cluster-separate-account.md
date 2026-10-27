# ADR-09-001: Confirm ADR-007 (separate exec cluster) and place it in a separate AWS account

- **Status:** Accepted; CCR-09-07 applied (C-ARCH 1.1.0; four exec roles in C-ARCH 1.4.0). Confirms ADR-007; the account split was
  because C-ARCH §4 draws one account.
- **Constraints:** D-06, D-21, D-22

## Context

ADR-007 was accepted provisionally; WS-02 and WS-09 may propose reversal with evidence. The
question is whether a VM- or user-space-kernel boundary per pod is strong enough to co-locate
sandboxes with the control plane, or whether a second cluster is worth its cost.

Evidence gathered on 2026-10-24:

- Kata Containers published, in 2026 alone, a Critical guest-root → host-root escape through
  virtio-fs (GHSA-2gv2-cffp-j227, CVE-2026-47243), a Critical annotation → arbitrary file load as
  host root (GHSA-mp2j-xm59-qfgw, CVE-2026-50540), annotation-driven virtiofsd argument injection
  that exposes the host root filesystem (GHSA-rr59-xxvx-96qr, CVE-2026-44210), annotation host bind
  mounts (GHSA-7fhf-v3p3-rp56), and Dragonball virtio escapes [verified:
  https://github.com/kata-containers/kata-containers/security/advisories, 2026-10-24].
- Cloud Hypervisor: virtio-block use-after-free (High, 2026-05) and host file exfiltration through
  QCOW backing files (High, 2026-02) [verified:
  https://github.com/cloud-hypervisor/cloud-hypervisor/security/advisories, 2026-10-24].
- Firecracker: virtio-pci out-of-bounds write (High, 2026-04) and a jailer symlink overwrite
  (2026-01) [verified: https://github.com/firecracker-microvm/firecracker/security/advisories,
  2026-10-24].
- runc: three High container escapes on 2025-11-05 [verified:
  https://github.com/opencontainers/runc/security/advisories, 2026-10-24]. Any non-sandbox pod on a
  shared node is exposed to this class.
- AWS states that containers are not a security boundary and that node applications (kubelet,
  CSI drivers) hold permissions beyond a single pod [verified:
  https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html, 2026-10-24].

Escapes are not hypothetical for any candidate runtime. What matters is what an escaped attacker
finds next.

## Options

1. **Single cluster** with tainted sandbox node pools, Cilium host policies, separate subnets.
   An escape lands on a node in the same cluster as Temporal, the broker, and Postgres clients: the
   same API server, the same CNI control plane, the same IAM account, and possibly the same VPC
   endpoints. One RBAC or network-policy mistake is enough to pivot.
2. **Separate exec cluster, same AWS account** (ADR-007 as drawn). Removes the shared API server
   and cluster-scoped objects. Still shares the account: node roles, VPC, endpoints, SCP scope, and
   IAM blast radius (an over-broad role trust or a resource policy with `Principal: *` in the
   account is reachable from exec nodes).
3. **Separate exec cluster in a separate AWS account** (chosen). The only crossings are explicit:
   PrivateLink endpoint services, cross-account bucket and KMS key policies naming the exec
   account's two roles, and nothing else.
4. Separate exec cluster per tenant group. Highest isolation, about 40× operational load. Kept for
   `restricted` tenants if R-01 is reassessed (ADR-007 "revisit when").

## Decision

Confirm ADR-007 and adopt option 3:

- `replicant-<env>-exec` account holds `fct-<env>-exec`, the run-token KMS key, the
  `sandbox-artifacts` and `runner-api` roles, and the exec VPC. SCP
  [scp.replicant-exec-account.json](../policies/iam/scp.replicant-exec-account.json) denies IAM writes
  outside the pipeline, IMDSv1 and hop limits above 1, internet gateways and NAT outside the
  pipeline, S3 outside Replicant buckets, and other regions.
- Sandbox subnets have **no default route**. Allowed destinations are VPC endpoints (S3 gateway with
  the sandbox endpoint policy, STS/KMS interface endpoints for exec-system subnets only) and
  PrivateLink endpoints to the core NLBs (memory-mcp, context-mcp, runner-sandbox is local) and to
  the AI gateway and Artifactory. repo-mirror's subnet alone routes to github.com through a central
  egress VPC with AWS Network Firewall domain allowlisting (`github.com`, `api.github.com`).
- Core exposes only these endpoint services to the exec account: memory-mcp, context-mcp,
  token-broker (consumer: repo-mirror subnet only), temporal-frontend (consumer: run-notifier subnet
  only). Exec exposes the runner orchestration listener to core worker subnets only.

## Consequences

- An escaped attacker on a sandbox node holds: the node's minimal role (ECR pull, EKS node
  registration), the kubelet credential (its own pods' Secrets: in-flight run tokens of co-located
  runs), and network reach to PrivateLink endpoints that authenticate every request. No Temporal,
  Postgres, GitHub, or core IAM reach. This is R-01 in the threat model.
- Cost: one more account (no direct cost), PrivateLink endpoint hours and data processing
  `[unverified: current PrivateLink pricing; WS-10 estimates]`, cross-account IAM to maintain.
- EKS Pod Identity in the exec cluster assumes exec-account roles; cross-account access to core
  buckets is through resource policies, not role chaining, so the 1-hour chaining cap applies only to
  the Runner-vended sandbox sessions (already 1 h by design).
- Local `k8s` profile stays one kind cluster (documented parity gap, WS-11).

## Revisit when

- An isolation runtime offers attested, formally verified device models and two years without a
  guest-to-host advisory.
- A tenant is classified `restricted` with a regulatory need for dedicated compute: create a
  dedicated exec cluster for that tenant group (option 4).
