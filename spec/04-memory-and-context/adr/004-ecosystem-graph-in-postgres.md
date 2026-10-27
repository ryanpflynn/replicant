# ADR-04-004: Store the ecosystem graph in Postgres, not a graph database

- **Status:** Accepted (WS-04, 2026-10-24); resolves the C-PG open question
- **Constraints:** D-13, D-09, D-26 (one data-access layer and migration tool)

## Context

The graph has about 1,500 repos, a few thousand services, and tens of thousands of edges. Queries
are neighborhood and blast-radius walks of depth ≤ 4. A graph database would be a new stateful
system with its own access layer, migrations, backups, and authorization, outside D-26's single
SQLAlchemy/Alembic path. Apache AGE is not on the RDS extension list `[unverified: AGE availability
on RDS/Aurora PostgreSQL 17/18; not load-bearing]`.

## Options

1. Neo4j or another graph DB. Expressive; new operational surface; violates the one-data-layer
   intent of D-23/D-26.
2. Postgres with AGE. Uncertain managed-service support.
3. **Plain Postgres adjacency tables with recursive CTEs (chosen).**

## Decision

`ecosystem.edges` with evidence per source; `ecosystem.consumers_of()` walks consumers with a
cycle-safe recursive CTE (tested). A monotonic `graph_seq` versions the graph for briefings.

## Consequences

- Depth-3 walks over tens of thousands of edges are millisecond queries with `edges_to`.
- No new store, backup, or authorization model.

## Revisit when

- Edges exceed about 5 million, or queries need path patterns beyond bounded-depth walks.
