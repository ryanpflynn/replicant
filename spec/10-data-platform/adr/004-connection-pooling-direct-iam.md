# ADR-WS10-004: No connection pooler in v1. Direct, budgeted, IAM-authenticated connections

- **Status:** Accepted (WS-10, 2026-10-24)
- **Constraints:** D-23, D-26 (IAM database authentication), C-PG rule 4 (transaction-scoped RLS
  variables)

## Context

The RLS protocol (C-PG rule 4; schema-management.md §4) issues `set_config('replicant.tenant_id', …, true)`
at the start of every transaction. The brief asks for PgBouncer or RDS Proxy.

| Fact | Source |
|---|---|
| RDS Proxy (PostgreSQL) pins a client connection to its database connection when the client is "Setting a parameter… Specifically, using SET and set_config commands"; also on LISTEN, session advisory locks, and named prepared statements managed with PREPARE/EXECUTE. "RDS Proxy doesn't support session pinning filters for PostgreSQL" | [verified: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-proxy-pinning.html, 2026-10-24] |
| RDS Proxy supports end-to-end IAM auth (since 2025-09) and cuts Aurora failover time "by up to 66%"; priced at $0.015 per vCPU-hour of the target | [verified: https://aws.amazon.com/about-aws/whats-new/2025/09/amazon-rds-proxy-end-to-end-iam-authentication; Aurora HA page; https://aws.amazon.com/rds/proxy/pricing/, 2026-10-24] |
| A role granted `rds_iam` can no longer log in with a password ("IAM authentication takes precedence") | [verified: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.html, 2026-10-24] |
| PgBouncer 1.26.1 is current. Protocol-level prepared statements work in transaction mode (1.21+, on by default since 1.24). It cannot validate RDS IAM tokens, so it would need password auth to the server, which conflicts with `rds_iam` on the same role | [verified: https://raw.githubusercontent.com/pgbouncer/pgbouncer/master/NEWS.md, 2026-10-24] |
| Aurora `max_connections` default `LEAST({DBInstanceClassMemory/9531392},5000)`. IAM auth needs 300–1000 MiB of extra memory on the instance | [verified: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Managing.html; IAMDBAuth page, 2026-10-24] |

## Options

1. **RDS Proxy.** Every transaction calls `set_config`, so every client connection pins for its whole
   life. The proxy multiplexes nothing and adds latency and cost. The pin problem could be avoided
   only by dropping transaction-local GUCs, which would mean giving up RLS defense in depth or
   moving tenant context into every query's predicate. Rejected.
2. **PgBouncer (transaction mode)**, as a Deployment in core. It multiplexes correctly (transaction-local
   GUCs are safe). But services would authenticate to PgBouncer with passwords, and PgBouncer to
   Aurora with passwords. That gives up IAM auth for service roles (D-26 spirit: no long-lived DB
   secrets) and adds a hop and a failure domain.
3. **No pooler (chosen).** Each pod runs a small SQLAlchemy pool. Connections go straight to the
   Aurora cluster endpoint (writer) or reader endpoint. Each pod mints an IAM token per new physical
   connection (local SigV4 signing, no API call). Hard per-role `CONNECTION LIMIT`s enforce a
   connection budget.

## Decision

Option 3. The connection budget (postgres.md §5) sums to about 710 connections at maximum HPA
scale, against `max_connections = 1000` on a `db.r8g.2xlarge` (64 GiB). Per-role limits
(ddl/10-roles-and-grants.sql) make an over-scaled deployment fail on its own role
(`too many connections for role`) instead of exhausting the instance. Long-lived exceptions:
control-plane-api keeps one LISTEN connection per pod, and the migrate Job holds one connection
with the session advisory lock.

## Consequences

- No service holds a database password in production. Only the RDS-managed master secret exists,
  used by the bootstrap Job. Langfuse is the exception: its static connection URLs need password
  roles on its own instance.
- Aurora failover reconnects take DNS TTL plus pool pre-ping, about 30–60 s, instead of the proxy's
  faster path. This fits the 99.9% budget (43 min/month). `pool_recycle=900` and `pool_pre_ping=True`
  are set in replicant-db.
- Pinned replicant-worker versions multiply connections, which is why `svc_replicant_worker` gets the
  largest limit (160). Each worker pod uses a pool of 2+2 and caches registry reads by digest.

## Revisit when (threshold)

- The connection-limit sum exceeds about 1,500, or pod counts are driven by bursty HPA, or more than
  10 replicant-worker versions are live. Then introduce PgBouncer 1.2x in transaction mode
  (`max_prepared_statements > 0`), with password-authenticated `svc_*_pool` roles that carry no
  `rds_iam`. psycopg's prepared statements work through it with libpq 17+ [verified:
  https://github.com/psycopg/psycopg/blob/master/docs/advanced/prepare.rst, 2026-10-24].
- AWS adds PostgreSQL pinning filters to RDS Proxy (for example, ignore `set_config` with
  `is_local=true`).
