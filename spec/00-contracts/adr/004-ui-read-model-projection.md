# ADR-004: UI read model as a Temporal → Postgres projection

- **Status:** Accepted (WS-00, 2026-10-24)
- **Constraints:** D-01 (Temporal is the source of truth), D-05, D-09

## Context

A job view combines state from four planes: the workflow (phase, gates), etcd (run phase), S3
(artifacts), and Postgres (cost, memory used, registry). The UI must show progress within 60 s and
must not query four stores per page load. Querying Temporal has three problems: Queries need a
polling worker for the workflow's pinned version; visibility is eventually consistent and limited
to search attributes; and fan-out over list pages would be expensive.

## Options

1. **The UI calls Temporal (Describe and Query) per page.** Fragile when workers are down, slow for
   lists.
2. **Change-data capture** from Temporal persistence. Couples Replicant to Temporal's internal
   schema. Rejected.
3. **Projection activity at each transition, plus reconciliation (chosen).**
4. **Workers write Postgres directly from workflow code.** Not possible: workflow code must be
   deterministic. A local activity in the workflow worker would put database credentials into every
   versioned worker. Rejected in favor of a dedicated projector deployment.

## Decision

- At every transition the workflow emits a `ProjectionEvent` carrying a full `JobSnapshot` with a
  monotonic `seq`. It does so through a non-awaited activity on task queue `replicant-projections`,
  served by `replicant-projector`, the only writer of `jobs_read` and `approvals`.
- Apply is idempotent and order-insensitive (seq guard).
- `NOTIFY` drives live push.
- `sys:projection-reconciler` compares the `ReplicantProjectionSeq` search attribute with the stored
  seq every 2 minutes and repairs drift via Query or Describe.
- Full rebuild is supported.

Details: [state-ownership.md §2](../state-ownership.md#2-ui-read-model-projection-must-answer-a-consistent-job-view-without-querying-four-stores).

## Consequences

- The UI is eventually consistent, typically within 2 s. Human actions are read-your-own-write
  through Update results.
- The projector must accept every snapshot schema version emitted by any live worker version for at
  least 37 days.
- The history cost is about 4 events per transition. It is budgeted by WS-01.

## Revisit when

- Temporal ships a supported change feed or event export API suitable for projections.
