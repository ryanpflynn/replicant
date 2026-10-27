# ADR-01-002: Consented bundle migration through a handoff run and a pinned override

- **Status:** Accepted (WS-01, 2026-10-24)
- **Constraints:** D-02, D-03, CC-3, ADR-006, C-MSG `job.offer_bundle_migration`

## Context

A pinned job moves to a newer bundle only with consent, at a safe point (CC-3). The target bundle may pin a
different `replicant-worker` build that is **not** the deployment's current version (a tenant ramp, a
candidate, an older promoted bundle on rollback). Temporal offers two primitives:

- **Upgrade-on-Continue-As-New** (`workflow.continue_as_new(initial_versioning_behavior=
  ContinueAsNewVersioningBehavior.AUTO_UPGRADE | USE_RAMPING_VERSION)`) moves the next run to the current
  or ramping version only. Its docstring says "currently experimental", and the docs label it Public
  Preview [verified: introspected `temporalio==1.34.0`;
  https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning/upgrade-on-continue-as-new,
  2026-10-24].
- **`UpdateWorkflowExecutionOptions` with a pinned override** moves a running execution to any version.
  The docs warn that the target must be able to replay the execution's history ("you may also need to use
  the other Versioning APIs to patch your Workflow in the 'fixed' build") [verified:
  https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning/roll-out-and-pin#moving-a-pinned-workflow,
  2026-10-24]. The Python SDK has no handle method for it, only the raw RPC
  `client.workflow_service.update_workflow_execution_options` [verified: introspected 1.34.0, 2026-10-24].

Moving a long-running execution to arbitrary new code would replay its entire history on that code, which
is exactly the nondeterminism pinning exists to avoid.

## Options

1. Upgrade-on-CAN. Preview; cannot target an arbitrary build (only current or ramping); breaks
   tenant-scoped and candidate migrations.
2. `UpdateWorkflowExecutionOptions` on the live run. Replays a long history on new code: unsafe.
3. **Handoff run (chosen):** Continue-As-New into a run whose history is a frozen, version-independent
   prologue, then re-pin that short run.

## Decision

**Migration Handoff Protocol v1** (code: `ReplicantJobBase._migration_prologue`; spec:
[versioning.md §6](../versioning.md#6-long-waits-and-consented-migration)):

1. The `bundle_migration` gate (requester consent) is approved. At the next safe point (a node boundary, or
   inside a gate or PR wait) the workflow drains projections and handlers, then Continue-As-New with
   `mode=migration_handoff`, the carry state, and `{target_build_id, target_release_bundle_id}`. The new
   run inherits the pinned source version N.
2. The new run's **first and only command** in every build is activity `release.apply_migration_override`
   (activity ID `release.c0.n1.apply_migration_override`). It asks control-plane-api to call
   `UpdateWorkflowExecutionOptions(workflow_id, run_id, pinned=N+k)`.
3. The run then waits, issuing no commands, until `workflow.info().get_current_build_id()` equals the
   target. A stale task routed to a source-version worker (sticky cache) therefore yields without commands.
4. Only target-version code passes that point. It restores the carry (upcasting if needed), records the new
   `release_bundle_id` (search attribute, projection `bundle.migrated`), recomposes the graph from the new
   bundle, and re-enters the node it was waiting in.

The handoff run's history before the re-pin is identical in every build, so the target build replays it
exactly. CI enforces the frozen prologue with a golden history ([versioning.md §8](../versioning.md#8-replay-testing)).

Evidence: experiment exp3 (two builds `b20261001-aaaaaaaaaaaa` and `b20261024-bbbbbbbbbbbb` on server 1.32.0)
completed the handoff with zero `WorkflowTaskFailed` events in the live histories. After the handoff a
node that exists only in build B executed. Build B's code replays both runs. Build A's code fails to replay
the post-migration run, as expected, since only the target must replay it [verified: experiment, 2026-10-24].

## Consequences

- Uses only GA mechanisms (Continue-As-New, pinned overrides). The preview feature is not needed.
- One extra run and about 15 history events per migration.
- `release.apply_migration_override` is the only replicant-worker activity that changes Temporal state, and
  it goes through control-plane-api, which holds the `UpdateWorkflowExecutionOptions` permission (C-MSG §1).
  G-23 enumeration: [temporal-platform.md §9](../temporal-platform.md#9-worker-side-temporal-calls-g-23).
- If control-plane-api is down, the activity retries (IO_INTERNAL policy, 30 min) and the run stays on N,
  waiting. After the schedule-to-close the job raises an operator alert and remains on N (a safe state).

## Revisit when

- Upgrade-on-CAN reaches GA **and** accepts an explicit target version. Then steps 2 and 3 collapse into
  the CAN options.
