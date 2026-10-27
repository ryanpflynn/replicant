# ADR-06-005: Overlay bundle requests produce one-change candidates

- **Status:** Accepted (WS-06, 2026-10-24)
- **Constraints:** D-03, C-DEF §4.3 (WS-06 owns the request format)

## Context

C-DEF's example bundle request resolves every selector at build time (`agent/x` = latest active,
`prompt/y@label:production`). A build from HEAD then picks up every unrelated change merged since the last
release. The diff becomes compound, the eval attributes nothing, and a rollback reverts several changes at once.

## Options

1. **Full builds only**, and accept compound diffs.
2. **Release branches of the config repo** per candidate. Misses Langfuse prompts, which are not in Git.
3. **Overlay requests (chosen):** start from an existing bundle's pinned spec (usually the environment's default)
   and re-resolve only the named overrides. Fail the build if the resulting diff contains change classes the
   request did not declare.

## Decision

Option 3 (release-bundle.md §3.2). Full builds remain for nightly builds and deliberate compound releases.

## Consequences

- Each candidate's diff equals its intent, so evals attribute effects and rollbacks are precise.
- Two overlays on different bases yield different bundles for the same change (lineage is part of the content).
- Queued overlays can be batched into one compound overlay when the ramp slot is the bottleneck (promotion.md §12).

## Revisit when

- Concurrent ramps (CCR-06-05b) are accepted, which removes most reasons to batch.
