# ADR-WS10-002: Temporal self-hosted on EKS, PostgreSQL persistence and SQL visibility (finalizes ADR-003)

- **Status:** Accepted (WS-10, 2026-10-24). Finalizes contract
  [ADR-003](../../00-contracts/adr/003-temporal-self-hosted-vs-cloud.md). WS-01 input: the custom
  Authorizer requirement (ADR-002 contract) and the search-attribute set (C-IDS §4.1).
- **Constraints:** D-01, D-17 (open-source preference), D-22

## Context

ADR-003 proposed self-hosting with PostgreSQL 16 persistence and SQL advanced visibility. WS-10
must confirm the hosting choice, persistence engine, and visibility store with verified facts.

| Fact | Source |
|---|---|
| Server v1.32.1 (2026-10-08) is current. Upgrades must go "sequentially, one minor version at a time", and the schema must be upgraded before the server | [verified: https://github.com/temporalio/temporal/releases; https://docs.temporal.io/self-hosted-guide/upgrade-server, 2026-10-24] |
| Tested persistence: "PostgreSQL 13.18, 14.15, 15.10 and 16.6". Plugins `postgres12`, `postgres12_pgx` | [verified: https://docs.temporal.io/temporal-service/persistence; temporal v1.32.1 sqlplugin/postgresql/plugin.go, 2026-10-24] |
| SQL visibility: PostgreSQL v12+ with server v1.20+. Custom search attributes per namespace: Bool 3, Datetime 3, Double 3, Int 3, Keyword 10, KeywordList 3, Text 3. Elasticsearch/OpenSearch "recommended for any setup that spawns more than a few Workflow Executions". On high volume, `executions_visibility` indexes bloat; Temporal recommends periodic `REINDEX TABLE CONCURRENTLY` and `autovacuum_vacuum_scale_factor = 0.05` | [verified: https://docs.temporal.io/self-hosted-guide/visibility, …/visibility/postgresql, https://docs.temporal.io/search-attribute, 2026-10-24] |
| Archival and Multi-Cluster Replication are both **experimental** | [verified: https://docs.temporal.io/self-hosted-guide/archival, …/multi-cluster-replication, 2026-10-24] |
| Server ≥ 1.31 supports `passwordCommand` for SQL stores (fresh credential per new connection; intended for expiring IAM tokens) | [verified source: https://raw.githubusercontent.com/temporalio/temporal/v1.32.1/common/config/config.go, 2026-10-24; not yet on docs.temporal.io] |
| Temporal Cloud: actions $50 per million (first 5 M), tapering to $25; active storage $0.042/GBh, retained $0.00105/GBh; Business minimum $500/month or 10%. SLA 99.9% single region, 99.99% replicated. Data-plane authorization is namespace-level roles only (Read, Write, Namespace Admin). No custom Authorizer | [verified: https://docs.temporal.io/cloud/pricing, https://temporal.io/sla, https://docs.temporal.io/cloud/manage-access/roles-and-permissions, 2026-10-24] |

## Options

1. **Self-hosted on EKS, PostgreSQL persistence (Aurora PG 16), SQL visibility in the same instance**
   (separate database `temporal_visibility`).
2. Self-hosted with Cassandra persistence and OpenSearch visibility.
3. Self-hosted with PostgreSQL persistence and OpenSearch visibility.
4. Temporal Cloud.

## Decision

**Option 1.**

- **Hosting: self-hosted.** The run-notifier's Signal-only credential (contract ADR-002) needs a custom
  Authorizer, and Temporal Cloud cannot scope a credential below namespace `Write`. Payloads and the
  codec stay in the account. Open source is preferred (D-17). Cloud is cheap at this volume (about
  $1–1.5k/month, contract ADR-003), so cost does not decide. Isolation and authorization do.
- **Persistence: PostgreSQL.** Cassandra adds a second distributed database to operate, for load that
  stays below 1% of what PostgreSQL persistence handles comfortably (temporal-persistence.md §5).
- **Visibility: SQL.** Replicant uses 8 of 10 Keyword attributes and fewer than the limit in every
  other type (C-IDS §4.1). Visibility queries come from the reconciler (every 2 min) and the releases
  screen, not from page loads (ADR-004). Bloat is handled by maintenance (temporal-persistence.md §7).
  The phrase "recommended for any setup that spawns more than a few Workflow Executions" is generic
  guidance. At about 1,000 workflows/day and a 30-day retention, `executions_visibility` holds about
  40k rows. That is tiny.
- No archival and no XDC (both experimental). The long-term record is the Replicant projection plus
  audit (C-STATE §1.1). Region DR is database-level (ADR-WS10-008).

## Consequences

- WS-10 operates upgrades (one minor at a time, schema first; temporal-persistence.md §6), shard
  sizing (fixed at 512, ADR-WS10-003), backups, and the custom server image that carries the IAM
  token helper.
- Visibility and persistence share one Aurora instance. A visibility scan cannot starve history
  writes: per-host `persistenceMaxQPS` caps and a separate pool (`maxConns: 10`) for the visibility
  store bound it.

## Revisit when (thresholds)

- More than 10 Keyword or 3 attributes of another type are needed, or visibility list p99 exceeds
  1 s, or `executions_visibility` exceeds 5 M rows. Then move visibility to OpenSearch 2+ (server
  ≥ 1.30.1).
- Sustained history load exceeds about 2,000 state transitions/s, or the Temporal Aurora writer's
  CPU exceeds 60% p95. Then scale the instance first. Evaluate Cassandra only beyond `db.r8g.8xlarge`.
- Fewer than two engineers can operate Temporal, or availability must reach 99.99%. Then Temporal
  Cloud with multi-region replication. This requires re-deciding ADR-002's notifier credential model
  (namespace Write role plus hint-only semantics).
