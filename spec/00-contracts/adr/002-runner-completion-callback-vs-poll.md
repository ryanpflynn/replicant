# ADR-002: Runner completion via Signal hint plus timer polling (not a long-poll activity)

- **Status:** Accepted (WS-00, 2026-10-24)
- **Constraints:** D-04, D-05, D-23

## Context

D-04: activities call the Runner API and receive a run ID. The workflow waits for completion via a
Runner-originated Signal or Update, with a timer timeout plus a polling fallback activity. Runs
last minutes to hours (median job 25 minutes; `activeDeadlineSeconds` up to 8 h). Someone on the
execution side must hold Temporal credentials to send the Signal. The execution cluster is the
most exposed part of the system ([ADR-007](007-separate-execution-cluster.md)).

## Options

1. **Long-poll heartbeating activity** for the whole run. It holds a worker slot per run (150 at
   peak). Heartbeat timeouts and resumption add complexity. No execution-side Temporal credentials
   are needed. Completion latency equals the poll interval inside the activity.
2. **Update from the Runner on completion.** Gives a result, but the Runner then depends on worker
   availability, and an admitted Update can be lost.
3. **Signal from a dedicated notifier, as a hint, plus workflow timer polling (chosen).**

## Decision

- The **agentrun-controller** posts a `RunStatusNotification` for every phase transition to
  **run-notifier**, an in-cluster HTTP endpoint authenticated by ServiceAccount token.
- **run-notifier** is the only execution-side holder of Temporal client credentials. It uses an
  mTLS certificate (cert-manager, 24 h). It is authorized for `SignalWorkflowExecution` only, on
  `replicant-<env>`, via the self-hosted Temporal Authorizer. It validates
  `callback.workflow_id` against the run's `job_id` and Signals `run.status_changed` by workflow ID
  only.
- The workflow treats the Signal as a **hint**. Its wait loop is
  `wait_condition(hint, timeout=poll_interval)` followed by activity `runner.get_run` (10 s
  start-to-close). Intervals are 15 s while Pending or Provisioning and 120 s while Running
  (policy `runner_polling`). It decides only from the `GET` response. An overall timer of
  `startup + active + finalize + 300 s` triggers `runner.cancel_run` and failure with
  `OrchestratorDeadlineExceeded`.
- Why not have the Runner Signal directly? Separating the processes keeps Temporal credentials out
  of the API process, which handles inbound traffic and is a larger target. The notifier has no
  ingress except the controller.

## Consequences

- A forged or replayed Signal only causes an extra poll. A compromised notifier can only cause
  extra polls and history growth, bounded by limits and alerts.
- History cost per run is about 1 Signal plus about 6 to 15 poll activities (3 events each) plus
  timers. Roughly 40 to 60 events per run. WS-01 includes this in its history budget.
- Completion latency is the time from notification to Signal, usually under 2 s. Without
  notifications it is at most one poll interval.
- Temporal Cloud has no per-API authorization for API keys or mTLS identities. If WS-10 picks Cloud,
  the notifier's credential has the namespace `write` role. The hint-only design is what keeps that
  acceptable.

## Revisit when

- Temporal Nexus or a callback-based external-completion primitive becomes the idiomatic way to
  complete a wait from an external system with narrowly scoped credentials. In that case,
  re-evaluate replacing the notifier.
- Poll volume becomes material. It will not at 150 concurrent runs (about 1.3 polls per second
  cluster-wide).
