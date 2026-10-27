# ADR-06-007: Postgres `evals` is the system of record; Langfuse is a derived export

- **Status:** Accepted (WS-06, 2026-10-24)
- **Constraints:** D-09, D-17, C-STATE §1.4 ("Eval results … PG[evals]; Langfuse scores are a derived export")

## Context

D-17 names Langfuse for datasets and scores. C-STATE already makes Postgres the owner of eval results. The open
design choice is how much the harness depends on Langfuse.

Langfuse v4 facts that shape the answer: experiments assume no repetitions per dataset item; there is no public
REST endpoint to create experiment runs, and the legacy `POST /api/public/dataset-run-items` is deprecated
[verified: https://langfuse.com/docs/evaluation/experiments/data-model, 2026-10-24]; experiments can be ingested
through OpenTelemetry attributes [verified: https://langfuse.com/integrations/native/opentelemetry/experiments.md,
2026-10-24]; dataset items are versioned by timestamp [verified:
https://langfuse.com/docs/evaluation/dataset-runs/datasets, 2026-10-24]; score writes overwrite only when ID,
name, and timestamp date match [verified: https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk,
2026-10-24].

## Options

1. **Langfuse as the eval store**, with Postgres caching it. Promotion decisions would depend on an observability
   tool's availability and data model (no repetitions, deprecated endpoints).
2. **Postgres only.** Loses trace-level drill-down from a score to the generation that produced it.
3. **Postgres as record, Langfuse as export (chosen).**

## Decision

Golden-task membership, cases, scores, comparisons, calibration, and pipeline projections are in `evals`. Langfuse
gets task statements as dataset items (versioned), one experiment per arm × rep through OTel attributes on each
job trace, and scores with deterministic IDs. Export failures never affect verdicts.

## Consequences

- Promotion works with Langfuse down.
- Two stores must be kept consistent for display. The exporter is idempotent and retried for 24 hours.
- Hidden tests and reference patches never enter Langfuse.

## Revisit when

- Langfuse adds first-class repetitions and a stable REST API for experiment runs.
