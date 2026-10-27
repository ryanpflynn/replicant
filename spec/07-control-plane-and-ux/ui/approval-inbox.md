# Approval inbox

Route `/approvals[/:gateId]`, views `mine | teams | delegated | decided | release-signoffs`.
Data: `GET /v1/approvals`, `GET /v1/gates/{id}`; SSE topic `inbox`. Prototype:
[approvals.html](../design/prototype/approvals.html). Behaviour: [../approvals.md](../approvals.md).

```
┌ Approvals · 4 waiting on you · 2 more on your teams ─────────────────────────── [⌘K] ◐ ┐
├─────────────────────────────────────────┬──────────────────────────────────────────────────┤
│[Mine 4][My teams 6][Delegated 1][Decided]│ PUBLISH APPROVAL · gate_5fgbq8n2xz · cycle 0     │
│                         Sort: expiry     │ Stale FX rate cache after deploy  [◆ Waiting on you]│
├─────────────────────────────────────────┤ acme-payments/ledger-api #1440 job_01m5n2a9qe     │
│▌◆ Publish · Stale FX rate cache…  5h left│ requested by Maya Torres · opened 18 h ago        │ (1)
│   HIGH ledger-api Maya T. UI Slack GH ESC1│ ⧗ expires Oct 24 14:20 · then auto-cancel        │
│ ◆ Plan · Add idempotency key…     2d 4h │▓ Escalated 6 h ago to #payments-leads. Base moved ▓│ (2)
│   MEDIUM payouts-svc Daniel O. UI Slack  │▓ 2 commits; rebase + QA re-run after approval.    ▓│
│ ◆ Question · Which currency table…3d 1h │ Why this needs a human  policy/risk-rules.paym…@6│ (3)
│ ◆ Budget · Extend to $60 …        6d 2h │ ▲ path.money_movement changed ledger/fx/… HIGH   │
│ 2 low-risk docs gates can be decided    │ ▲ service.tier1 ledger-api is tier 1      HIGH   │
│ together                [Review batch]   │ ◇ diff.size_medium 87 lines (≥ 50)       MEDIUM │
│ ◇ Publish · Fix broken links …       1h │ Plan r2 · approved 08:12 by Priya Raman          │
│ ◇ Publish · Update chargeback FAQ …  3h │ 1. … 2. … 3. …                                    │
│                                         │ Diff 3 files +61 −26          Open full diff D   │
│                                         │ ┌ledger/fx/cache.go +38 −19──────────────────┐  │
│                                         │ │@@ -42,14 +42,21 @@ …                        │  │
│                                         │ └──────────────────────────────────────────────┘  │
│                                         │      QA ✓ Passed · 1,284 tests · 6 screenshots    │ (4)
│                                         │      Cost $11.40 of $40 · Review 2 notes          │
│                                         │      Memory used 3 · read-only                    │
│                                         ├──────────────────────────────────────────────────┤
│                                         │ [Comment (required for Reject / Request changes)] │ (5)
│                                         │ Packet r3 · sha256:9be1…07c2 · also in Slack and  │
│                                         │ on PR #1440 · first decision wins                 │
│                                         │        Reject X  [Request changes R] [Approve A]  │
└─────────────────────────────────────────┴──────────────────────────────────────────────────┘
```

1. Expiry in the attention colour when < 12 h; `ESC n` badge shows escalation level.
2. Banners only for facts that change what the approver should do (escalation, base drift, stale view).
3. Rules fired come from the packet (deterministic rule IDs and evidence); "LLM raised" is shown
   explicitly when true.
4. On ≥ 1440 px the evidence column sits beside the packet; at 1024 px it stacks below.
5. Decision bar states: components.md §8 (idle, submitting, accepted, superseded, denied, stale view,
   closed).

## Release sign-offs view (release managers, security)

Same layout. List items: "Sign-off · 50% → full · rb_k7wq0f2e9n · 1 of 2 signers". Packet: WS-06
evidence packet (stage history, guardrail values R1–R10 with P(worse), online deltas, change classes,
change author). Buttons: Approve / Reject; the bar states "You are excluded: you authored this change"
when applicable (validator `rejected_self_signoff`), and "Needs a second signer from
replicant-release-managers or replicant-security" after the first signature. Sent as Update
`release.signoff` via `POST /v1/releases/pipelines/{id}/signoffs` with `presented_digest`.

## Batch view

`Review batch` opens a list of the batchable gates with each packet's summary and digest; "Approve all
2" sends two `gate.decide` Updates (two `decision_id`s); per-item results appear inline (one may be
`superseded`).

## States

| State | Rendering |
|---|---|
| Loading | List skeleton (6 rows), packet skeleton |
| Empty (mine) | "Nothing is waiting on you. Gates for your teams: 6 (view)." |
| Empty (all) | "No open approvals for your teams." |
| Gate decided while open on screen | Bar replaced by the outcome; item collapses from the list after 2 s |
| Packet revised while open | Stale banner with "what changed" (sections diff), buttons re-enable after review |
| Not eligible but can read the job | Read-only packet with "You can't decide this: not in ledger-core / payments-approvers" |
| Error loading packet | Region error with retry; list still works |
