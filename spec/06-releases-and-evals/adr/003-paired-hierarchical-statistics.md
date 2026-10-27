# ADR-06-003: Paired, hierarchical statistics with tiered non-inferiority margins

- **Status:** Accepted (WS-06, 2026-10-24)
- **Constraints:** D-03 (promotion after passing the harness), brief "detect a 5-point change"

## Context

Agent outcomes are binary, vary strongly by task difficulty, and vary run to run because sampling is
nondeterministic (no seeds; sampling parameters are rejected by the current Anthropic API version [verified:
https://platform.claude.com/docs/en/api/versioning, 2026-10-24]). Each case costs about $3.65 (eval-harness.md §8).

## Options

1. **Unpaired two-proportion tests.** 1,471 runs per arm to detect 0.60 → 0.65. Ignores task difficulty.
2. **Paired McNemar on one run per task.** 469–1,097 task pairs depending on the discordance rate, which is unknown
   in advance and collapses run noise and difficulty into one number.
3. **Hierarchical paired design** (tasks × repetitions, cluster-level analysis, task bootstrap) with
   non-inferiority for routine promotion and superiority for improvement claims (chosen).

## Decision

Option 3, with the model, numbers, and procedure of eval-harness.md §6. Tier sizes: T1 60 × 1, T2 300 × 1, T3
450 × 2. Margins: 16, 7.5, 4.5 points at about 0.80–0.85 power (computed by `tools/eval_sizing.py`, confirmed by
Monte Carlo). An O'Brien–Fleming interim stop at 50% for harm.

## Consequences

- A 5-point improvement is detectable only at T3 size (MDE 4.7 pt). T2 can rule out regressions larger than
  7.5 points, not smaller ones. Smaller effects are left to online metrics.
- The parameters ρ and σ_u are assumptions until measured. They are re-measured quarterly and tier sizes are
  recomputed.

## Revisit when

- Measured ρ falls below 0.3 (tasks less discriminating): T3 needs about 543 tasks × 2.
- Costs per case fall enough that R = 3 becomes affordable for T3.
