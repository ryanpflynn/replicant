# ADR-003: Temporal self-hosted on EKS vs Temporal Cloud (initial recommendation)

- **Status:** Proposed. **WS-10 finalizes**, with WS-01 input.
- **Constraints:** D-01, D-17 (open source preference), D-22

## Context

Load is modest. About 1,000 jobs per day at peak × about 2,000–4,000 history events per job
(including Continue-As-New segments) gives 2–4 M events per day, an average of about 25–50 events
per second. 150 concurrent runs. 30-day waits. The availability target is 99.9% for the control
plane.

Relevant facts:

- Self-hosted Temporal supports PostgreSQL persistence. The versions Temporal has tested are 13–16
  [verified: https://docs.temporal.io/self-hosted-guide/persistence, 2026-10-24]. SQL advanced
  visibility allows only 3 custom search attributes per type (10 for Keyword) [verified:
  https://docs.temporal.io/search-attribute, 2026-10-24].
- Worker Versioning is GA as of 2026-03-30 [verified: https://temporal.io/changelog/product-area/server,
  2026-10-24]. Self-hosted needs server ≥ 1.29.1. The current release is 1.32.1 [verified: GitHub
  releases, 2026-10-24].
- Self-hosted authorization offers a pluggable ClaimMapper and Authorizer. The default is no-op,
  which allows everything [verified: https://docs.temporal.io/self-hosted-guide/security, 2026-10-24].
- Temporal Cloud charges by action, from $50 per million actions down to $25 at volume, with plan
  minimums (Business: the greater of $500 per month or 10% of usage). It supports Worker
  Versioning, Updates, Nexus, API keys, and mTLS [verified: https://docs.temporal.io/cloud/pricing,
  https://docs.temporal.io/cloud/api-keys, 2026-10-24].

## Options

1. **Self-hosted on EKS** (Helm), with persistence on a dedicated PostgreSQL 16 instance and SQL
   advanced visibility.
2. **Self-hosted** with Cassandra persistence and OpenSearch visibility.
3. **Temporal Cloud.**

## Decision (initial)

Option 1. Reasons:
- The shared context prefers open source.
- The custom Authorizer is needed for least-privilege notifier credentials
  ([ADR-002](002-runner-completion-callback-vs-poll.md)).
- Payloads stay inside the AWS account, with the codec server under Replicant's control.
- The scale is easily within a PostgreSQL-backed cluster.
- The search attribute budget fits SQL visibility with headroom ([ids-and-naming.md](../ids-and-naming.md) §4.1).

Option 2 only if WS-10 finds PostgreSQL persistence inadequate, which is not expected at this
scale. Option 3 remains a viable fallback if the team lacks Temporal operations capacity. At an
estimated 15–30 M actions per month (WS-01 to compute), Cloud costs about $750–$1,500 per month plus
storage. It is cheap enough that operational capacity, not cost, should decide.

## Consequences

- Replicant runs Temporal upgrades, `temporal-sql-tool` schema migrations, shard sizing (fixed at
  cluster creation; WS-10 chooses with headroom), multi-AZ, and backups.
- The Temporal PostgreSQL instance stays on 16 until Temporal tests 17. The Replicant database runs
  17 or later, so the two instances diverge in version.

## Revisit when

- More than 10 Keyword or 3 per-type search attributes are needed. Then move visibility to
  OpenSearch.
- Availability target rises to 99.99%, or there are fewer than two engineers able to operate
  Temporal.
- Sustained volume exceeds about 500 events per second.
