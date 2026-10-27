# ADR-WS10-001: Host all three Postgres instances on Amazon Aurora PostgreSQL

- **Status:** Accepted (WS-10, 2026-10-24)
- **Constraints:** D-09 (separate instances; hosting is WS-10's open decision), D-22 (AWS), D-26
  (break-glass via IAM database authentication), D-20 (local parity)
- **Contracts:** C-PG §1

## Context

Three instances (C-PG §1): `replicant` (PostgreSQL 17 or 18, pgvector ≥ 0.8), `temporal` (PostgreSQL 16,
the newest version Temporal tests), and `langfuse` (Langfuse v4 needs PostgreSQL ≥ 15). The load is
modest (postgres.md §8). Data loss tolerance is low for `replicant` and `temporal` (recovery.md §1), and
the control plane targets 99.9%.

Verified facts that decide the comparison:

| Fact | Source |
|---|---|
| Aurora PostgreSQL 16.15, 17.11 and 18.6 are current. Every Aurora 17.x ships pgvector ≥ 0.8.0, and 17.10+ ships 0.8.2. RDS for PostgreSQL ships the same pgvector versions per minor | [verified: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraPostgreSQLReleaseNotes/AuroraPostgreSQL.Extensions.html and https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html, 2026-10-24] |
| Aurora failover: "typically restored in less than 60 seconds, and often less than 30 seconds". Storage is six copies across three AZs | [verified: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Concepts.AuroraHighAvailability.html, 2026-10-24] |
| RDS Multi-AZ DB instance failover is "typically 60–120 seconds". The Multi-AZ DB *cluster* fails over in under 35 s but supports only local-NVMe `d` classes (no r7g/r8g), and AWS Backup PITR and cross-Region automated backups do not support it | [verified: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.MultiAZ.Failover.html, …/multi-az-db-clusters-concepts.html, https://docs.aws.amazon.com/aws-backup/latest/devguide/point-in-time-recovery.html, 2026-10-24] |
| Aurora Global Database replicates "typically under a second"; managed failover takes "a few minutes"; a **headless** secondary (storage only, no instances) is supported for PostgreSQL via CLI/API | [verified: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-global-database-disaster-recovery.html and …/aurora-global-database-attach.console.headless.html, 2026-10-24] |
| Aurora fast clone (copy-on-write, up to 15 per source) | [verified: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Managing.Clone.html, 2026-10-24] |
| Database Activity Streams: Aurora PostgreSQL 16.1+ / 17.4+ (not 18, not Serverless). **Not available on RDS for PostgreSQL** | [verified: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Concepts.Aurora_Fea_Regions_DB-eng.Feature.DBActivityStreams.html and https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.RDS_Fea_Regions_DB-eng.Feature.DBActivityStreams.html, 2026-10-24] |
| IAM DB auth works on RDS and Aurora (15-minute tokens, checked at connect only; CloudTrail does not log it). CloudNativePG has no IAM equivalent (pg_hba: SCRAM, certificates, LDAP) | [verified: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.html; CloudNativePG docs/src/postgresql_conf.md, 2026-10-24] |
| CloudNativePG 1.30.1 (2026-09-23) supports PostgreSQL 14–18 on Kubernetes 1.34–1.36. Native `barmanObjectStore` is deprecated and removed in 1.32.0 in favor of the Barman Cloud Plugin. Image-volume extensions need PostgreSQL 18 and Kubernetes 1.35+ | [verified: https://github.com/cloudnative-pg/cloudnative-pg (supported_releases.md, backup.md, imagevolume_extensions.md), 2026-10-24] |
| Aurora PostgreSQL on-demand us-east-1: db.r8g.xlarge $0.552/h, db.r8g.2xlarge $1.104/h (Standard); storage $0.10/GB-month plus $0.20 per million I/O, or I/O-Optimized at $0.225/GB-month with no I/O charge; switch to I/O-Optimized "when your I/O spending is 25% or more" | [verified: AWS Price List us-east-1 2026-10-20; https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Overview.StorageReliability.html, 2026-10-24] |

## Options

1. **Amazon Aurora PostgreSQL** (provisioned writer plus one reader in a second AZ; Global Database
   headless secondary for DR).
2. **Amazon RDS for PostgreSQL**, Multi-AZ DB instance.
3. **CloudNativePG on EKS** (three clusters, Barman Cloud Plugin to S3, Kata/NVMe or gp3 storage).

| Criterion | Aurora | RDS Multi-AZ instance | CloudNativePG |
|---|---|---|---|
| Failover (99.9% control plane = 43 min/month budget) | < 60 s, often < 30 s | 60–120 s | Operator-driven, typically 10–60 s; depends on our node and storage health |
| Region DR | Global Database, RPO seconds, headless secondary | Cross-Region automated backups (RPO ≈ minutes to backup interval) or a cross-Region read replica | Replica cluster in a second EKS cluster; we operate it |
| pgvector 0.8 | yes (0.8.2 on 17.10+) | yes | yes (standard images) |
| IAM DB auth (D-26 break-glass) | yes | yes | **no** (would need Vault dynamic credentials or a proxy) |
| Session audit for break-glass | pgaudit **and** Database Activity Streams | pgaudit only | pgaudit only |
| Restore rehearsal / migration rehearsal on production-shaped data without connecting to production | fast clone in minutes | snapshot restore (slow for large DBs) | backup restore to a new cluster |
| Operational load | lowest | low | highest: operator upgrades, PVCs, Karpenter interaction, backups, PITR drills are ours |
| Cost at this scale (postgres.md §8) | ≈ $3.5k/month for all three including DR | ≈ 10–20% less for instances; no headless DR equivalent | instance cost lower; engineering cost far higher |
| Local parity | stock PostgreSQL 16/17 + pgvector images | same | same (could run in kind) |

## Decision

Option 1 for all three instances.

| Instance | Engine | Topology (prod) | Storage configuration |
|---|---|---|---|
| `replicant` | Aurora PostgreSQL **17** (17.11 today; pgvector 0.8.2). Not 18: Database Activity Streams is not yet available on Aurora PG 18, and break-glass audit wants it | writer `db.r8g.2xlarge` + reader `db.r8g.2xlarge` (other AZ, failover tier 0) | Standard (I/O well under 25% of spend; re-evaluate monthly) |
| `temporal` | Aurora PostgreSQL **16** (16.15). Temporal tests community PostgreSQL 13–16 only | writer `db.r8g.xlarge` + reader `db.r8g.xlarge` | **I/O-Optimized** (Temporal is write-heavy; I/O is projected above 25% of spend) |
| `langfuse` | Aurora PostgreSQL **16**, provisioned `db.r8g.large` (revised wave 1, R1-235: WS-08 settled on 16; provisioned because the instance also holds Grafana's `grafana` database and its steady connection load makes Serverless v2 cost more) | writer + reader | Standard |

Region DR: Aurora Global Database with a **headless** secondary in us-west-2 for `replicant` and
`temporal`. `langfuse` uses cross-Region snapshot copies (its data is derived and lower value)
(ADR-WS10-008).

## Consequences

- **Temporal on Aurora is outside Temporal's tested matrix.** Aurora PostgreSQL is wire- and
  SQL-compatible, but Temporal's docs neither claim nor disclaim Aurora support `[unverified:
  Temporal on Aurora PostgreSQL compatibility; no Temporal statement either way]`. Mitigation, which is
  load-bearing: the staging soak (temporal-persistence.md §8) runs Temporal's persistence test suite
  plus a 72-hour load replay against Aurora before production. **Fallback:** RDS for PostgreSQL 16
  Multi-AZ DB instance for `temporal` only. That costs 60–120 s failovers and uses cross-Region
  automated backups for DR (RPO about 5 min instead of seconds). Switching later needs a Temporal
  maintenance window and a dump/restore, because the shard count and data move with the database.
- The Aurora PostgreSQL minor version lags community minors by weeks. Security minors are applied in
  the maintenance window (Sunday 06:00–07:00 UTC).
- Local development uses stock `postgres:16` and a pgvector image pinned to **the production pgvector
  minor** (0.8.2), not upstream latest (0.8.7 today), so a query that relies on newer pgvector
  behavior fails locally first (config/compose/postgres-init/ and WS-11's compose stack, R1V-182).
- Database Activity Streams and IAM auth both tie break-glass to AWS. That is accepted under D-22.

## Revisit when

- Temporal publishes Aurora support, or adds PostgreSQL 17 to its tested set (then evaluate moving
  `temporal` to 17 so that all instances share one major).
- Aurora PostgreSQL 18 gains Database Activity Streams (then consider moving `replicant` to 18).
- Replicant DB spend exceeds about $10k/month, or a non-AWS deployment target appears. Then
  re-evaluate CloudNativePG.
