# ADR-08-005: Loki in distributed mode; Tempo in monolithic mode (no Kafka)

- **Status:** Accepted (WS-08, 2026-10-24)
- **Constraints:** D-17 (Grafana OSS stack), shared context section 2 (scale, 99.9%)

## Context

- Loki deployment modes: monolithic suits "up to approximately 20GB per day"; Simple Scalable (SSD) "is being
  deprecated and will be removed with the Loki 4.0 release"; microservices (distributed) for larger or HA setups
  [verified: https://grafana.com/docs/loki/latest/get-started/deployment-modes/, 2026-10-24]. The Helm chart's
  `deploymentMode` takes `SingleBinary`, `SimpleScalable` (chart default), `Distributed` [verified:
  production/helm/loki/values.yaml at v3.7.9, 2026-10-24].
- Replicant log volume: about 5 GB/day steady, about 10 GB/day at the 1,000 jobs/day peak (platform.md 4.1), so
  within monolithic's range today but within a factor of 2 of its ceiling, and the volume is dominated by sandbox
  transcripts, which grow linearly with jobs.
- Tempo 3.x: monolithic mode (`-target=all`) runs without Kafka; microservices mode requires a Kafka-compatible
  queue (Apache Kafka, Redpanda, WarpStream) [verified: Tempo 3.1.1 configuration docs,
  docs/sources/tempo/configuration/_index.md, and v3.0.0 release notes, 2026-10-24]. Trace volume about
  4.4 GB/day at peak.

## Decision

- **Loki: Distributed** (`deploymentMode: Distributed`), replication factor 3 across three AZs: distributor x3,
  ingester x3 (zone-aware, WAL on gp3), querier x3, query-frontend x2, query-scheduler x2, index-gateway x2,
  compactor x1, ruler x2, gateway (nginx) x2. Reasons: SSD is a dead end; monolithic would need a migration at
  about 2x growth; logs are on the incident path for the 99.9% control plane, so HA ingestion matters.
- **Tempo: monolithic**, one replica with a 100 GiB gp3 PVC (WAL and live-store), S3 blocks, backend scheduler
  and worker in-process. Traces are diagnostic, not on the SLO path; a pod restart loses no acknowledged data
  that is in the WAL `[unverified: monolithic Tempo 3.x replays the live-store WAL on restart from the PVC;
  fallback: accept losing up to the last few seconds of spans]`. Running Kafka only for traces is not justified at
  4.4 GB/day.

## Consequences

- Loki costs about 22 vCPU and 60 GiB of requests (platform.md 4.5) against about 4 vCPU for monolithic.
- Tempo has a single point of failure for trace ingestion: during a Tempo outage, Alloy queues up to 50,000 span
  batches (`sending_queue.queue_size`), then drops; jobs are unaffected.

## Revisit when

- Loki ingestion exceeds about 1 TB/day: add bloom filters (experimental; Grafana recommends them above 75 TB per
  month) and scale queriers.
- Tempo ingestion exceeds about 50 GB/day, or trace queries need HA: move to Tempo microservices with a
  Kafka-compatible queue (shared with Mimir if Mimir has moved to ingest storage, ADR-08-001).
