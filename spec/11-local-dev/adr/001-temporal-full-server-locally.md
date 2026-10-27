# ADR-11-001: Local Temporal is the full server on PostgreSQL 16, not the CLI dev server

- **Status:** Accepted (WS-11, 2026-10-24)
- **Constraints:** D-01, D-02, D-20, D-22; C-IDS §4.1 (search attributes); ADR-003

## Context

The brief asks us to decide between `temporal server start-dev` and the full server with Postgres.
Facts:

- The CLI dev server (`temporalio/temporal` image, CLI 1.9.1 at time of writing) uses SQLite and is one
  process. [verified: https://hub.docker.com/r/temporalio/temporal tags, 2026-10-24]
- Production is self-hosted Temporal 1.32.x on PostgreSQL 16 with SQL advanced visibility (ADR-003),
  whose custom search attribute limits (Keyword 10, …) are what C-IDS §4.1 is sized against.
- The official compose examples moved from the archived `temporalio/docker-compose` repo to
  `temporalio/samples-server/compose`; the Postgres variant runs `temporalio/server`, schema setup via
  `temporalio/admin-tools` with `temporal-sql-tool --plugin postgres12 … setup-schema -v 0.0` and
  `update-schema -d /etc/temporal/schema/postgresql/v12/{temporal,visibility}/versioned`, and namespace
  creation via `temporal operator namespace create`. [verified:
  https://github.com/temporalio/docker-compose (archived 2026-01-05) and
  https://github.com/temporalio/samples-server/tree/main/compose, 2026-10-24]
- Current images: `temporalio/server:1.32.1`, `temporalio/admin-tools:1.32.1` (2026-10-08),
  `temporalio/ui:2.55.0` (2026-10-01). [verified: Docker Hub tag API, 2026-10-24]

## Options

1. **CLI dev server.** One container, ~150 MB, starts in seconds. Different persistence (SQLite) and
   visibility implementation from production. Schema migrations, shard count, dynamic config surface,
   and search-attribute registration behave differently.
2. **Full server on PostgreSQL 16 (chosen).** Same image, persistence plugin (`postgres12`), schema
   tooling, SQL visibility limits, and dynamic config keys as production. ~600 MB more RAM and a
   one-shot schema job.

## Decision

Option 2 for the compose stack: `postgres-temporal` (16.14) → `temporal-schema` (admin-tools one-shot)
→ `temporal` (all four services in one process, `NUM_HISTORY_SHARDS=4`) → `temporal-bootstrap`
(namespace `replicant-local`, retention 30 d, the 14 C-IDS search attributes) → `temporal-ui` (behind Dex
OIDC) and `temporal-codec-server`.

The dev server is still used where it is the right tool: `WorkflowEnvironment.start_local()` in
integration tests that need a real server without the stack (test-strategy.md §3).

## Consequences

- Search-attribute overflow (an 11th Keyword) fails locally exactly as it would in production.
- `temporal-sql-tool update-schema` runs on every `replicant dev up`, which is the same pre-upgrade step
  WS-10 runs in production.
- Shard count is 4 locally and is fixed at cluster creation; a `replicant dev reset --temporal` drops the
  Temporal database to change it. Production's shard count (WS-10) is never exercised locally.
- No mTLS or custom Authorizer locally (parity.md P-03).

## Revisit when

- The dev server gains PostgreSQL persistence with identical visibility, or laptop memory becomes the
  binding constraint for most developers.
