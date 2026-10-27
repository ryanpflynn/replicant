# ADR-WS10-008: Region DR is "warm data, pilot-light compute"

- **Status:** Accepted (WS-10, 2026-10-24)
- **Constraints:** shared context §2 (99.9% control plane; 30-day waits), D-22

## Context

99.9% availability (43 min/month) is an in-region target. Multi-AZ Aurora, multi-AZ EKS, and S3
cover it. Region loss is rarer but would strand up to about 4,000 open jobs, some holding 30 days of
human context. The question is how much of Replicant to keep running in a second region.

## Options

1. **Backups only.** Cross-Region snapshot copies and S3 CRR. RPO up to the snapshot interval
   (hours). RTO about a day.
2. **Warm data, pilot-light compute (chosen).** Aurora Global Database **headless** secondaries for
   `replicant` and `temporal` (RPO seconds; no instance cost). S3 CRR with RTC for every bucket class
   except `session`. EKS clusters, Temporal, and services in us-west-2 are defined in IaC and Argo CD
   but scaled to zero (the clusters exist with system node pools only).
3. **Active/passive with warm compute.** Option 2 plus running Temporal and workers in us-west-2.
   Doubles compute for an event Temporal's own XDC (experimental) would handle better.

| | Option 1 | **Option 2** | Option 3 |
|---|---|---|---|
| RPO Replicant / temporal | ≤ 24 h / ≤ 24 h | **≤ 1 min / ≤ 1 min** (Global DB lag "typically under a second") | same as 2 |
| RPO artifacts | ≤ 15 min (CRR+RTC) | ≤ 15 min (RTC SLA covers 99.9% of objects [verified: https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication-time-control.html, 2026-10-24]) | same |
| RTO | ~24 h | **≤ 4 h** (add Aurora instances, scale node pools, sync Argo) | ≤ 1 h |
| Added cost/month | ~$150 | **~$700** (headless storage + replicated I/O + S3 replicas, capacity-and-cost.md) | ~$5k+ |

## Decision

Option 2. The full runbook is in [recovery.md §7.3](../recovery.md).

## Consequences

- `session` (raw CLI state, job+7 d) is not replicated. Jobs failed over mid-run lose session resume
  and fall back to handoff summaries (WS-04 already supports this path).
- In-flight sandbox runs in the failed region are lost. The workflow sees them as `NodeLost`-like
  timeouts (`OrchestratorDeadlineExceeded`) and re-runs nodes with ordinal+1 (C-CRD §4).
- The DR region needs its own KMS keys: one multi-Region replica per tenant key, plus DR platform
  keys. Offboarding must delete those too (lifecycle.md §4).
- A failback drill runs twice a year in staging (recovery.md §9).

## Revisit when

- The availability target rises to 99.99% (then Option 3, or Temporal Cloud multi-region).
