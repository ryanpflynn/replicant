# Job detail

Route `/jobs/:jobId[/plan|/diff|/qa|/runs|/transcripts|/artifacts]`. Data: `GET /v1/jobs/{id}`,
`/timeline`, `/runs`, `/artifacts`, `/memory`; SSE topic `job:<id>`; live tail SSE for the active
run. Permission `job:read` (404 otherwise, no existence leak).

```
┌ Jobs / payments / job_01m5p7x7r0bt69j0h0fbxepw90 ────────────────────────── [⌘K] ◐ ┐
│ Refund ledger entries double-count FX fees for partial refunds        [→ Steer S][Pause P|Take over T] Cancel │ (1)
│ ◔ Running  implementing · cycle 0 │ acme-payments/ledger-api #1423 │ MT Maya Torres via GitHub label replicant │ bugfix · 08:00 · 1h 02m │
│ ▬▬▬routing ▬▬▬context ▬▬▬planning ▬▬▬◆plan gate ▬▬▬implementing ───qa ───review ───verify ───◆publish gate ───publishing ───pr review │ (2)
├──────────────────────────────────────────────────────────────────────┬─────────────────────────────────────────┤
│ ◔ implementer run_gj3h3ep3ph · claude-code 2.1.240 · opus-5-5 · kata│ Status   ◔ Running · implementing        │ (3)
│   6 vCPU/16 GiB · disk 11.2/40 GiB        running 14m 03s  Live ↗    │ Cost     $2.86 of $40.00 ▬───────────    │ (4)
│ 08:55:41 Bash go test ./ledger/... -run 'TestApplyRefund|…'          │ Risk     HIGH 3 rules                    │
│ 08:55:58 ok   github.com/acme-payments/ledger-api/ledger 4.112s      │ Bundle   rb_ds5p3wmcj 2026.10.24-3       │
│ 08:56:02 Edit ledger/refund.go (+14 −9)                              │ Worker   replicant-worker:b20261024-3f9a…  │
├──────────────────────────────────────────────────────────────────────┤ Repo     main @ 7c1e9a40d2               │
│ Timeline 23 │ Plan │ Diff │ QA │ Runs 2 │ Transcripts │ Artifacts 31   │ Repair   0 of 5 cycles               │
├──────────────────────────────────────────────────────────────────────┼─────────────────────────────────────────┤
│ 08:56:09 ◔ implementer running · turn 41 · 212k in / 18k out     now │ Memory used  4 entries · read-only (5)  │
│ 08:47:30 ➜ Steering from Maya Torres delivered live to run_gj3h…  UI │  money.Amount, never float64            │
│          ┃ USER TEXT · msg_01m5pc2k8q                                │   mem_01m42qyvr0@3 · repo · PR #1288    │ (6)
│          ┃ Use the existing Proration helper in ledger/fx …          │  FX reversals keyed on refund_id        │
│ 08:42:02 ◌ implementer sandbox ready · Provisioning 47 s             │ …                                       │
│ 08:41:12 ✓ Plan approved by Priya Raman in Slack · 35 min  dec_cnd2… ├─────────────────────────────────────────┤
│          ┃ APPROVER COMMENT Fine. Watch the EUR→GBP rounding …       │ Observe                                 │ (7)
│ 08:06:45 ◆ Plan approval gate opened · eligible ledger-core … UI Slack GH│ Langfuse trace     5ba8ff0c12…           │
│ 08:06:44 ▲ Risk classified HIGH · 3 rules · LLM did not raise        │ Grafana logs (job_id)                   │
│          path.money_movement codeowners.ledger_core service.tier1    │ Tempo trace                             │
│ 08:06:40 ✓ planner succeeded · 6m21s · $0.61 · plan r1               │ Temporal workflow ↗  (operators only)   │
│ …                                                                    │                                         │
└──────────────────────────────────────────────────────────────────────┴─────────────────────────────────────────┘
```

1. Actions shown depend on `job:control:own|any` and status (e.g. Resume replaces Pause when paused).
   Terminal jobs offer "Take over" (failed, expired; within retention) and "Restart from approved plan"
   (expired at a gate). See [job-actions.md](job-actions.md).
2. Phase track from the graph's phase list for this workflow type; gates marked ◆; the current phase
   in the info colour, a waiting gate in the accent.
