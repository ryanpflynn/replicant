# ADR-01-003: `workflow.patched()` only in emergency patch builds; everything else is a new deployment version

- **Status:** Accepted (WS-01, 2026-10-24). Requires CCR-01-01 (`release.notice` kind `bundle_patched`).
- **Constraints:** D-02, D-03, CC-2, CC-3, ADR-006

## Context

Pinned jobs run their start build's code for up to 30 days and more. Two kinds of fix must reach them:

- **Normal fixes** (a better prompt, a node bug that misparses results). These can wait for the next safe
  point and the requester's consent (CC-3).
- **Emergency fixes**, where waiting is unsafe or blocks jobs: a security bug in workflow code, a crash
  loop at a node every pinned job will reach, or data corruption. They must reach pinned workflows without
  per-job consent.

Temporal's patching API (`workflow.patched(id)` returns false while replaying history recorded before the
patch, true otherwise; `workflow.deprecate_patch(id)` once no old histories remain) lets one build serve
old and new histories [verified: introspected `temporalio==1.34.0` docstrings;
https://docs.temporal.io/develop/python/workflows/versioning, 2026-10-24]. A build identity is its
`build_id`, and the bundle pins `worker_deployment.build_id` and `image_digest` (D-03).

## Decision

1. **Default: new worker deployment version, no `patched()`.** All code changes ship as a new build N+1 and a
   new bundle. Running jobs stay on N. They pick up N+1 only through consented migration (ADR-01-002). Builds
   never need to replay histories of other builds, so normal builds carry no patch branches.
2. **Emergency patch build N.p (allowed only with an incident ID and two approvers in the release UI):**
   - Build from N's commit plus the fix. Every behavior change is guarded by `if workflow.patched("inc-<id>")`.
   - New `build_id` (deployment versions are immutable), and NodeCatalog `workerDeployment.patchOf = N`.
   - Replay gate: **every** open workflow pinned to N (fetched live from Temporal by the replay-gate Job, versioning.md §8)
     must replay on N.p with zero failures before anything moves.
   - Registry builds a **patch bundle** B' = B with only `worker_deployment` and `node_catalog` changed and
     `lineage.parent_release_bundle_id = B`. Promotion is mechanical: the bundle differs only in worker
     build, and the replay gate passed.
   - control-plane-api moves the workflows **one at a time** (R1-012). It lists
     `TemporalWorkerDeploymentVersion = 'replicant-worker:N' AND ExecutionStatus = 'Running'`
     (`ListWorkflowExecutions`) and calls `UpdateWorkflowExecutionOptions` with pinned override N.p per
     workflow, rate-limited to 20/s with an audited progress record. Both APIs are in control-plane-api's
     C-MSG §1 grant. C-MSG grants `StartBatchOperation` for signals only, so the batch form (CLI `temporal
     workflow update-options --query …` [verified: temporalio/cli v1.9.1 commands.yaml, 2026-10-24]) is
     **not** used and no contract change is needed. It then sends Signal
     `release.notice(kind=bundle_patched, recommended_release_bundle_id=B')` as a batch Signal, which the
     grant covers.
   - On the notice (processed in the main flow at the next wait iteration or safe point), the workflow
     checks that B' is B's patch lineage (activity `release.check_migration_eligibility` with
     `lineage: patch`, ID `release.c0.n<k>.patch_check`). It records `release_bundle_id = B'` in state,
     `ReplicantReleaseBundleId` and the projection (`bundle.patched`), and continues. No consent is needed,
     because the patch bundle differs only in worker code that replays every recorded history. An
     ineligible notice is ignored with timeline `notice.rejected`. Implemented and tested (R1-005)
     [verified: scenario `bundle_patched`, 2026-10-24].
   - Assignments that pointed at B now point at B', so new jobs start on N.p as well.
   - N is drained like any version. `deprecate_patch` is never needed, because the patch line dies with its
     drained version.
3. **When `patched()` is not acceptable, use a new version plus consented migration:** any change to
   workflow state shape, carry schema, the order or IDs of side-effecting activities already executed by
   live histories, or graph topology for nodes jobs have already passed.

**Must-answer: how does a running job pick up a fix in node code it has not reached yet?**
(a) Normally through consented migration at its next safe point: the release UI offers it
(`job.offer_bundle_migration`), the requester approves the `bundle_migration` gate, and the handoff run moves
it. Eligibility requires that the job's current node exists in the target graph. (b) In an emergency
through a patch build (rule 2). A fix in a node *not yet reached* adds no commands to recorded histories, so
it usually needs no `patched()` guard at all. The replay gate proves it either way.

## Consequences

- Most builds contain no patch code, so long-lived branches never accumulate.
- Patch builds are rare, audited (incident ID), and proven by replay against the actual live histories.
- D-03 holds: each patched job records the patch bundle it now runs on.

## Revisit when

- Temporal adds first-class "patch version" semantics for pinned versions.
