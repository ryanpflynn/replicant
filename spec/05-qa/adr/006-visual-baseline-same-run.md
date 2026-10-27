# ADR-05-006: Visual and accessibility baselines come from a base pass in the same AgentRun

- **Status:** Accepted (WS-05, 2026-10-24)
- **Constraints:** D-08, D-16 (visual diff against a baseline from the base branch, built and run in
  the same AgentRun or cached: decide), D-21

## Context

Visual diffs and a11y deltas need the base branch rendered under the same conditions as the change.
Playwright warns that rendering varies with host OS, browser version, settings, and hardware, and
recommends generating baselines in the same environment [verified:
https://playwright.dev/docs/test-snapshots, 2026-10-24].

## Options

1. **Same-run base pass (chosen).** Build and run `base_sha` in a worktree after the change-side web
   stages, crawl the same routes, compare.
2. **Cross-job baseline cache** keyed by `(repo_id, base_sha, environment digest, route digest)`
   in S3. Saves the base build when many jobs share a base commit. Needs a new key layout outside
   run prefixes (contract change), a writer for shared objects (sandboxes may only write their own run
   prefix), and opens cache poisoning: a compromised sandbox could plant a baseline that hides a
   change in other jobs.
3. **Baselines committed in the repo** (`toHaveScreenshot` snapshot files). Teams that already do
   this are covered by repo tests; generating them requires the same environment anyway, and the
   Replicant must not commit to repos outside the PR.

## Decision

Option 1, with **reuse within a job**: when an earlier QA run of the same job has the same
`base_sha`, environment digest, and route-list digest, the QA node stages its base captures and a11y
results into the new run's inputs (server-side copy from the job's own earlier run prefix), and the
web base pass is skipped. The base pass runs only when the diff touches `visual.whenPathsChanged`
globs (or when a11y deltas are needed and no reusable baseline exists).

## Consequences

- Cost: one more build and app start for UI changes (Next.js: ~4–7 minutes), avoided on fix
  iterations and repair cycles with an unchanged base.
- No shared mutable state across jobs; a poisoned baseline can affect only the job that produced it.
- Base and change render in the same VM with the same fonts and browser build, so pixel noise is
  minimal and thresholds can stay tight (`maxDiffPixelRatio` 0.002 default).

## Revisit when

- Base passes exceed 15% of QA sandbox minutes; then design option 2 with a publisher-side (trusted)
  baseline builder and a contract change for the key layout.
