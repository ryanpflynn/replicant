# ADR-05-001: QA runs in a separate AgentRun on a fresh checkout with the diff applied

- **Status:** Accepted (WS-05, 2026-10-24)
- **Constraints:** D-05, D-16, D-21, D-07

## Context

The implementer AgentRun already has a built workspace, and running QA there would save one
sandbox start and one dependency install. But:

- The Publisher pushes from the **diff bundle** (D-21: no push credentials in sandboxes), not from
  the implementer's workspace. Anything not in the bundle (untracked generated files, edited
  caches, local environment changes, files in `.gitignore`) does not reach the PR.
- The implementer is an agent with a shell, prompt-injectable by issue text and repo content (D-21).
  It can edit tests, skip them, or fake report files in its own workspace.
- QA needs a different pod shape: sidecars, more memory, image-build security profiles (Kata only),
  and `workload_class: qa_build`, which policy may map to a different node pool and RuntimeClass
  than `coding`.
- The AgentRun contract already supports a QA run: role `qa`, workload class `qa_build`, the inputs
  manifest lists a "prior diff bundle to apply", and `WorkspaceReady` covers `DiffApplyFailed`
  (C-RUNNER, C-CRD).

## Options

1. **QA inside the implementer run** (same workspace, after the CLI exits). Cheapest. Tests a tree
   that is not the published artifact; the agent controls the environment that grades it; one pod
   shape must fit both roles.
2. **Separate AgentRun reusing a snapshot of the implementer workspace.** Needs workspace
   snapshots, which D-06/D-07 rule out as persistence ("snapshots are never a persistence or
   checkpoint mechanism"; workspaces are scratch).
3. **Separate AgentRun on a fresh checkout of `base_sha` with the diff bundle applied (chosen).**

## Decision

Option 3. The QA node starts `runner.start_run` with `role: qa`, `workload_class: qa_build`, the
latest diff bundle staged in inputs, and the same `base_sha`. `workspace-prep` applies the bundle;
the harness records `head_tree_sha = git write-tree`, which identifies exactly what was tested. QA
produces no `diff_bundle`; anything it writes to the checkout is discarded.

## Consequences

- "QA passed" is a statement about the exact tree the PR will contain (modulo the Publisher's
  rebase, which triggers QA again, D-19).
- Extra cost per job: one sandbox start (p95 target < 90 s) plus dependency installation, mostly
  absorbed by mirror and node-local caches (WS-02 fast path). At ~1.4 QA runs per job and 1,000
  jobs/day peak: ~1,400 QA runs/day.
- The implementer still runs fast tests in its own run; those are advisory and never substitute.
- Trust separation: the QA run's agent (if any) cannot modify the diff, and its tools are
  restricted (ADR-05-004).

## Revisit when

- Sandbox start plus install dominates QA time for most codebases (measure `startup` and `install`
  durations per codebase type for 30 days); then consider pre-warmed dependency layers per lockfile
  hash, still on a fresh checkout.
