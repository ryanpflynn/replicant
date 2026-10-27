# ADR-08-001: Mimir (classic architecture) rather than Prometheus for metrics

- **Status:** Accepted (WS-08, 2026-10-24)
- **Constraints:** D-17 ("Prometheus or Mimir"), D-22, shared context section 2 (99.9% control plane)

## Context

Metrics come from two EKS clusters per environment in two AWS accounts (CCR-09-07): core (control plane,
workers, Temporal, Langfuse, LGTM) and exec (Runner, controller, sandboxes). Estimated load at the assumed scale
(platform.md section 4.4): about 0.8 to 1 M active series, about 27 k samples per second at a 30 s interval,
about 150 k new sandbox pod series per day (churn), and a need for 13 months of history for capacity and cost
trends, SLO windows of 30 days, and HA alert evaluation.

Facts checked: Mimir 3.x supports two architectures, ingest storage (Kafka-based) and classic, and monolithic
mode (`-target=all`) "can be horizontally scaled by deploying multiple Mimir binaries" for HA, while
microservices mode is recommended for production [verified:
https://grafana.com/docs/mimir/latest/references/architecture/deployment-modes/, 2026-10-24]. Current releases:
Mimir 3.2.3, Prometheus 3.15.1 [verified: GitHub releases API, 2026-10-24].

## Options

1. **Prometheus HA pair per cluster** with local TSDB. Each pair fits 1 M series. But: two independent pairs need
   a global query layer for one dashboard across clusters (Prometheus federation or Thanos, which D-17 does not
   name); 13 months on gp3 per replica (about 1.7 TB each, single-AZ EBS); deduplication of the HA pair falls to
   Grafana or Alertmanager; no tenant limits.
2. **Mimir microservices, classic architecture** (no Kafka): object-storage-backed blocks, replication factor 3
   across AZs, global view across both clusters by remote write, per-tenant limits, HA ruler and Alertmanager.
3. **Mimir with ingest storage**: requires operating Kafka (or a compatible system) for one metrics backend.
4. **Mimir monolithic, 3 replicas**: same storage model, fewer moving parts, not the documented production mode.

## Decision

Option 2 in production and staging, deployed with the `mimir-distributed` Helm chart with
`ingest_storage.enabled: false` (config/mimir/mimir.yaml, validated by starting Mimir 3.2.3 with it). Option 4 is
the documented fallback if a chart version drops classic-mode support `[unverified: whether mimir-distributed
6.x still renders classic mode by default; fallback is option 4, which is verified]`. Local mode keeps
Prometheus (WS-11 compose), which accepts the same remote-write stream; the rule files load into both.

Alloy (both clusters) scrapes and remote-writes; nothing scrapes across the account boundary. Exec Alloy reaches
the Mimir distributor through a PrivateLink endpoint service (platform.md section 1.2).

## Consequences

- One `X-Scope-OrgID` tenant `replicant` (ADR-08-002). Retention 400 days (`compactor_blocks_retention_period`).
- Mimir is the source for the Mimir ruler and Alertmanager; Grafana evaluates no alert rules.
- About 30 vCPU and 120 GiB of requests for the Mimir components (platform.md section 4.5), more than a
  Prometheus pair (about 8 vCPU) but with HA, S3 durability, and a single global view.

## Revisit when

- Active series exceed 5 M or ingestion exceeds 300 k samples/s: move to ingest storage (Kafka) per Mimir's
  preferred architecture.
- Grafana announces classic architecture deprecation in Mimir.
- Replicant collapses to one cluster and retention needs drop to 30 days: a Prometheus pair would suffice.
