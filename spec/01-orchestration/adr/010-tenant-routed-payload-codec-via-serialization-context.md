# ADR-01-010: Route tenant payload keys through the SDK serialization context

- **Status:** Accepted (WS-01, 2026-10-24). Implements XD-09-06 (WS-09 secrets.md §6.2).
- **Constraints:** D-21, D-22, C-IDS (workflow ID = `job_id`, no tenant in it), ADR-01-005

## Context

WS-09 requires every Temporal payload to be encrypted with a data key from the **job's tenant** KMS key,
with encryption context `{"replicant:purpose": "temporal-payload", "replicant:tenant": <tenant>}`, so that every
decrypt is tenant-checkable and audited. A `PayloadCodec` sees payloads, not jobs. The workflow ID is the
`job_id`, which contains no tenant (C-IDS), and one namespace serves all tenants (ADR-01-005).

## Options

1. **Per-tenant namespaces** with a codec configured per namespace. 40× worker and release operations
   (rejected in ADR-01-005).
2. **Tenant inside the workflow ID** (`<tenant>:<job_id>`). A C-IDS breaking change, and it leaks tenant
   names into every Temporal surface.
3. **Codec looks up the tenant by `job_id`** in Postgres or control-plane-api on every encode. I/O in the
   codec hot path, plus a new dependency for every worker.
4. **Serialization context plus a learned directory (chosen).** The Python SDK passes the workflow ID to
   codecs that implement `WithSerializationContext` (`WorkflowSerializationContext.workflow_id`,
   `ActivitySerializationContext.workflow_id`) [verified: introspected `temporalio==1.34.0`, 2026-10-24].
   The tenant is learned from the authenticated metadata of the first decoded payload of that workflow,
   or registered by the client that starts or signals it.

## Decision

Option 4 ([codec.py](../reference/replicant_orchestration/codec.py)): `TenantContextCodec(key_provider,
TenantDirectory)`. Decode verifies AES-GCM (tenant as associated data) and records `job_id → tenant`. Encode
looks the tenant up and **fails closed** if unknown. `sys:*` workflows use `replicant-system`. Clients that
originate payloads for a job (control-plane-api, github-integration, run-notifier) register the tenant
before encoding; they know it from the intake row or the notification. WS-09 provides the KMS
`KeyProvider` (GenerateDataKey/Decrypt with the encryption context, data keys cached 5 min or 10,000
payloads).

Evidence: scenario `tenant_codec` ran a full job (gates, PR, merge) with every payload encrypted. All 117
activity inputs and the start input carried the encrypted encoding and tenant label, search attributes
stayed clear, and every run replayed through the codec [verified: test run, 2026-10-24].

## Consequences

- No contract change, and no I/O in the codec beyond KMS.
- Every task's first step is a decode, so the directory is always warm before encoding. A process restart
  clears it safely (the next decode refills it).
- A bug that made a client encode without registering the tenant fails loudly (`PermissionError`), never
  silently writes cleartext.
- Child workflows `<job_id>:<kind>:<n>` map to their job's tenant by prefix.

## Revisit when

- Temporal adds first-class per-workflow encryption key selection, or the SDK exposes search attributes
  or memo in the serialization context.
