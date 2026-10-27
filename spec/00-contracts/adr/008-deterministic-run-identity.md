# ADR-008: Deterministic run identity; idempotency on the AgentRun name

- **Status:** Accepted (WS-00, 2026-10-24)
- **Constraints:** D-04 (idempotency key from workflow ID + activity ID + attempt semantics), D-05,
  D-23 (Runner idempotency lives on the AgentRun resource)

## Context

The Runner must never create duplicate runs on activity retries, and it has no database (D-23).
D-04 derives the key from "workflow ID + activity ID + attempt semantics". Taken literally with
Temporal's default activity IDs, that is unsafe. The Python SDK assigns `activity_id = str(seq)`
by default [verified:
https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow_instance.py,
2026-10-24], and the sequence restarts after Continue-As-New. A second run's activity "7" would
then collide with the first run's activity "7". Code changes that insert an activity would also
shift every later key.

## Options

1. **Default activity IDs.** Unsafe, as above.
2. **Random run IDs, plus a label-selector lookup by key-hash label.** Has a race window between
   lookup and create.
3. **Explicit logical activity IDs plus a deterministic `run_id`, with etcd name uniqueness as the
   atomic check (chosen).**

## Decision

- Every side-effecting activity is started with an explicit `activity_id =
  <node_key>.c<cycle>.n<ordinal>.<effect>`. The key is `ik1:<workflow_id>:<activity_id>`, which is
  workflow ID plus activity ID, satisfying D-04 literally. **Attempt semantics:** Temporal retry
  attempts reuse the key. A deliberate re-run decided by the workflow increments `ordinal`.
- `run_id = derived("run", tenant_id, key)` and AgentRun name `ar-<body>`. A concurrent duplicate
  create gets `AlreadyExists`. The Runner compares `replicant.example.com/spec-digest` and returns
  200 for an identical spec or 422 `idempotency_key_reuse` for a different one.
- Each AgentRun executes the agent at most once. The controller never retries.

## Consequences

- The workflow can compute `run_id` before calling the Runner, so it can stage inputs under the run
  prefix in advance.
- AgentRuns must be retained for at least the longest window in which a retry could arrive (45
  days), or 410 is returned.
- Workflow authors must use explicit IDs. Enforcement: a WS-01 CI lint, plus a regex check in the
  Runner-call activity.

## Revisit when

- A Temporal SDK offers stable, user-keyed activity identity across Continue-As-New natively.
