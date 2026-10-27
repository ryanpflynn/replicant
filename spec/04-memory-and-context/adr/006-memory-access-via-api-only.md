# ADR-04-006: replicant-worker reads memory only through the memory-service API

- **Status:** Accepted (WS-04, 2026-10-24); resolves the C-PG open question
- **Constraints:** D-25, D-09, C-PG rule 1

## Context

C-PG allowed replicant-worker a read-only role on `memory` "if WS-04 allows". Snapshot creation is a
write (bindings, holdouts, usage), selection logic must be identical for workers, eval, and MCP, and
memory reads must respect RLS and D-25 views.

## Decision

No service other than memory-service gets privileges on `memory` (grafana_ro sees analytics views
only). replicant-worker uses `POST /internal/v1/snapshots`, `…:select`, `/candidates`, `/outcomes`,
`/episodes`, `/feedback-signals`; control-plane-api uses `/internal/v1/jobs/{id}/memory` and the
admin API.

## Consequences

- One implementation of selection and ranking; one place for audit.
- memory-service is on the critical path of context assembly; it runs 3 replicas, and activities
  retry with idempotency keys.

## Revisit when

- memory-service latency dominates context assembly (p95 > 1 s).
