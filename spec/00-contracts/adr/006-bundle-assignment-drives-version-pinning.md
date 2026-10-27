# ADR-006: Bundle assignment decides the pinned worker deployment version at start

- **Status:** Accepted (WS-00, 2026-10-24). WS-01 designs the mechanics and may refine them through
  a contract change request.
- **Constraints:** D-02, D-03

## Context

D-02 requires Temporal Worker Versioning (deployments, versions, pinned or auto-upgrade, ramping),
and operator ramps **by percentage or tenant**. D-03 says a bundle fully determines behavior,
including the worker deployment version.

Temporal's own ramp is percentage-based. Via `SetWorkerDeploymentRampingVersion`, the server
chooses the version for new workflows on a task queue [verified:
https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning, 2026-10-24].
Temporal cannot ramp by tenant and does not know which bundle a job got. If the server picked the
worker version independently of the bundle, a job could get bundle B, which expects worker N+1,
while running on worker N.

## Options

1. **Temporal's ramp picks the worker version**, and the workflow then selects a bundle matching it.
   Config-only bundles, which do not change the worker, cannot be ramped by this mechanism. No
   tenant ramps.
2. **Per-version task queues.** Defeats Worker Deployments.
3. **Control-plane bundle assignment picks the bundle, which determines the worker version; start
   the workflow with a pinned versioning override (chosen).**

## Decision

- At intake, control-plane-api resolves the bundle: default, tenant override, ramp by job-hash
  percentage and tenant lists, and tenant derivation
  ([definitions-contracts.md §4.5](../definitions-contracts.md#45-bundle-assignment-operational-not-in-the-bundle)).
  It starts `ReplicantJob` with `versioning_override = Pinned{deployment_name: replicant-worker,
  build_id: bundle.spec.worker_deployment.build_id}`. `StartWorkflowExecutionRequest.versioning_override`
  exists [verified:
  https://github.com/temporalio/api/blob/master/temporal/api/workflowservice/v1/request_response.proto,
  2026-10-24], and the Python SDK exposes `PinnedVersioningOverride` [verified:
  https://python.temporal.io/temporalio.common.VersioningOverride.html, 2026-10-24].
  The Python keyword `client.start_workflow(…, versioning_override=PinnedVersioningOverride(…))` is
  [verified: introspected temporalio 1.34.0 and experiment, 2026-10-24; WS-01 versioning.md §2]. WS-01 verified that a start-time pinned override may target a version that is
  neither current nor ramping, provided it has registered its task queues (V01-01, 2026-10-24). The earlier fallback (setting the override right after start with
  `UpdateWorkflowExecutionOptions`) is no longer needed.
- Workflow types are declared `VersioningBehavior.PINNED`. Pinned versions are inherited across
  Continue-As-New and by pinned children on task queues in the same deployment [verified:
  https://docs.temporal.io/worker-versioning, 2026-10-24].
- Temporal's **current version** is set to the worker version of the default bundle, so workflows
  without an override (system workflows) land correctly. Temporal's ramping version is not used for
  `ReplicantJob`.
- Before a bundle can be promoted, its worker deployment version must be registered with pollers
  present. The releases UI shows pinned-workflow counts per version via the
  `TemporalWorkerDeploymentVersion` search attribute.
- **A running job picks up a node-code fix it has not reached yet** (WS-01 must-answer) only through
  the consented migration path: `job.offer_bundle_migration` → `bundle_migration` gate →
  Continue-As-New onto the new bundle, with the override updated. Temporal's upgrade-on-Continue-As-New
  is public preview [verified:
  https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning/upgrade-on-continue-as-new,
  2026-10-24]. WS-01 decides whether to use it or set the override explicitly via
  `UpdateWorkflowExecutionOptions`. Emergency fixes that must reach pinned workflows without consent
  use `workflow.patched()` in a new build of the **same** version line (WS-01 policy).

## Consequences

- Ramps by tenant, percentage, and config-only change all work the same way.
- Old worker versions must keep running until their pinned workflows drain, up to 30 or more days.
  Worker cost is several concurrent versions (WS-01 sizes this).
- Activities on other deployments (`publisher-worker`, `replicant-projector`) are not pinned with the
  job. Their activity contracts must stay backward compatible for at least 37 days (rule CC-4).

## Revisit when

- Temporal adds attribute-based (tenant) ramping or a first-class external routing hook.
