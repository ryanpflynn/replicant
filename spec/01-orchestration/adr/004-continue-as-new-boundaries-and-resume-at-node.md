# ADR-01-004: Continue-As-New at declared boundaries and history safe points; resume at a node

- **Status:** Accepted (WS-01, 2026-10-24)
- **Constraints:** D-02 ("Continue-As-New at repair-cycle and phase boundaries to bound history size"),
  C-STATE §3 (carry ≤ 256 KiB), C-IDS §3 (job identity), ADR-009

## Context

Temporal limits: 51,200 events or 50 MB per run is an error; 10,240 events or 10 MB is a warning;
`limit.historyCount.suggestContinueAsNew` = 4,096 and `limit.historySize.suggestContinueAsNew` = 4 MB
drive `workflow.info().is_continue_as_new_suggested()` [verified: temporalio/temporal v1.32.1
`common/dynamicconfig/constants.go`, 2026-10-24]. Updates are limited to 2,000 per run and Signals to
10,000 per execution [verified: same file, `history.maxTotalUpdates`, `history.maximumSignalsPerExecution`].
A job lives up to 30 days in PR review with up to 5 repair cycles. Measured first segments are 445 to 741
events (bugfix, two gates, test polling cadence; [history-budget.md](../history-budget.md)).

Pinned versions are inherited across Continue-As-New [verified: https://docs.temporal.io/worker-versioning,
2026-10-24], so CAN never changes code by itself.

## Options

1. CAN only on history thresholds. Simple, but repair cycles then share runs and history size varies by job.
2. CAN at every node boundary. Many runs and wasted CAN cost.
3. **Declared boundaries plus threshold safe points (chosen).**

## Decision

- **Declared boundaries** in each GraphDefinition (`spec.boundaries`):
  - `phase` before `pr_wait`: entering PR review, taken only when entering from another phase (so a
    `pr_wait → pr_wait` self-loop for ignored events does not CAN every time);
  - `repair_cycle` before `triage`: always taken; the cycle counter increments, and per-cycle visit
    counters reset.
- **Threshold safe points:** before every node, inside every gate and PR wait loop, and inside every
  Runner-run wait loop outside parallel branches (amended, R2-048), the workflow continues-as-new if
  `is_continue_as_new_suggested()` is true or `get_current_history_length() > 8,000`. Both values come
  from the WorkflowTaskStarted event, so they are replay-safe.
- **Other CANs:** reroute (docs → bugfix), consented migration (ADR-01-002).
- **Never mid-activity. Mid-run-wait only at a poll boundary (amendment, Phase 3 fix round, R2-048).** The
  original text said "never mid-run-wait" and estimated a node's history at about 1,000 events. The history
  model showed a single long run with hints and steering can reach about 4,150 events
  ([history-budget.md](../history-budget.md) §2). So the run waiter now treats each poll iteration as a
  history safe point. The next run re-enters the node, finds `runner.start_run` memoized (same idempotency
  key and `run_id`), and keeps polling the same AgentRun. This is never done inside a parallel branch,
  because the join would wait for a sibling the next run does not re-enter. Measured: with the ceiling
  lowered to 150 events, one implementer run spanned 9 Temporal runs with a single AgentRun [verified:
  scenario `mid_run_can`, 2026-10-24]. The per-run bound is therefore the server suggestion (about 4,100
  events) for every node.
- **Resume at a node.** The compiled graph has a conditional edge from `START` to `JobState.resume_at`. A
  node interrupted before CAN (a gate or PR wait) is **re-entered**. `JobState.active_nodes` keeps its
  ordinal, and `JobState.memo` returns the results of its completed side-effecting steps, so re-entry issues
  no duplicate commands and opens no second gate.
- **Before CAN:** prune dedupe windows (7 days), stage a steering backlog of more than 4 messages to S3,
  upsert `ReplicantRepairCycle`, emit the projection, wait at most 10 min for outstanding projection
  activities (C-STATE §2.1), then `wait_condition(workflow.all_handlers_finished)`.
- **Carry:** `JobState.carry()` (schema `replicant.wf-carry/v1`, JSON ≤ 256 KiB, `CarryTooLarge` fails
  loudly). Field list: [versioning.md §4.3](../versioning.md#43-what-carries-over).

## Consequences

- Per-run history stays bounded at about 4,096 events by the server suggestion, regardless of job length.
- One job is a chain of runs under one workflow ID (`job_id`). Updates and Signals address the workflow ID,
  so they follow the chain (C-IDS §3).
- Update ID dedupe is per run, so the carried applied-ID set (7 days) gives cross-run dedupe (C-MSG V3).

## Revisit when

- Temporal raises the suggest thresholds, or offers server-side cross-run Update dedupe.
