# History Budget (must-answer)

**Question (WS-01 brief):** What is the maximum history a 30-day job with 5 repair cycles produces, and
how is it bounded?

## Answer

**The maximum is bounded per Temporal run: no run exceeds about 4,100 events or 4 MB** (R1-018
reconciliation). Continue-As-New fires at the next history safe point after the server suggests it
(`limit.historyCount.suggestContinueAsNew` 4,096 / `limit.historySize.suggestContinueAsNew` 4 MB). History
safe points include every Runner-run poll outside parallel branches, so even the worst single node (one
AgentRun at the maximum deadline, about 4,150 events of polling, §2) is split across runs. A safe point
adds at most one poll iteration (about 11 events) past the suggestion. Temporal's hard per-run limits are
51,200 events / 50 MB.

The **job total** is bounded by the job's loops and lifetime (5 repair cycles, QA fix iterations,
`maxVisits`, the 60-day `job_max_age_days`), not by a per-job history limit. The **typical** heavy case,
a high-risk 30-day job with 5 repair cycles, is about **5,500 events in 12 runs** (about 3.4 MB in total):
4,845 events measured on Temporal server 1.32.0 (wave-2 code; 4,824 on the interpreter engine) for the event-heavy part, plus a model for the multi-day
waits and notifier hints that a test cannot run for 30 days (§2). Its largest run is about 1,390 events
(1,121 measured, plus the modeled gate reminders and hints). Earlier drafts said "no run above about
1,400 events". That was this typical case, not the bound.

**Measurements (R1V-090).** There are three, each of scenario `history_budget`, and only the latest is quoted
elsewhere:

| Code | Pregel | Interpreter | Largest run | Used for |
|---|---|---|---|---|
| wave 1 | 4,824 | 4,824 | 1,118 | the per-run table in §1 |
| wave 2 | 4,845 | 4,824 | 1,121 / 1,118 | the totals in this answer, README.md and versioning.md §9 |
| wave 3 | see graphs/README.md §9 | | | final run |

The per-run differences between them are at most 6 events (one timeline event and QA index activities).
Older figures (4,796; 4,808/4,811) predate wave 1. Carry ≤ 27 KiB in all three.

The bound does not depend on job length. It is enforced by:

1. **Continue-As-New at declared boundaries:** before every PR-review wait (phase) and at the start of every
   repair cycle. A 5-cycle job is 12 runs: 1 initial, 5 × (PR wait + repair cycle), 1 final PR wait.
2. **Continue-As-New at history safe points:** at every node boundary, in every gate and PR wait iteration,
   and **inside long run waits**, whenever `is_continue_as_new_suggested()` (server: 4,096 events / 4 MB)
   or more than 8,000 events. The interrupted node is re-entered with memoized side effects. Measured: a
   long implementer wait forced across 8 Continue-As-New transitions kept one Runner run and completed
   (scenario `mid_run_can`).
3. **Bounded loops:** repair budget (5; hotfix 2), QA fix iterations (2), `maxVisits` on every looping node,
   run retries (2), base-check rebases (3). Each wait is time-bounded: gate expiry 7 days, PR-wait expiry
   30 days after the last activity, run deadline `startup + active + finalize + 300 s`.
4. **Bounded carry:** `JobState` ≤ 256 KiB (fails loudly). Measured 11.4 KiB after the initial run, growing
   about 3 KiB per cycle to 27.0 KiB (27,654 bytes) after 5 cycles. Every growing field is capped: runs ≤ 30, closed
   gates ≤ 10, dedupe windows 7 days, per-cycle counters pruned at cycle boundaries, steering backlog
   staged to S3.

## 1. Measurement

Scenario `history_budget` (reference/tests/test_scenarios.py): a bugfix job at **high risk** (plan gate,
publish gate, and a publish gate on every repair push), **5 repair cycles**, then merge. Poll counts per
run match the production cadence (120 s while Running): planner 5, implementer 12 (≈ 25 min), QA 10,
reviewer 4, repair 8. Stub activities, Temporal CLI 1.9.1 / server 1.32.0, `temporalio` 1.34.0, both
engines (identical side-effect sequences: 183 side-effect activities, 14 AgentRuns, 236 projections).