3. Live node strip only while a run is active; tail is redacted transcript from Loki (C-STATE §2 exc. 2).
   `Succeeded` phase vs task outcome are shown separately on the Runs tab (C-CRD risk).
4. Cost from `cost.job_rollups` joined at read; bar turns attention at ≥ 80% of the cap.
5. Memory used = memory-service's job view for the pinned snapshot (`GET /v1/jobs/{job_id}/memory`, proxied to
   `/internal/v1/jobs/{job_id}/memory` with the viewer's repo access; R1-099). Read-only for everyone, no score
   bands or ranking internals; source PR links only for repos the viewer can read. Each entry has "Flag this entry"
   (`POST /v1/jobs/{job_id}/memory/{entry_id}/flags {reason_code: wrong|outdated|too_broad|harmful|unsafe|other,
   note?}`, R2-028), and the panel renders the returned `effect` ("Hidden from new jobs while admins review",
   or "Admins will review by <due_at>"). Admins additionally get "Open in memory curation ↗" (D-25).
8. Terminal jobs show "Report a problem with this result" (`POST /v1/jobs/{job_id}/outcome-reports {kind:
   wrong_fix|regression, evidence_url?, note?}`, R2-017), for anyone who can view the job; memory-service decides
   what changes, admins review.
6. Untrusted text appears only inside labelled quote blocks, plain text.
7. Deep links computed from `job_id` (trace ID by rule T-1; Loki query by structured metadata).

## Tabs

| Tab | Content | Source |
|---|---|---|
| Plan | Plan revisions (r1, r2) with diff between revisions, approval decisions per revision | `plan` artifact (presigned), gates |
| Diff | Diff viewer (components.md §6) for the latest `diff_bundle`; selector per run/cycle; "approved digest" stamp when a publish gate approved it | `diff_bundle` |
| QA | Summary (build, tests, health, Playwright checks), screenshots grid with lightbox, JUnit failures, logs, trace download | `qa_report`, `screenshot`, `junit_xml`, `build_log` |
| Runs | Table of runs: node, cycle/ordinal, role, phase, **result outcome**, reason, isolation, duration, cost | `job_runs` + cost |
| Transcripts | Log viewer per run (chunks), search, redaction chips | `transcript` |
| Artifacts | Index with type, size, retention class, download (audited presign) | `job_artifacts` |

## States

| State | Rendering |
|---|---|
| Loading | Header skeleton, timeline skeleton rows; side panel skeleton |
| Queued (first seconds) | Timeline shows "Job created from GitHub …" and "Routing…" with the ring-dotted glyph; banner if `start_status=pending`: "Queued: Replicant will start this shortly" |
| Waiting capacity | Live strip from `sub_status` (R2-020): "Waiting: tenant run quota 12 of 12 in use · waiting 41 min · attempt 9" (runner-api has no queue, so no position; XD-W2-02-04) with actions "View payments' running jobs" and "Ask payments-leads to raise the quota". No ETA unless computed from the tenant's running jobs. Cluster capacity: "Waiting: cluster capacity (attempt 4)". Budget exhausted: neutral banner "Paused: tenant model budget exhausted, resets Nov 1 · contact payments-leads" (not in "needs a human"). No progress 30 min: banner from the stall reconciler |
| Waiting human | Accent banner "Waiting for plan approval from ledger-core · suggested Priya Raman · expires Oct 31 · [Open approval]" |
| Paused | Neutral banner "Paused by Arjun Mehta at 09:12 (at next node) · [Resume]" |
| Failed | Danger banner with reason in words, CRD reason, the node, the failure class (environment, infrastructure, agent, QA, policy; for example "Failure class: environment" for `WorkspaceDiskExceeded`, as in the prototype), and a suggested next step with buttons Take over and Start again (R2-036, rendered in the prototype); "Retry from node" is not offered (workflow decides retries) but "Start again with same request" is |
| Taken over | Special banner: "Taken over by Daniel Okafor · package tko_…" with the package links |
| Bundle notice | Banner "A newer bundle is available / this bundle was revoked: migration required by Oct 30" with Migrate (opens the migration gate) |
| Not found / no access | "No job job_… that you can see." |
| Error | Region-scoped error banners; other regions keep working |
| Stale | Lag banner "Showing data as of 10:41:12 · live updates reconnecting" (rendered in the prototype) |
