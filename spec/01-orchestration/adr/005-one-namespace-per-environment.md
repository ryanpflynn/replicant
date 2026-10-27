# ADR-01-005: One Temporal namespace per environment; tenancy by search attributes, task-queue fairness, and payload keys

- **Status:** Accepted (WS-01, 2026-10-24). Answers OQ-00-08 with WS-09's tenant-keyed codec (XD-09-06).
- **Constraints:** D-01, D-21, D-22, C-IDS §4

## Context

There are 40 tenants and one replicant-worker Worker Deployment with several live versions (up to 6, see
[temporal-platform.md §4](../temporal-platform.md#4-worker-topology-and-scaling)). Isolation concerns:
payload confidentiality across tenants, noisy neighbors, and operator access in the Temporal UI.

## Options

1. **Per-tenant namespaces.** Strong separation. But 40 × (Worker Deployment registrations, pollers per
   live version, Schedules, search attribute registration). A deployment is per namespace, so release
   operations (ramp, drain, retire) multiply by 40. Cross-tenant maintenance batches and system workflows
   need a home. Per-namespace limits apply (`matching.maxDeployments` = 100 per namespace [verified:
   temporalio/temporal v1.32.1 constants.go, 2026-10-24]).
2. **One namespace per environment (chosen),** with:
   - tenant identity in search attributes (`ReplicantTenantId`) and every workflow input;
   - **payload encryption keyed per tenant** (WS-09 `TenantKmsCodec`), so a decrypt in the codec server is
     tenant-authorized and audited. The SDK's serialization context supplies the workflow ID
     ([temporal-platform.md §7](../temporal-platform.md#7-payload-codec-and-codec-server));
   - **Fairness keys = tenant** on task queues, so one tenant's burst cannot starve others
     ([temporal-platform.md §5](../temporal-platform.md#5-rate-limits-concurrency-and-tenant-fairness));
   - Temporal UI access only for `replicant-operators` (C-MSG §1). Tenant users never see the Temporal UI;
     they use the control plane, which authorizes per tenant.

## Decision

Option 2.

## Consequences

- One Worker Deployment and one set of release operations per environment.
- Search attribute values are **not** encrypted by the codec [verified: https://docs.temporal.io/search-attribute,
  "stored unencrypted … not processed by a custom Payload Codec", 2026-10-24]. Replicant stores only IDs,
  enums, and repo names in search attributes, never free text.
- A regulated tenant that requires separate Temporal persistence gets a dedicated environment (namespace
  plus exec cluster), not a namespace in the shared cluster.

## Revisit when

- A tenant contract requires a separate Temporal persistence store or a distinct operator group.
- Tenants exceed about 200, or one tenant's volume needs its own frontend rate limits.
