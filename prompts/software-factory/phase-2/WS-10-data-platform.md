# WS-10: Data Platform (Postgres, Object Storage, Temporal Persistence, Recovery)

**Output directory:** `spec/10-data-platform/`
**Constraints most relevant:** D-08, D-09, D-22, D-01

## Purpose

Design the stores that hold durable state and how they are operated: the factory Postgres, Temporal
persistence, Langfuse's dependencies, object storage, and backup and disaster recovery across all of
them.

## Required outputs

### 1. Postgres topology (`postgres.md`)

- Instances: factory, Temporal, Langfuse (separate). Hosting decision with ADR: Amazon RDS / Aurora
  PostgreSQL vs CloudNativePG on EKS, considering operations, cost, extension support (`pgvector`
  version), failover, and local parity.
- Factory database: schemas per `postgres-ownership.md`, connection pooling (PgBouncer or RDS Proxy),
  roles and least-privilege grants per service, RLS for tenancy, partitioning for high-volume tables
  (audit, usage telemetry), vacuum and index maintenance for `pgvector`, all applied and maintained
  without anyone connecting to the database (D-26).

### 1a. Schema management and migrations (`schema-management.md`)

Implements D-26 in full:

- The shared data-access package: layout, SQLAlchemy 2.0 model conventions, naming conventions
  (`MetaData(naming_convention=...)` so constraint names are stable for Alembic), session and
  transaction patterns for async FastAPI and for Temporal activities, RLS session variables for
  tenancy, and how each schema's models are owned by one service per `postgres-ownership.md`.
- Alembic setup: one migration history per database, branch strategy if schemas evolve
  independently (or a single linear history; decide), autogenerate limits that need hand-written
  operations (pgvector indexes, RLS policies, partitions, `CREATE INDEX CONCURRENTLY` outside a
  transaction).
- CI pipeline: render SQL, lint (squawk or equivalent), upgrade/downgrade test, drift check
  between models and migrated schema, and a check that the spec DDL matches.
- Runtime: the migration Job (image, command, advisory lock, timeout, failure handling and
  alerting), how deployments wait on it, startup revision check in services, and the expand →
  migrate → contract procedure with a worked example (renaming a column on a hot table).
- Admin surface: migration status in the control-plane admin area and as a metric; data
  migrations; the `factory` CLI commands that replace any task someone would otherwise do in `psql`.
- Break-glass access: IAM database authentication, approval, time limit, session audit, runbook.
- Temporal and Langfuse schema upgrades with their own tooling, automated in the same deploy flow.
- Consolidated DDL index: collect table DDL from WS-03, WS-04, WS-06, WS-07 into one reviewed
  schema reference, reporting conflicts to the owning workstreams.

### 2. Temporal persistence and hosting (`temporal-persistence.md`)

Final ADR on self-hosted Temporal (with WS-01's input) vs Temporal Cloud. If self-hosted: persistence
on Postgres (verify supported versions and scale limits) vs Cassandra, visibility store (Postgres
advanced visibility vs OpenSearch/Elasticsearch), history shard count (fixed at cluster creation:
choose with headroom), sizing, upgrades, and multi-AZ.

### 3. Object storage (`object-storage.md`)

Implements `artifact-layout.md`: buckets, prefix-scoped IAM policies, SSE-KMS with per-tenant
encryption context, lifecycle rules per artifact type (real S3 lifecycle JSON), Object Lock for
audit artifacts if required, presigned URL policy for UI downloads, MinIO configuration for local
parity.

### 4. Backup, recovery, and DR (`recovery.md`)

RPO/RTO per store; backups (PITR for Postgres, S3 versioning/replication, Temporal persistence
backups); recovery runbooks for: loss of the factory DB, loss of Temporal persistence, region loss,
accidental memory deletion, and corrupted registry publish. What happens to in-flight jobs in each
case, and how state stores are reconciled after restore (for example AgentRuns that completed while
Temporal was down).

### 5. Data lifecycle (`lifecycle.md`)

Retention per data type across all stores, deletion requests (a tenant offboards; a secret must be
purged), and how deletions propagate to derived data (memory snapshots, Langfuse, Loki).

## Must answer

- After restoring the factory DB to 15 minutes ago, what is inconsistent and how is it repaired?
- How many Temporal history shards, and why?
- What is the monthly storage cost estimate per store at the assumed scale?

## Acceptance criteria

- Hosting decisions have ADRs with verified facts.
- Real lifecycle JSON, IAM policy examples, and migration rules.
- Every store has an RPO/RTO and a tested-recovery procedure outline.
