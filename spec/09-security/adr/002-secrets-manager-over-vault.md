# ADR-09-002: AWS Secrets Manager as the secret store (not Vault)

- **Status:** Accepted for the WS-09 design (2026-10-24). Decides the "Secrets Manager or Vault"
  question left open in C-ARCH §1.3.
- **Constraints:** D-21, D-22, D-17 (open-source preference applies to observability; not stated
  for secrets)

## Context

Replicant needs storage for about 40 secrets per environment (webhook and signing secrets, OAuth
client secrets, SCIM token, third-party component secrets), rotation for a few database passwords,
and nothing dynamic: database access uses IAM authentication, AWS access uses Pod Identity, and
GitHub tokens come from the broker. The most sensitive key (GitHub App) is held in KMS, not in a
secret store.

## Options

1. **AWS Secrets Manager.** Managed, multi-AZ, KMS-encrypted, per-secret resource policies,
   CloudTrail per read, managed rotation for RDS. $0.40 per secret per month and $0.05 per 10,000
   API calls [verified: https://aws.amazon.com/secrets-manager/pricing/, 2026-10-24]. Pod Identity
   works with it directly and with External Secrets Operator.
2. **HashiCorp Vault** (self-hosted). Dynamic secrets, PKI, transit encryption. Licensed under the
   BSL since August 2023 `[unverified: license date re-checked from memory; not re-fetched]`; the
   Replicant would operate an HA, auto-unsealed cluster as tier-0 infrastructure.
3. **OpenBao**, the community fork of Vault under the Linux Foundation's OpenSSF [verified:
   https://openbao.org/, 2026-10-24]. Same operational burden as Vault.

## Decision

Option 1. Replicant's secret needs are static and AWS-native; dynamic-secret features would go
unused because IAM DB auth, Pod Identity, and the broker already give short-lived credentials. A
self-hosted secrets cluster would be the most security-critical service Replicant runs and would
add an unseal-key ceremony and its own HA story without reducing risk.

## Consequences

- Secrets are AWS-region-scoped; DR to another region needs replication (`ReplicaRegions`).
- Local mode uses a file-backed stub implementing the same `replicant-secrets` interface (WS-11).
- If a later need for an internal PKI beyond cert-manager + AWS Private CA arises, revisit
  OpenBao.

## Revisit when

- Replicant leaves AWS or needs dynamic credentials for systems without native short-lived auth.
