# WS-06: Release Bundles, Evaluation Harness, and Self-Improvement

**Output directory:** `spec/06-releases-and-evals/`
**Constraints most relevant:** D-02, D-03, D-14, D-17

## Purpose

Make every change to factory behavior (graphs, prompts, models, CLI versions, agent and environment
definitions, policies, memory) a versioned, evaluated, reversible release. Without this, the factory
cannot tell whether it is getting better.

## Required outputs

### 1. Release Bundle (`release-bundle.md`)

Full schema (conforming to the WS-00 envelope), content addressing, the resolution process (labels
→ concrete versions), immutability, storage in the Postgres registry, the bundle diff (what changed
between two bundles, shown in the UI), and bundle assignment rules (default per environment,
per-tenant overrides, experiment cohorts).

### 2. Evaluation harness (`eval-harness.md`)

- **Golden task set**: how tasks are collected (historical merged PRs with their originating
  issues, synthetic tasks, past factory failures), stored (Langfuse datasets plus repo snapshots in
  object storage), labeled, and refreshed. Coverage targets per workflow type and codebase type.
- **Execution**: running a candidate bundle against the task set in real AgentRuns on an isolated
  eval tenant, with the gateway in metered mode; parallelism and cost caps.
- **Scoring**: deterministic checks first (build, tests, hidden tests, QA health, contract checks,
  diff size, files touched outside scope), then LLM-judge rubrics (versioned, calibrated against
  human labels, with agreement measured). Scores recorded in Langfuse.
- **Comparison**: candidate vs current bundle with statistical treatment (paired comparisons,
  confidence intervals, minimum detectable effect given task-set size).
- Replay determinism limits: what can and cannot be reproduced given LLM nondeterminism.

### 3. Promotion pipeline (`promotion.md`)

Stages: offline eval → shadow (candidate runs alongside current on live jobs without publishing;
decide whether affordable) → canary ramp by percentage or tenant through Worker Versioning (WS-01)
→ full. Promotion gates with thresholds, automatic rollback triggers from online metrics, and human
sign-off rules. Sequence diagram from "prompt edited in Langfuse" to "100% of new jobs".

### 4. Online metrics (`online-metrics.md`)

Job success rate, PR acceptance/merge rate, review rounds per PR, repeated-comment rate, QA
first-pass rate, time to PR, human wait time, cost per job, gate override rate, take-over rate,
cancellation rate. Definitions, sources, Prometheus metric names, and dashboards (coordinate with
WS-08).

### 5. Self-improvement loop (`self-improvement.md`)

How online signals and PR feedback flow into (a) memory (WS-04), (b) proposed prompt or definition
changes (generated as candidate bundles, never auto-promoted), and (c) new golden tasks from
failures. Guardrails against optimizing for the judge rather than for reviewers.

### 6. CLI and model upgrades (`upgrades.md`)

Procedure for a new Claude Code or Codex CLI version or a new model: candidate bundle, eval, canary,
rollback; compatibility checks for session-capture formats (WS-04) and output formats (WS-03).

## Must answer

- How many golden tasks are needed to detect a 5-point change in success rate, and what does a full
  eval run cost?
- How does an operator roll back a bad prompt change in under five minutes, and what happens to jobs
  already pinned to it?
- How is LLM-judge drift detected?

## Acceptance criteria

- Bundle schema and promotion state machine are complete.
- Every promotion gate has a numeric threshold or an explicit human decision.
- Eval cost and statistical power are estimated with stated assumptions.
