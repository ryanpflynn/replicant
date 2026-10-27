# ADR-06-002: Eval cases run as real ReplicantJobs through the production intake path

- **Status:** Accepted (WS-06, 2026-10-24)
- **Constraints:** D-03, D-04, D-10, D-20; C-JOBREQ (`channel=eval`, `related.eval_case`), C-IDS (`EvalSuite`, `EvalCase`)

## Context

The harness must measure the bundle as production runs it: the graph interpreter on the bundle's pinned
worker version, the Runner and controller, real sandboxes, native CLIs, the gateway, memory snapshots. C-JOBREQ
already reserves `channel = eval`, `related.eval_case`, and `requested_release_bundle_id` for the harness, but
does not say whether an eval case *is* a job.

## Options

1. **Separate eval runner** that calls the Runner API directly with the bundle's agent definitions. Cheaper to
   build, but it skips the graph, the router, gates, context assembly, and repair cycles, so it measures a
   different system. It would also duplicate orchestration logic outside Temporal workflows.
2. **Langfuse experiment runner** (`dataset.run_experiment`). The task function runs in-process with the SDK
   [verified: https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk, 2026-10-24], which does
   not fit a 60-minute, multi-sandbox, durable job.
3. **Each case submits a JobRequest and observes a real ReplicantJob (chosen).**

## Decision

`EvalCase` (eval-worker) submits a JobRequest through `POST /v1/jobs` in tenant `replicant-eval` with
`requested_release_bundle_id`, waits by timer and polling `GET /v1/jobs/{id}`, then scores in a separate
`role: eval` AgentRun. Evaluation mode (ADR-06-006) removes humans and publishing. Langfuse experiments are
populated through OpenTelemetry attributes, not the SDK runner.

## Consequences

- The eval measures the production code paths, including bundle resolution and pinning (ADR-006).
- Eval load competes with production for sandboxes. Capacity and priority are specified (eval-harness.md §3.5).
- eval-worker needs only control-plane-api and Runner credentials; it never touches replicant-worker internals.
- A defect in intake or the workflow breaks evals too. That is a feature: the sentinel detects it.

## Revisit when

- Eval volume exceeds about 30% of production sandbox capacity on a sustained basis, which would argue for a
  dedicated exec cluster or node pool for `replicant-eval`.
