# ADR-09-003: EKS Pod Identity for workload identity (IRSA not used)

- **Status:** Accepted for the WS-09 design (2026-10-24). Decides D-22's "IRSA or EKS Pod Identity".
- **Constraints:** D-22, D-21

## Context

Two EKS clusters in two accounts (ADR-09-001), about 20 workload roles, and per-run sandbox
credentials that must be bound to one run's prefixes.

## Options

1. **IRSA.** OIDC provider per cluster in IAM; `sts:AssumeRoleWithWebIdentity` from each SDK; trust
   policies keyed on `sub`; no session tags; consumes the account's STS quota; can assume
   cross-account roles directly [verified: comparison table in
   https://docs.aws.amazon.com/eks/latest/best-practices/identity-and-access-management.html,
   2026-10-24].
2. **EKS Pod Identity.** Associations managed in EKS; one service principal
   `pods.eks.amazonaws.com`; six automatic session tags (cluster, namespace, service account, pod
   name and UID) for ABAC; no OIDC provider; agent DaemonSet on `hostNetwork` at
   `169.254.170.23`; up to 5,000 associations per cluster; cross-account through role chaining or the
   association's target role; optional association session policy (requires
   `disableSessionTags=true`); Linux EC2 only [verified:
   https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html and
   https://docs.aws.amazon.com/eks/latest/APIReference/API_CreatePodIdentityAssociation.html,
   2026-10-24]. AWS recommends Pod Identity unless a specific use case needs IRSA [verified: best
   practices page above].

## Decision

Option 2 for all Replicant workloads. Trust policies require the namespace and service-account
session tags, the source account, and the cluster ARN
([runner-api.trust.json](../policies/iam/runner-api.trust.json)). Cross-account S3 and KMS access
uses resource policies, not chaining. **Sandboxes have no association**: their credentials come
from the Runner, and the Pod Identity agent address is denied to sandbox pods by Cilium
([ccnp-sandbox-baseline.yaml](../policies/network/ccnp-sandbox-baseline.yaml)), because the agent
would otherwise be reachable from pod networking.

## Consequences

- The Pod Identity agent is a node-level credential vending point; on sandbox nodes it serves no
  association in `sbx-*` namespaces, and it is unreachable from sandbox pods.
- Pod Identity requires `iam:PassRole` for whoever creates associations; only the pipeline role
  has it (SCP).
- Association changes are eventually consistent (seconds); they are never on a hot path.

## Revisit when

- Fargate or Windows workloads are introduced (Pod Identity does not support them).
