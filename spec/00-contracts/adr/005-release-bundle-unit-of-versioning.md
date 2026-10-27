# ADR-005: The Release Bundle is the unit of versioning

- **Status:** Accepted (WS-00, 2026-10-24)
- **Constraints:** D-02, D-03, D-10, D-14

## Context

Replicant behavior depends on graph definitions, worker code, agent and environment definitions,
prompts (in Langfuse), CLI images, model identifiers, policies, and memory snapshot policy. These
change independently and frequently. Jobs live up to 30 days. Without one versioned unit,
reproducing or rolling back a job's behavior requires reconstructing a dozen independent versions,
and evaluation cannot attribute changes.

## Options

1. **Independent versioning per artifact**, with jobs recording each version used. Every pairing is
   possible and untested. Rollback is per artifact. Evaluation cannot gate combinations.
2. **Git commit of the config repo as the version.** Misses Langfuse prompts, images, and worker
   builds, and is not content-addressed across environments.
3. **A closed, content-addressed bundle of pinned references (chosen).**

## Decision

- `ReleaseBundle` is a resolved, **closed** set of pinned references (`ref@version#digest`). It
  covers exactly the D-03 elements and pins exactly one `replicant-worker` deployment version.
- `release_bundle_id = rb_ + b32_130(sha256(JCS(spec)))`, so identical content yields an identical
  ID, in every environment.
- Bundles are immutable. Promotion state lives beside the bundle, not in it.
- Only promoted bundles are assignable outside the eval tenant (WS-06).
- Every job records its bundle at start: the workflow input, the search attribute, the intake
  record, the projection, and Langfuse `release`.
- Runtime components resolve definitions **only through the job's bundle**.

Schema: [schemas/release-bundle.schema.json](../schemas/release-bundle.schema.json). Rules:
[definitions-contracts.md](../definitions-contracts.md).

## Consequences

- Any change, including a one-line prompt edit, produces a new bundle, which must be evaluated and
  promoted. WS-06 must make that cheap through tiered evals.
- Rollback means assigning the previous bundle. It takes effect for new jobs at once. Running jobs
  stay pinned unless migrated.
- Memory is not in the bundle (it changes continuously). Bundles pin the **snapshot policy**, and
  each job pins its **snapshot**, which keeps jobs reproducible (D-14).

## Revisit when

- The bundle count makes the releases UI unmanageable. Group by base bundle and tenant layer
  ([ADR-013](013-behavioral-vs-operational-config-tenant-derived-bundles.md)).
