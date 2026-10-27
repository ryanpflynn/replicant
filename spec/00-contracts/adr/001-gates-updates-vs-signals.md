# ADR-001: Temporal Updates vs Signals for human gates and job control

- **Status:** Accepted (WS-00, 2026-10-24)
- **Constraints:** D-01, D-18, D-19

## Context

D-01 says human gates are LangGraph interrupts backed by Temporal Updates (preferred) or Signals,
with the choice justified per gate type. D-19 requires first-valid-decision-wins across UI, Slack,
and GitHub, idempotent duplicates, and every channel updated to show who decided. These facts
bear on the choice:

- A rejected Update writes no history events [verified:
  https://github.com/temporalio/temporal/blob/main/docs/architecture/workflow-update.md, 2026-10-24].
- An Update is durable once **accepted**. An Update that has been **admitted** but not yet accepted
  lives in memory and can be lost. When no worker is polling, the SDK client retries indefinitely
  [verified: same source; https://docs.temporal.io/develop/python/message-passing, 2026-10-24].
- Temporal deduplicates Update IDs per workflow run only [verified:
  https://docs.temporal.io/handling-messages, 2026-10-24].
- Limits: 10 in-flight Updates per execution, 2,000 per run [verified:
  https://docs.temporal.io/cloud/limits, 2026-10-24].
- Signals are durable in history without a worker, return no result, and are limited to 10,000 per
  execution [verified: https://docs.temporal.io/encyclopedia/workflow-message-passing, 2026-10-24].
- The Temporal LangGraph plugin's documented human-in-the-loop example uses a Signal plus a Query.
  It defines no handlers of its own, so Replicant writes the handlers itself [verified:
  https://docs.temporal.io/develop/python/integrations/langgraph, 2026-10-24].

## Options

1. **Signals for everything.** Durable without workers. The sender learns nothing: no "already
   approved by Alice", no denial reason, and stale or duplicate clicks all land in history.
2. **Updates for everything.** A validated, synchronous result. They need a live worker, and
   machine senders (notifier, webhooks) would block on worker availability.
3. **Per message type (chosen).** Updates where a human waits for an outcome, Signals where a
   machine reports facts.

## Decision

| Message | Primitive | Justification |
|---|---|---|
| `gate.decide` (plan, publish, clarification, budget, migration) | Update | First-wins outcome and winner identity are returned to the channel, and every channel is updated with them. Validators reject stale views (`presented_digest`), closed gates, and non-humans without writing history. The authoritative authorization check runs in the handler (activity) at decision time |
| `job.steer` | Update | The user needs to know whether delivery was live or queued |
| `job.cancel`, `job.pause`, `job.resume`, `job.takeover` | Update | Authorization result plus resulting status. Cancel goes through an Update rather than Temporal's native cancel so the actor is recorded and validated. Native cancel stays an operator fallback, handled the same way |
| `job.offer_bundle_migration` | Update | The operator needs eligibility feedback |
| `run.status_changed` | Signal | Machine hint, no result needed. Must not depend on worker availability ([ADR-002](002-runner-completion-callback-vs-poll.md)) |
| `pr.event` | Signal | Feedback events must be durable while workers are down. Ordering and buffering are done in the workflow |
| `release.notice`, `identity.principal_changed` | Signal | Fan-out via batch operations; informational |

Mitigations for Update weaknesses: senders persist pending actions in an outbox and retry with the
**same Update ID** (`decision_id`, `message_id`, `action_id`), with a 10 s attempt timeout. The
workflow carries applied IDs across Continue-As-New (7-day window) to extend dedupe beyond one run.

## Consequences

- Approvals depend on a live `replicant-worker` for the job's pinned version. During worker outages,
  decisions queue in sender outboxes and the UI shows "recording…".
- Duplicate and stale clicks cost nothing in history.
- Handler concurrency: an asyncio lock per gate serializes decisions, and the second decision
  returns `superseded`.

## Revisit when

- Temporal makes admitted Updates durable, or offers server-side cross-run dedupe.
- Approval volume approaches the 2,000 Updates-per-run limit (it will not at the current scale; this
  is about 50 times headroom per run with Continue-As-New per cycle).
