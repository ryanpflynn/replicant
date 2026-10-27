# ADR-01-007: Maintenance fan-out: child jobs started with ParentClosePolicy ABANDON; progress by Signal; sliding window in the parent

- **Status:** Accepted (WS-01, 2026-10-24). Requires CCR-01-04 (Signal `batch.child_progress`).
- **Constraints:** D-01 (child workflows), C-IDS §2 (child job workflow ID = child `job_id`;
  `intake.create_child_job`), ADR-009 (child jobs live through PR review)

## Context

A scheduled maintenance sweep targets up to 1,500 repositories (`target_selector.max_repos`). Each repo is
a full job: its own PR, gates, review, and repair, living up to 30 days. Facts:

- At most 2,000 pending child executions per workflow (`limit.numPendingChildExecutions.error`) [verified:
  temporalio/temporal v1.32.1 constants.go, 2026-10-24].
- Default `ParentClosePolicy` is TERMINATE. Values: TERMINATE, ABANDON, REQUEST_CANCEL [verified: introspected
  `temporalio==1.34.0` `workflow.start_child_workflow`, 2026-10-24]. A parent that continues-as-new closes
  its run, so TERMINATE children would die.
- A child on a task queue in the parent's pinned deployment version inherits the parent's version [verified:
  https://docs.temporal.io/worker-versioning#inheritance-semantics, 2026-10-24].

Each child start plus completion costs about 8 history events in the parent, and progress tracking adds
more, so 1,500 children exceed the 4,096-event CAN suggestion. The parent must continue-as-new several times.

## Options

1. Parent awaits child results (TERMINATE). Holds the parent open for 30 days and breaks across CAN.
2. Independent jobs started by an intake activity, tracked by visibility polling. Loses parent/child
   inheritance (bundle pin) and adds polling cost.
3. **Child workflows with ABANDON, explicit IDs, progress Signals to the parent by workflow ID (chosen).**

## Decision

- `MaintenanceBatch` (workflow type registered in C-IDS) resolves targets once (`catalog.resolve_targets`,
  recorded), then keeps a **sliding window**: at most `max_concurrency` (default 10) children in active states
  (`queued`, `running`, `waiting_human`). Children in `waiting_external` (PR in review) do not count
  (ADR-009).
- Each child is created by activity `intake.create_child_job` (explicit ID
  `batch.c0.n<i>.create_child`, allocates `job_id` and the intake row through control-plane-api so
  `parent_job_id` and the bundle assignment exist), then started with
  `workflow.start_child_workflow("ReplicantJob", …, id=<child job_id>, parent_close_policy=ABANDON,
  versioning inherited, search attributes ReplicantParentJobId)`.
- Children report progress with **`SignalExternalWorkflowExecution`** of `batch.child_progress {child_job_id,
  status, phase, seq}` to the parent's workflow ID (CCR-01-04) at status changes. The parent keeps a compact
  status table (1,500 × about 40 bytes) in its carry and continues-as-new by its own thresholds.
- `batch.cancel` (Update) → the parent sends `RequestCancelExternalWorkflowExecution` to every
  non-terminal child. Each child treats native cancellation exactly like `job.cancel`, with the batch
  requester as actor (ADR-001: "native cancel … handled the same way").
- The parent completes when every child is terminal, or after the batch expiry (`max(children PR expiry)`).
  Children keep running independently if the parent is terminated by an operator.

## Consequences

- Children are fully independent jobs (own gates, PRs, repair budgets) and still inherit the batch's pinned
  version, so one batch runs on one build.
- The parent history stays bounded through CAN; children are unaffected by parent CAN (ABANDON).

## Revisit when

- Temporal supports child handles that survive parent CAN, or batches regularly exceed 5,000 repos (then
  shard into sub-batches of 500).
