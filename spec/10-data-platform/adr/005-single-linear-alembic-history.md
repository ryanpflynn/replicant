# ADR-WS10-005: One linear Alembic history for database `replicant`

- **Status:** Accepted (WS-10, 2026-10-24)
- **Constraints:** D-26, C-PG rule 7 ("WS-10 decides whether to use one linear history or per-schema branches")

## Context

Sixteen schemas, about eight owning workstreams or teams, one database, one migrate Job.

## Options

1. **Per-schema branches** (Alembic `branch_labels`, multiple heads, `depends_on` for cross-schema
   ordering).
2. **One linear history (chosen).**

| Concern | Branches | Linear |
|---|---|---|
| Startup revision check (C-PG rule 8) | `ops.alembic_version` holds several rows. The "built-against revision" becomes a set, and the compatibility rule becomes a lattice | One row, one walk down `down_revision` (replicant_db/revision.py) |
| Cross-schema ordering (grants on a new schema, `audit.emit` used by every writer, RLS helpers in `ops`) | Explicit `depends_on`, easy to forget | Implicit: order is the history |
| Concurrent PRs from different teams | Independent heads merge freely | Two PRs on the same parent conflict. CI rejects multiple heads (`alembic heads` must print one line), and the later PR re-parents with `alembic merge` or a rebase of `down_revision`. This is an explicit, reviewed step |
| Contract gate and expand/contract tracking | Per-branch | Single ordered log (`ops.migration_log`) |
| Downgrade in CI | Branch-aware | Straight line |

## Decision

One linear history. The revision ID is `<yyyymmddhhmm>_<schema>`, which sorts by time and fits in
32 characters. Every revision declares `replicant_schema`, `replicant_compat`, and (for contract
revisions) `replicant_requires_reader_revision`. CI enforces:

1. Exactly one head.
2. The revision touches only `replicant_schema` (lint of the rendered SQL: every DDL target is
   qualified with that schema, or `multi` for grants/roles revisions owned by WS-10).
3. CODEOWNERS: `migrations/versions/*_<schema>.py` is owned by that schema's DDL owner
   (C-PG §2), and `*_multi.py` and `*_ops.py` by WS-10.

## Consequences

- Teams occasionally rebase `down_revision`. A bot comment on the PR shows the exact command.
- A slow revision blocks every later one in the queue. Large data migrations therefore never run
  inline. They are batched data revisions (schema-management.md §8) or admin API operations.

## Revisit when

- More than about 30 migrations per week, or teams routinely blocked by head conflicts.
