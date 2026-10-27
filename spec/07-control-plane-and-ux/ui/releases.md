# Releases and versions

Routes `/releases` (bundles), `/releases/bundles/:rbId`, `/releases/workers`, `/releases/pipelines/:rplId`,
`/releases/assignment`. Operations are WS-06's ([promotion.md](../../06-releases-and-evals/promotion.md));
implementation and API mapping: [../architecture.md](../architecture.md) §2.5. Prototype:
[releases.html](../design/prototype/releases.html) and the rollback dialog state
(`releases.html?dialog=rollback`).

```
┌ Releases / Bundles · prod ──────────────────────────────────────────────────── [⌘K] ◐ ┐
│ Failed jobs by bundle  share reaching failed, hourly, 24 h · all tenants                │ (1)
│ ─ rb_k7wq0f2e9n (canary)  ─ rb_ds5p3wmcj (default)  Table view                          │
│ 20% ┤                                          ¦ring ¦10% │R3 pause 06:40              │
│ 10% ┤                                          ¦     ¦   ╭─────── 9.7%                 │
│  0% ┼──────────────────────────────────────────┴─────┴───────── 4.3%                   │
├──────────────────────────────────────────────────────────────┬─────────────────────────┤
│ BUNDLE        VERSION      STATE            ASSIGNMENT  RUN WAIT FAIL24 EVALΔ          │ BUNDLE rb_k7wq0f2e9n    │ (2)
│▌◔ rb_k7wq0f2e9n 2026.10.24-5 ⏸ canary·held   canary 10%  41  12  9.7%  −0.9 pt       │ Pipeline rpl_01m5m2v8q4 │
│ ● rb_ds5p3wmcj 2026.10.24-3 ● promoted      default     96  63  4.3%  +0.4 pt       │ ▬ring ▬10%⏸ ◆50% ─full  │ (3)
│ ◑ rb_c4m8p2r6t0 2026.10.17-2 ○ deprecated   draining     3  18  5.1%   —            │ ▓Guardrail R3 paused at ▓│ (4)
│ ○ rb_9hf2xw1d3m 2026.10.09-1 ○ deprecated   drained      0   0   —     —            │ ▓06:40 … auto rollback  ▓│
│ ⊠ rb_2tq8v0kz4p 2026.10.23-1 ⊠ eval_failed  —            0   0   —   −4.8 pt        │ ▓at 10:40               ▓│
│ Tenant-derived bundles · 37 across 31 tenants · expand                                │ [Roll back…][Resume][Abort]│ (5)
├──────────────────────────────────────────────────────────────┤ Rollback target ds5p…  │
│ Worker deployment replicant-worker                             │ Running 41 · waiting 12 │
│ current b…3f9a1c2e7d4b · candidate b…8d2e44a1c09f registered │ Sign-off at 50%: 2 signers│ (6)
│ BUILD                STATUS      PINNED POLLERS OLDEST       │ Changes vs parent       │ (7)
│ b20261024-8d2e44a1c09f registered  53     6    5 h          │ ~ prompt 21 → 22 …      │
│ b20261024-3f9a1c2e7d4b current     159    12    1 d          │ Evaluation T2 …         │
│ b20261017-a17c3e9b2f40 draining     21     2    8 d [Offer migration]               │
│ b20261009-0e6b5d8c71aa drained       0     2    —   [Retire]                         │
└──────────────────────────────────────────────────────────────┴─────────────────────────┘
```

1. The chart answers one question ("which bundle raised the failure rate?"); stage and guardrail
   markers; table view toggle; data `GET /v1/releases/bundles/{rb}/failures`.
2. Bundle panel for the selected row; derived bundles group under their base (ADR-013).
3. Stage track from the pipeline (ring-early → 10% → 50% → sign-off → full; holdback after).
4. Guardrail banner: trigger ID, observed delta, P(worse), n, auto-rollback deadline (WS-06 §6.2).
5. Roll back… (primary) opens the dialog: it loads `GET /v1/releases/{env}/assignment:rollback-preview`
   (blast radius: pinned jobs by status, by tenant, open gates by kind, derived bundles), sends `preview_digest`,
   asks for a reason, and offers `migrate_running` `required_by_operator` / `offer` / `none` (and `required` in the
   menu) with no default except WS-06's `suggested_migrate_running` (`required_by_operator` for a failed canary;
   XD-FIX-R2-021). Resume = `release.hold` resume with reason. Abort = `release.abort`.
6. Sign-off requirement from the promotion policy (two-person classes, excluded author).
7. Changes vs parent from `GET /v1/registry/bundles/{rb}/diff` (WS-06 path).

Permissions: `release:read` sees everything read-only (buttons hidden); `release:operate` rollback,
resume, abort, offer migration, worker operations; `release:revoke` (replicant-release-managers) the `required` and `required_by_operator` options;
`release:signoff` signs in the inbox.

## Sub-screens

| Screen | Content |
|---|---|
| Bundle detail `/releases/bundles/:rbId` | Full bundle document (collapsible by section), lineage, promotion history, eval comparisons (WS-06), pipeline history, pinned jobs list, notes |
| Pipeline `/releases/pipelines/:rplId` | Stages with timestamps, guardrail evaluations table (every 10 min, sparkline per trigger), sign-offs (who, when, digest), holdback status |
| Assignment `/releases/assignment` | Default, up to three ramp rows (`ramps[]` slots 0–2: canary or experiment in 0–1, holdback in 2 at buckets `[9500, 10000)`; each with candidate, percent, bucket range, tenant include/exclude, pipeline), tenant overrides (holdback, operator pins, layer pending) with expiry; history of versions with diffs and who/why. The only precondition shown before a ramp is "candidate worker build registered on all task queues" (XD-FIX-R1-132, R1-136) |
| Model retirements `/releases/model-retirements` | Calendar from `GET /v1/releases/model-retirements`: retiring model, date, bundles that use it, successor mapping and its approval (XD-ORCH-01, XD-FIX-R2-044) |
| Eval curation `/releases/evals/labeling` | Blind labeling queue (no judge output, no arm) and golden-task verify/transition, for `eval:curate` (XD-FIX-R1-142) |
| Workers `/releases/workers` | All deployments (`replicant-worker`, `publisher-worker`, `replicant-projector`, `eval-worker`), versions, pollers, pinned counts, draining progress |

## States

| State | Rendering |
|---|---|
| Loading | Chart placeholder (axis only), table skeleton |
| Empty (fresh install) | "No bundles yet. Build one: `replicant registry bundles build bundles/requests/main.yaml`" |
| No canary running | Side panel shows the default bundle with "Start pipeline" for a built candidate |
| Rollback committed | Banner "Rolled back to rb_ds5p3wmcj at 10:21 by Sam Whitfield; 41 jobs remain pinned (offer migration)" |
| Temporal visibility unavailable | Pinned counts still show (they come from the projection, `jobs_read.v_pinned_counts`, R1-188); only the Retire precondition, which needs a live visibility count, is disabled with "visibility unavailable" |
| Error | Region-scoped |
