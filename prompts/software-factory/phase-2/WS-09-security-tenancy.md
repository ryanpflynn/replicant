# WS-09: Security, Tenancy, and Threat Model

**Output directory:** `spec/09-security/`
**Constraints most relevant:** D-06, D-18, D-21, D-14, D-19

## Purpose

Produce the threat model and the security and tenancy architecture. Other workstreams design
controls in their areas; you own the model that says which controls are required, verify the
others' designs against it, and fill gaps.

## Required outputs

### 1. Threat model (`threat-model.md`)

STRIDE or attack-tree analysis covering at least:

- Sandbox escape and lateral movement (per runtime candidate from WS-02).
- Prompt injection via issues, PR comments, repo files, dependency content, tool output, and
  briefings; what an injected agent can do with its capabilities.
- Credential exfiltration from the sandbox (gateway token, artifact upload credentials, MCP tokens).
- Supply chain: malicious dependencies installed during QA, poisoned shared caches, base images,
  CLI binaries.
- Memory poisoning (with WS-04).
- Approval bypass and confused deputy (with WS-07).
- Data exfiltration through PR content, artifacts, or egress.
- Insider misuse by operators.
- Denial of wallet (runaway token spend) and resource exhaustion.

For each: likelihood, impact, controls, residual risk, and detection.

### 2. Capability model for agents (`agent-capabilities.md`)

What each role's sandbox can reach and do (network destinations, credentials, file access, MCP
tools), designed so that a fully prompt-injected agent cannot push code, approve its own work,
access other tenants, or exfiltrate beyond its allowed destinations.

### 3. Secrets and credentials (`secrets.md`)

Secret storage (AWS Secrets Manager or Vault; decide), workload identity (IRSA vs EKS Pod Identity),
per-run short-lived credentials (issuance, scope, TTL, revocation), GitHub App private keys
(storage, rotation, separate dev and production apps, and what a leaked dev-app key can reach),
installation tokens used only by the Publisher and the webhook/reply service, webhook secrets, encryption keys (KMS) for Temporal payloads, S3, and Postgres.

Database access (D-26): no standing human access to any Postgres instance; per-service roles with
least privilege; break-glass via short-lived IAM database authentication with approval and session
audit.

### 4. Tenancy (`tenancy.md`)

Tenant isolation at every layer: Temporal (namespace or search attributes plus authorization),
Kubernetes (namespaces, node pool separation for high-sensitivity tenants, quotas, priority
classes), network, object storage (prefix policies and per-tenant KMS context), Postgres (RLS),
memory scopes, observability (Loki/Grafana tenancy), and gateway budgets. Noisy-neighbor controls.

### 5. Data classification and compliance (`data-handling.md`)

Classes of data the factory handles (source code, secrets found in code, PII in logs, transcripts),
handling rules per class, retention and deletion, and controls mapped to common frameworks (SOC 2
style) at a level useful to an auditor.

### 6. Review of other workstreams (`control-review.md`)

After reading the other Phase 2 outputs (coordinate through `spec/_coordination/`; if they are not
ready, state assumptions and mark the review for R2), list each required control, where it is
designed, and gaps found.

## Must answer

- An agent is fully prompt-injected inside a QA run. List everything it can and cannot do.
- A tenant's repo contains a secret. Where can it end up, and what stops it?
- What is the blast radius of a compromised Runner API?

## Acceptance criteria

- Every threat has a mapped control or an accepted residual risk with an owner.
- The capability model is concrete enough to write network policies and IAM policies from.
