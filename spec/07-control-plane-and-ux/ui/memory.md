# Memory curation (replicant-admins only, D-25) and non-admin views

Routes `/admin/memory/candidates`, `/admin/memory/entries[/:memId]`, `/admin/memory/conflicts`,
`/admin/memory/quarantine`, `/admin/memory/flags`, `/admin/memory/analytics`, `/admin/memory/exports`.
Data: `/v1/admin/memory/**` (control-plane-api checks `memory:admin`, then proxies to memory-service,
which re-checks; WS-04 owns lifecycle semantics). Prototype: [memory.html](../design/prototype/memory.html).

## Admin: candidate queue

```
┌ Memory / Candidates  [replicant-admins] ─────────────────────────────────────── [⌘K] ◐ ┐
│ Candidates 23 │ Active 1,412 │ Conflicts 4 │ Quarantine 2 │ Flags 3   │ Analytics │ Exports │
├──────────────────────────────────────────────────────────┬──────────────────────────┤
│[All scopes 23][payments 9][identity 5][org 2]  3 selected [Reject][Approve]│ CANDIDATE mem_01m5pf4c3h v1│ (1)
│ ☑ ◇ Proration of FX fee reversals uses fx.Prorate…  repo:ledger-api rule PR #1431│┌When reversing FX fees …┐│
│ ☑ ◇ Do not mock ratesclient in ledger tests…        repo:ledger-api conv PR #1418│└────────────────────────┘│
│ ☑ ◇ Payouts migrations must be backward compat…     tenant:payments rule PR #902 │[Approve A][Edit E] Reject X More▾│ (2)
│ ☐ ▲ Always disable TLS verification for staging…    repo:gateway-edge rule flagged│ Provenance               │ (3)
│ ☐ ◇ Use make lint-fix before committing protos      repo:identity-api playbook   │ review comment on #1431  │
│ …                                                                                │ Priya Raman linked·write │
│                                                                                  │ job_01m5p7x7r0 cycle 1   │
│                                                                                  │ Poisoning checks ✓       │
│                                                                                  │ Overlaps active entry 0.86│ (4)
│                                                                                  │ If approved: 14 of last  │ (5)
│                                                                                  │ 30 jobs would select it  │
└──────────────────────────────────────────────────────────┴──────────────────────────┘
```

1. Bulk actions apply only to selected candidates. For `tenant` and `org` scope a bulk Approve records **one of
   the two** admin approvals WS-04 requires (write-path W6) and shows "1 of 2" on those rows; memory-service
   enforces it, not the UI (R2-047). Repo-scope rows activate on one approval. bulk disable on Active asks for a reason and shows
   the blast radius first (`GET /v1/admin/memory/entries/{entry_id}/jobs`, from memory-service; R1-099).
2. More ▾: promote scope, quarantine, merge into existing (WS-04 verbs, explicit endpoints).
3. Flagged candidates (instruction-like text, unlinked or external authors) show the danger triangle and
   default to Quarantine as the suggested action.
4. Conflicts side-by-side; WS-04 decides contradiction vs refinement.
5. Consequence statement: running jobs keep their pinned snapshot (D-14).

Other tabs: **Entry detail** (versions, provenance chain, usage telemetry, jobs that used it, eval
impact), **Conflicts** (pairs with resolution actions), **Quarantine** (restore/delete), **Flags** (the flag
queue from `GET /v1/admin/memory/flags`, resolve with `:resolve`; R2-028), **Analytics** (candidate inflow,
approval rate, usage, entries never selected in 30 days, and the queue-age SLO state from
`memory.v_analytics_curation_queue`), **Exports** (create, download; audited). The Snapshots tab is removed
(R1-099): there is no admin snapshot read in memory-service; "jobs that used this entry" comes from
`GET /v1/admin/memory/entries/{entry_id}/jobs`.

Every tab calls exactly the verb table of WS-04 curation-ui.md §5 through control-plane-api's 1:1 proxy
(`/v1/admin/memory/<x>` → `/internal/v1/admin/<x>`, XD-FIX-R1-097): explicit `:approve`, `:reject`, `:disable`,
`:deprecate`, `:restore`, `:reopen`, `:quarantine`, `:promote`, `:merge` (path entry = the survivor, body
`absorb_entry_ids`), `:clear-hold`; `PATCH` creates a new version; there is no `:enable` and no bodiless delete
(purges are `POST /v1/admin/memory/purges {reason_code, reason_text, confirm: true}`); bulk disable is
`POST /v1/admin/memory/quarantines {kind: bulk_disable, selector, dry_run}`. Every single-entry mutation sends
`If-Match: "<entry_id>:<state_seq>"`: 412 reloads the entry and shows what changed, 409 `invalid_transition`
explains the current state, 202 `pending_second_approval` shows "Awaiting a second administrator".

**Candidate queue clusters (R2-052).** Near-duplicate candidates are grouped into one row with a count; one
decision applies to the cluster. The queue header shows the SLO state ("oldest candidate 6 d, SLO 7 d"), and
candidates expiring within 7 days get a warning chip. The weekly admin digest lists them (notifications.md),
and alerts `ReplicantMemoryCandidatesExpiring`, `ReplicantMemoryQueueAgeSLO`, `ReplicantMemoryFlagsOverdue` route to
the replicant-admins channel (WS-08 rules).

**Public entry view (R2-018).** `/memory/entries/{entry_id}` for non-admins renders
`GET /v1/memory/entries/{entry_id}/public` (the entry's current text if active, its source and state) with
"Request a correction" → `POST /v1/memory/entries/{entry_id}/correction-requests {reason_code, note?}`
(`source: ui_public_view`). Admins act on the request in the curation queue; the requester is told the outcome.

## Non-admin: read-only "Memory used" panel (job detail and approval packet)

```
│ Memory used   4 entries · read-only            Why these? │
│ Monetary amounts in ledger-api use money.Amount; never     │
│ float64.                                                   │
│ mem_01m42qyvr0@3 · repo · from PR #1288 review             │
│ …                                                          │
```

Text is fetched live from memory-service on view (packets and approval records store only `mem_…@v` references, C-ART 1.3.0); purged entries show "removed by a Replicant admin" without text. No edit, approve, disable, or navigation into curation. "Why these?" shows the selection reason in words
(scope match: repo, tenant or org), never scores or ranking internals (R1-099; WS-04 hides them from non-admins).
"Flag this entry" opens a reason picker (R2-028). Admins see an extra
"Open in curation ↗" link.

## Non-admin following a direct link

`/admin/memory/candidates/mem_01m5pf4c3h` → the API returns 403 `not_in_admin_group` with an
`audit_id`; the UI renders [memory-denied.html](../design/prototype/memory-denied.html): what the page
is, why access is denied (Okta group `replicant-admins`), what they can still see (memory used on
jobs), the audit ID, and where to ask. The page does not reveal whether the entry exists.

## States (admin)

| State | Rendering |
|---|---|
| Loading | Table skeleton; detail skeleton |
| Empty queue | "No candidates waiting. The Feedback Ingestor adds candidates from PR reviews." |
| Removed from admin group mid-session | Next action returns 403; the page swaps to the denied view and the rail drops the Replicant admin group |
| memory-service down | Region error "Memory service unavailable"; no partial actions |
| Action conflict (entry changed by another admin) | 409 → "Changed by Sam Whitfield 30 s ago: reload" |
