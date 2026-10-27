# ADR-05-005: Flaky versus regression by in-run change/base evidence plus history, with fixed rules

- **Status:** Accepted (WS-05, 2026-10-24); the history store depends on CCR-05-02
- **Constraints:** D-15 (bounded loops), D-19 (spirit: deterministic decisions)

## Context

A failing test in QA is either caused by the change, pre-existing on the base branch, flaky, or
caused by the environment. Wrong answers are expensive in both directions: treating a flake as a
regression wastes a fix loop (an implementer run plus a QA run) and may make the agent "fix" correct
code; treating a regression as a flake ships a bug.

## Options

1. **Rerun-until-pass** (common CI practice). Hides intermittent regressions; no base comparison.
2. **History only** (flake-rate database). Cannot judge new tests; misattributes regressions in
   historically flaky tests.
3. **Base comparison only.** Distinguishes pre-existing from caused, but one base pass cannot
   separate intermittent behavior.
4. **In-run change/base reruns, interleaved, plus history for mixed outcomes, plus quarantine
   (chosen).**
5. **LLM judgment.** Not reproducible; rejected as a decider (it may only escalate, ADR-05-004).

## Decision

- Each failed test is re-run individually: 2 more times on the change tree and 3 times on a base
  worktree, interleaved (`change, base, change, base, base`), unless more than 25 tests failed (then
  one whole-step base run).
- Rules (failure-feedback.md §3.4), first match wins: environment signatures; dependency-change
  install failures (code); added/modified failing tests (code); consistent change failure with
  consistent base pass (code); failure on both (pre-existing, or environment for build/app stages);
  mixed change outcome with quarantine/base flake/history ≥ 2% (flaky_known, non-blocking); mixed
  change outcome with clean base and stable history (flaky_new, blocking by default).
- History: 30-day per-test stats in Postgres schema `qa` (derived from QA reports; CCR-05-02),
  excluding code-caused failures from the flake rate.
- Quarantine: human-curated, owner and expiry, in the environment definition (pinned in the bundle).
  Quarantine never overrides consistent change-fail/base-pass evidence.

## Consequences

- A flaky test with per-attempt failure probability 5% is misread as a regression with probability
  ≈ 1.1 × 10⁻⁴ per failure occurrence; a deterministic regression is never misread as flaky.
- An intermittent regression (a new race) is blocked as `flaky_new`.
- Reruns add seconds to minutes per QA run with failures, plus one base install (cached).
- Without the history store (CCR rejected or Postgres unavailable), C1/P1 decisions are unchanged;
  only the flaky_known/flaky_new split for unquarantined tests degrades to F3 (non-blocking, low
  confidence).

## Revisit when

- WS-06's labeled sample (200 failures per quarter) shows misclassification above 5% for any cause.
- Test suites where one rerun costs more than 2 minutes (large e2e suites): consider fewer reruns
  with stronger history weighting for those stages.
