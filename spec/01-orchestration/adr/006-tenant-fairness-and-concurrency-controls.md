# ADR-01-006: Tenant fairness and concurrency: fairness keys on task queues, Runner quotas as the hard limit, intake admission for open jobs

- **Status:** Accepted (WS-01, 2026-10-24)
- **Constraints:** D-04, D-21; scale table (150 concurrent AgentRuns, 1,000 jobs/day peak); ADR-009 (count
  `waiting_external` separately)

## Context

The scarce resources, in order: sandbox capacity (150 concurrent AgentRuns), model spend (gateway budgets,
WS-03), in-worker LLM calls (router, triage), and Temporal worker slots (plentiful). A tenant with a
2,000-repo maintenance sweep must not starve others.

Temporal Task Queue Priority and Fairness: `temporalio.common.Priority(priority_key: int | None,
fairness_key: str | None, fairness_weight: float | None)`. `priority_key` runs 1..5 (lower runs sooner,
default 3). `fairness_key` is at most 64 bytes. `fairness_weight` is clamped to 0.001..1000. Activities and
child workflows inherit priority. Self-hosted enables Fairness with dynamic config
`matching.enableFairness=true`; Priority is on by default. GA status: a 2026-05-12 Temporal blog says GA,
while some doc snippets still say Public Preview [verified: temporalio/sdk-python `common.py`; temporalio/temporal
v1.32.1 constants.go (`matching.enableFairness` default false, `matching.useNewMatcher` default true);
https://temporal.io/blog/task-queue-priority-and-fairness-your-task-queue-your-way, 2026-10-24].
`[unverified: the blog's instruction to also set matching.enableMigration=true; the key is not in v1.32.1
or main source]`.

## Decision

| Resource | Control | Where | Value (initial) |
|---|---|---|---|
| Sandbox capacity | **Hard:** Runner admission per-tenant quota (429 `tenant_quota_exceeded`, Retry-After) | runner-api (WS-02); workflow retries `runner.start_run` honoring `Retry-After` (`RUNNER_START`: unlimited attempts, schedule-to-close 6 h) and shows `waiting_capacity` | WS-02 sets per-tenant concurrency from policy (for example 20 per tenant; 150 global) |
| Fair dispatch of activity tasks | `Priority(priority_key=map(job.priority), fairness_key=tenant_id, fairness_weight=tenant weight)` on every `execute_activity` and child start | replicant-worker workflow code (`NodeContext.act`) | priority: urgent 1, high 2, normal 3, low 4; maintenance children 5. Weight 1.0, configurable per tenant (operational config, read at start) |
| In-worker LLM calls | Separate queue `replicant-llm-calls` with `Worker(max_task_queue_activities_per_second=…)` (server-enforced) plus `max_concurrent_activities` | replicant-worker deployment | 20 activities/s, 32 concurrent per pod |
| Open jobs per tenant | Intake admission: control-plane-api counts `ReplicantTenantId = T AND ExecutionStatus = 'Running' AND ReplicantStatus IN ('queued','running')` (excluding `waiting_external` and `waiting_human`, ADR-009). Above the limit, intake persists the request with `start_status=deferred`; the intake retrier starts it later | control-plane-api (WS-07) using visibility; WS-01 defines the query | 50 active per tenant; unlimited waiting |
| Maintenance fan-out | Sliding window inside `MaintenanceBatch` (`max_concurrency` children not yet in `waiting_external`) | MaintenanceBatch workflow | 10 per batch; tenant cap 2 concurrent batches |
| Frontend protection | `frontend.namespaceRPS` (default 2,400 per instance) and per-caller rate limits on the notifier identity | Temporal dynamic config (WS-10 operates) | default; notifier capped by C-MSG §1.1 |

Fairness is enabled in self-hosted (`matching.enableFairness: true` in dynamic config for namespace
`replicant-<env>`). If an upgrade or an incident shows fairness misbehaving, turning it off falls back to
plain FIFO. The Runner quota remains the hard guarantee, so fairness is an optimization and not load-bearing.

## Consequences

- No distributed semaphore workflow is needed. The scarce resource (sandboxes) is already gated by the
  Runner, and waiting is a cheap retrying activity.
- Temporal Cloud bills fairness at an extra 0.1 action per action [verified: https://docs.temporal.io/cloud/actions,
  2026-10-24]. This is relevant only if WS-10 chooses Cloud.

## Revisit when

- Sandbox concurrency exceeds about 1,000 (ADR-007 sharding); per-tenant exec clusters change the quota owner.
