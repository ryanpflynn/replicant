# ADR-06-004: Shadow mode is sampled and used only for distribution-shift risks

- **Status:** Accepted (WS-06, 2026-10-24)
- **Constraints:** brief ("shadow… decide whether affordable"), D-21 (untrusted code, tenant isolation), D-19

## Context

Shadow runs a candidate on live job inputs without publishing. It sees the production distribution (repos,
sizes, environments) that a 450-task golden set cannot fully represent. It cannot observe merges or reviews.

## Options

1. **No shadow.** Cheapest. Distribution-shift breakages (a CLI that fails on very large monorepos, a model
   that mishandles one build system) reach users at the first canary stage.
2. **Shadow every job for every candidate.** Doubles production cost: it adds about $0.33–0.63M per year, the
   cost of production itself at 300 jobs per day (eval-harness.md §8.2), and doubles sandbox load.
3. **Sampled shadow (10% of eligible live jobs, ≥ 50 pairs, ≤ 5 days) for model, CLI minor or major, graph,
   router, and memory-policy changes (chosen).**

## Decision

Option 3. Shadow jobs run in the **source tenant** (its KMS keys and namespace, but charged to the platform eval budget account `fgw:pipeline:<pipeline_id>`, never to the tenant's quota; R2-061) with
`options.evaluation.mode = shadow`: gates auto-approve, clarifications stop the case, nothing is published,
nothing is notified, and publisher-worker refuses any publish for such a job. Cost: about $150–$690 per pipeline,
capped at $1,500 (promotion.md §5).

## Consequences

- Tenant data never moves to the eval tenant.
- Shadow detects gross operational breakage before any user sees it, but not quality differences that need
  reviewer outcomes.
- Shadow jobs carry the same untrusted-code exposure as their live originals, doubling it for the sample only.

## Revisit when

- Production volume reaches about 1,000 jobs per day: a 3% sample then gives 50 pairs within 2 days.
