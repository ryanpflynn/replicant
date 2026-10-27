# ADR-WS10-003: 512 Temporal history shards

- **Status:** Accepted (WS-10, 2026-10-24)
- **Constraints:** D-01; shared context §2 (scale)

## Context

`numHistoryShards` "cannot be changed after the initial deployment". "You can't change this value
after the first run. … Set it high enough for your expected peak load." Changing it later "requires a
rebuild and a migration to the new Temporal Service" [verified:
https://docs.temporal.io/references/service-configuration and
https://docs.temporal.io/self-hosted-guide/production-checklist, 2026-10-24].

Temporal's guidance: each shard is a unit of concurrent database operations. Clusters have run with
"1 to 128K History Shards, with each Shard responsible for tens of thousands of Workflow Executions".
Start at "1 History Service process for every 500 History Shards" [verified:
https://docs.temporal.io/temporal-service/temporal-server#history-shard, 2026-10-24]. "small production
clusters use 512 shards … it is rare for even large Temporal clusters to go beyond 4,096 shards. An
overly large shard count wastes CPU and Memory on History pods" [verified:
https://temporal.io/blog/scaling-temporal-the-basics, 2026-10-24].

Replicant load (C-ARCH, contract ADR-003, WS-01 budgets): 1,000 jobs/day peak × 2,000–4,000 history
events gives 2–4 M events/day. That is a 25–50 events/s average, and an estimated burst of 10× during
morning peaks (≈ 500 events/s). Open workflows: up to about 4,000 (30-day human waits). Up to
about 40k executions are retained for 30 days.

## Options

| Shards | Executions per shard at 10× growth (≈ 400k retained) | Per-shard burst load at 10× growth (5,000 events/s) | Idle overhead (SQL task polling, memory on history pods) | Fits Temporal guidance |
|---|---|---|---|---|
| 128 | ≈ 3,100 | ≈ 39 events/s | lowest | below "small production" |
| **512** | ≈ 780 | ≈ 10 events/s | low: 512 shard controllers over 3 history pods | "small production clusters use 512" |
| 2,048 | ≈ 200 | ≈ 2.4 events/s | 4× the 512 overhead; more idle DB polling per second | medium/large |
| 4,096 | ≈ 100 | ≈ 1.2 events/s | high, for no benefit at this scale | "rare even for large clusters" |

## Decision

**512 shards**, configured once in `config/temporal/values-prod.yaml` and identical in every
environment. The same count everywhere means a DB restore or clone works across environments.
Local uses 4, the Docker default, because local databases are disposable.

Why 512, with headroom:

1. **10× growth stays shallow.** At 10× today's peak (10,000 jobs/day), each shard sees about 10
   events/s in bursts and holds under 1,000 retained executions. That is far below "tens of
   thousands of Workflow Executions" per shard and well inside what a single Aurora writer serves
   per shard.
2. **The shard count is not the bottleneck at any plausible Replicant scale.** The persistence writer
   is. At 512 shards the cluster can grow to about 50,000 jobs/day (50×) before per-shard
   concurrency, rather than database capacity, matters. By then the design changes anyway: exec
   cluster sharding begins at about 1,000 concurrent runs (C-ARCH §4).
3. **Overhead is small.** With 3 history pods, each owns about 170 shards. That is well under the
   1-process-per-500-shards starting ratio, and it leaves room to scale history pods to 6 without
   re-sharding.

## Consequences

- History pods: 3 (AZ spread). Scale to at most 6 before considering the database.
- If the Replicant ever needs more shards, the path is a new Temporal cluster with the new count, plus
  migration. New jobs start on the new cluster and old ones drain; jobs live at most 30 days plus PR
  review. This is documented in recovery.md §7.2 as the same procedure as "rebuild Temporal".
- This ADR is final. The value is protected by a CI check that fails any change to
  `numHistoryShards` in the values files.

## Revisit when

- Sustained > 2,000 events/s cluster-wide, or > 40,000 open workflows. Then plan a new cluster
  (2,048 shards).
