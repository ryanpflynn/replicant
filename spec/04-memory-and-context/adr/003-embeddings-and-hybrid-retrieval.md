# ADR-04-003: Embeddings and hybrid retrieval over memory entries only

- **Status:** Accepted (WS-04, 2026-10-24)
- **Constraints:** D-14 (pgvector over memory entries only), D-13 (no repository embeddings), D-09

## Context

Memory corpora are small per scope (hundreds of push entries per repo) and larger for episodes
(about 110k per year). Retrieval must be deterministic for push (reproducibility and testability)
and fast for pull. pgvector 0.8 offers HNSW with iterative scans; HNSW indexes `halfvec` up to 4,000
dimensions [verified: https://github.com/pgvector/pgvector/blob/master/README.md, 2026-10-24].

## Options

1. ANN (HNSW) for everything. Approximate and filter-sensitive (filters apply after the index scan).
2. Exact only. Fine for push; slow for cross-repo episodes at scale.
3. **Exact hybrid (FTS + cosine, RRF) over the scope-filtered set for push and `memory_search`; HNSW
   with iterative scan plus exact re-rank for episodes (chosen).**
4. No vectors (FTS only). Misses paraphrases ("floats drift" vs "use decimal").

## Decision

- `halfvec` without typmod in `memory.entry_embeddings`, one partial expression HNSW index per
  embedding space (`m=16`, `ef_construction=128`), cosine. Default space `vc4-1024`
  (`voyage-code-4`, 1,024 dims) via the AI gateway; fallback `te3l-1024`.
- Dedupe and conflict thresholds are stored per space and calibrated, so a model change cannot
  silently shift merge behavior.
- RRF with k = 60 combines FTS and vector ranks, then specificity, confidence, recency, and outcome
  (retrieval-injection.md §2).

## Consequences

- Push selection is reproducible and cheap; pull scales.
- Re-embedding is a data operation with a shadow eval, not a schema change.

## Revisit when

- Per-scope active sets exceed about 5,000 rows (then index-assisted filtering for push), or the
  gateway cannot serve an embedding model.

## Amendment (wave-1 fix round, 2026-10-24, R2-051)

At 100k+ episodes per tenant, one org-wide HNSW graph per embedding space makes tenant-filtered
queries scan mostly other tenants' points, and iterative scans can stop at `max_scan_tuples` with
fewer than `k` results. Changes:

- `memory.entry_embeddings` is `PARTITION BY LIST (tenant_id)`; the parent's partial HNSW index is
  created on each tenant partition (verified on PostgreSQL 18 + pgvector 0.8.1). Partitions are
  created at tenant onboarding by `memory.ensure_tenant_partitions()` (migrate Job).
- Exact search when the filtered estimate is ≤ 20,000 rows; HNSW above, with an exact retry on
  underfill (counted, never silent).
- Embeddings of non-current, superseded, rejected, expired, and purged versions are deleted nightly.
- Nightly per-tenant recall probe (HNSW vs exact, recall@10) with an alert below 0.90.
- Snapshot items are monthly partitions dropped after the 2-year snapshot lifetime (C-IDS).

Revisit when a single tenant's active vectors exceed about 2 million, or recall falls below target
with the current parameters (then raise `m`/`ef_construction` per partition or shard by repo group).