| Run | Content | Events | History bytes | Carry in (bytes) |
|---|---|---|---|---|
| 1 | routing, context, plan, plan gate, implement, QA, review, verify, publish gate, publish | 1,118 | 384,089 | 1,815 (start input) |
| 2 | PR wait (until review) | 58 | 66,844 | 11,393 |
| 3 | repair cycle 1 (triage, repair, QA, verify, push gate, push, memory) | 673 | 360,042 | 11,389 |
| 4 | PR wait | 55 | 81,613 | 15,641 |
| 5 | repair cycle 2 | 673 | 424,033 | 15,400 |
| 6 | PR wait | 55 | 94,521 | 18,645 |
| 7 | repair cycle 3 | 670 | 486,417 | 18,404 |
| 8 | PR wait | 55 | 107,414 | 21,648 |
| 9 | repair cycle 4 | 673 | 549,522 | 21,407 |
| 10 | PR wait | 55 | 120,328 | 24,651 |
| 11 | repair cycle 5 | 670 | 611,910 | 24,411 |
| 12 | final PR wait, merge, terminal bookkeeping | 69 | 109,589 | 27,654 |
| **Total** | | **4,824** | **3,396,322** | max 27,654 |

Re-measured after the Phase 3 fix round (wave-1 code; interpreter engine: same 12 runs, 4,824 events,
identical side-effect activity sequence of 183 activities) [verified: scenario `history_budget`,
2026-10-24]. Earlier measurements (4,796 and 4,808 events) predate wave-1 changes such as the
`ops.read_kill_switches` read at start.

Event mix: workflow tasks 46.8 % (752 × 3), activities 41.0 % (659 × 3, of which about 236 are non-awaited
projections), timers 7.8 %, search attribute upserts 3.3 % (159, after coalescing to one per workflow
activation; 335 before), Signals 0.4 %, Updates 0.3 %, CAN and lifecycle 0.5 %.

**History bytes grow by about 63 KB per cycle** because every projection activity carries a complete
`JobSnapshot` (C-STATE §2), and the snapshot lists the job's runs and gates. These lists are capped (100
runs, 50 gates in the snapshot; 30 runs and 10 closed gates in the carry), so the largest possible run is
about 1.5 MB. Optional CCR-01-05 (batched projection events) would cut both the activity count and the
bytes.

## 2. Model for the 30-day job

`tools/history_model.py` adds the costs a test does not exercise. Each addition uses Temporal's
structural event counts (an activity is 3 events, a workflow task 3, a timer 2, a Signal 1):

| Addition | Assumption | Cost each | Events |
|---|---|---|---|
| Plan gate waits 2 days | reminders at 4 h, 24 h, 48 h; escalation at 24 h | 15 (timer + notify activity + projection) | 60 |
| Publish gate waits 1 day | reminders at 4 h, 24 h; escalation | 15 | 45 |
| Push gate per cycle waits about 4 h | 1 reminder per cycle | 15 | 75 |
| PR waits | 2 reminders over the job (day 7 and day 21 without activity) | 15 | 30 |
| run-notifier hints | about 4 phase Signals per run, versus 1 in the test: 3 extra per run × 14 runs | 11 (Signal + poll activity + cancelled timer) | ≈ 500 (165 in the initial run, 66 per cycle) |
| **Modeled job total** | | | **≈ 5,500 events** (model output 5,529) |
| Largest run (initial) | 1,118 measured + gates and hints | | **≈ 1,390 events** (model output 1,388) |
| Largest repair-cycle run | 673 measured + hints and a reminder | | **≈ 755 events** (model output 754) |

Worst cases:

| Case | Events | Bound |
|---|---|---|
| Single AgentRun at the maximum deadline (30 min startup at 15 s polls + 8 h active at 120 s) | ≈ 4,150 within one node (model 4,147) | mid-wait Continue-As-New at the 4,096 suggestion (`mid_run_can`), so no run exceeds about 4,100; **this is the per-run bound** |
| Gate open 30 days with daily reminders | ≈ 450 | gate expiry (7 days by default) |
| 200 steering messages | ≈ 2,200 (Update + activity) | Updates are capped at 2,000 per run; CAN resets |
| Signal flood (forged hints) | ≤ 10,000 per execution (server) | hints are ignored unless the run is active; alert |
| 1,500-repo maintenance parent | ≈ 45,000 over its life | CAN every ~4,096 events (about 130 children per run) |

## 3. Billable actions (input to self-hosted vs Cloud, WS-10)

Measured for the heavy job (Temporal Cloud action definition: starts, CAN, activity schedules, timers,
Signals, accepted Updates, search attribute upserts): **1,044 actions** (1,220 before upsert coalescing).
The typical job (one repair cycle, one or two gates) is about 400 to 600 actions. At 300 jobs/day (9,000 per
month × ~600) that is about 5.4 M actions per month, roughly $270 at the published tiers, so the $500
Business minimum applies. At 1,000 jobs/day it is about 18 M actions, roughly $800 per month, plus storage
and fairness surcharges (temporal-platform.md §10).

## Interfaces

- **Consumed:** C-STATE §2 (projection volume), §3 (carry ≤ 256 KiB); Temporal limits (verified, V01-10).
- **Provided:** sizing input to WS-10 (Temporal persistence) and WS-08 (history export volume).
