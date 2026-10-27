# Steer, pause, cancel, take over

All four are Temporal Updates sent by control-plane-api through the outbox (C-MSG §2.1, architecture.md
§2.4). Every action shows three states: **submitting** (button label "Recording…"), **result**
(inline confirmation with the outcome), **recording** (202: "Still recording; you can leave this
page", outcome arrives by SSE and in the notification feed).

## Steer (`S`)

```
┌ Steer job_01m5p7x7r0 ──────────────────────────────────────────────┐
│ Message (sent to the agent as a user message, not as instructions  │
│ to the replicant)                                                    │
│ ┌────────────────────────────────────────────────────────────────┐ │
│ │ Use the existing Proration helper in ledger/fx …               │ │
│ └────────────────────────────────────────────────────────────────┘ │
│ Delivery  (•) Auto: live to implementer (supports live steering)   │ (1)
│           ( ) Next node only    ( ) Live only (fail if not possible)│
│ 16,000 chars max · visible to everyone who can see the job          │
│                                              [Cancel] [Send ⌘↵]    │
└────────────────────────────────────────────────────────────────────┘
```

1. The "Auto" line states what will happen, computed from the active run's agent definition
   (`steering.live`) via `GET /v1/jobs/{id}` `live_run`.
Results: `delivered_live` → timeline "delivered live to run_…"; `queued_next_node` → "will be
included in the next node's inputs"; `not_deliverable` (live_only) → "No live run accepts messages
now; nothing was sent"; `denied` → reason in words.

## Pause (`P` at next node, `⇧P` at next gate)

Confirmation popover: "Pause at the next node boundary? The current run (implementer, 14 min) finishes
first; nothing is cancelled." / "Pause at the next gate? Work continues until a gate opens; approvers
are not notified while paused." Result: status `paused` banner with Resume. `no_op` when already paused.

## Cancel (`⇧C`)

```
┌ Cancel job_01m5p7x7r0? ────────────────────────────────────────────┐
│ • The running implementer run is cancelled (partial artifacts kept) │
│ • The draft PR #1440, if any, is closed with a comment             │
│ • Open gates are cancelled in Slack and GitHub                     │
│ Reason (shown on the issue and in the timeline)                    │
│ ┌────────────────────────────────────────────────────────────────┐ │
│ └────────────────────────────────────────────────────────────────┘ │
│                                       [Keep running] [Cancel job]  │ (danger button, not accent)
└────────────────────────────────────────────────────────────────────┘
```

## Take over (`T`)

```
┌ Take over job_01m5p7x7r0 ──────────────────────────────────────────┐
│ You will get:                                                      │
│ [x] Branch replicant/job_01m5p7x7r0… pushed with current changes     │
│ [x] Workspace archive (≈ 1.4 GiB, available until Nov 7)           │ (1)
│ [x] Transcripts   [x] Plan   [x] Handoff document                  │
│ Replicant stops working on this job. Status becomes taken_over.  │
│ Draft PR: ( ) none  (•) open a draft PR from the branch            │
│                                        [Cancel] [Take over]        │
└────────────────────────────────────────────────────────────────────┘
After: package page /jobs/:id/takeover/:tkoId
┌ Takeover package tko_01m5… · ready ────────────────────────────────┐
│ Branch      acme-payments/ledger-api  replicant/job_01m5p7x7r0…  ↗  │
│ Archive     workspace.tar.zst · 1.38 GiB · sha256 4c1e… [Download] │ (2)
│ Handoff     HANDOFF.md [View]   Plan PLAN.md [View]                │
│ Transcripts 3 runs [View]                                          │
│ Continue locally:                                                  │
│   replicant takeover job_01m5p7x7r0 --dir ./ledger-api               │
│   # or: git fetch origin replicant/job_01m5p7x7r0… && git switch …   │
└────────────────────────────────────────────────────────────────────┘
```

1. The requester or a holder of `job:control:any` can take over. The workspace archive is `raw-restricted` and
   is presigned (15 min, GET only) only to the **principal who passed takeover authorization** (the requester,
   or the tech lead taking over for a deprovisioned requester), with `audit.emit` `job.takeover_archive_presigned
   {job_id, principal, key}` per presign (XD-FIX-R2-026).
   Take over is offered on `running`, `paused`, `waiting_*`, **`failed` and `expired`** jobs while the
   session artifacts are within retention; the dialog shows the retention from the bundle's
   `sessionStateGraceDays` and C-ART `r30d` for the takeover archive ("available until Nov 7"), not a fixed
   number. After the grace it offers the **reconstructed takeover** instead: base SHA + diff bundle + handoff
   (XD-FIX-R2-026). The branch goes to `replicant-takeover/<job_id>`,
   a prefix tenant CI excludes (onboarding.md OB-1); if the workspace diff touches CI-executed paths and the
   tenant has not attested OB-1, only the archive and patch are offered.
2. While the package is being assembled: "Assembling package: waiting for the implementer to upload
   its workspace (up to 5 min)". Failure: "The workspace archive could not be captured (run was lost);
   branch, plan, handoff, and transcripts are available."

## Error and edge states (all actions)

| Case | UI |
|---|---|
| Job became terminal meanwhile | "The job already finished (succeeded at 10:02). Nothing to do." (`no_op`) |
| Permission lost (repo write revoked) | "You no longer have write access to acme-payments/ledger-api." |
| Worker for the job's version unavailable | "Recording…" → after 60 s "Still recording" banner; the outbox keeps the action |
| Double click | Same `action_id`/`message_id` → same result |
