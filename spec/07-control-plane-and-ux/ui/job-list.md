# Job list

Route `/jobs` (`?view=mine|needs_human|running|pr_review|failed_24h`, `?q=` query language).
Data: `GET /v1/jobs` (projection), SSE topic `jobs:<tenant>`. Permission `job:read`; rows filtered to
tenants the caller belongs to and repos they can read.

```
┌ Jobs · tenant payments · 148 open ───────────────────────── [Jump to… ⌘K] ◐ ┐
│[Open 148][Needs a human 17][Running 42][In PR review 71][Recent 7d][Failed 24h 9]   status:open repo:ledger-* sort:updated▾ [New job N] │ (1)(2)
├────────────────┬──────────────────────────────┬──────────────────┬───────────────────┬──────────────┬──────────────┬───────┬───────┤
│ JOB            │ TITLE                        │ STATUS           │ PHASE / NODE      │ REPO         │ REQUESTER    │  COST │UPDATED▾│ (3)
├────────────────┼──────────────────────────────┼──────────────────┼───────────────────┼──────────────┼──────────────┼───────┼───────┤
│ job_01m5p7x7r0 │ Refund ledger entries double…│ ◔ Running        │ implementing · im…│ ledger-api   │ Maya Torres  │ $2.86 │   8 s │ (4)
│ job_01m5n2a9qe │ Stale FX rate cache after de…│ ◆ Waiting on hum…│ publish_gate · 5h │ ledger-api   │ Maya Torres  │$11.40 │  18 h │ (5)
│ job_01m5g9yb2k │ Bump golang.org/x/net to v0.…│ ◑ In PR review   │ pr_review · #1437 │ ledger-api   │ svc_scheduler│ $1.38 │   3 h │ (6)
│ job_01m5q1v7tn │ Chargeback dispute webhook d…│ ◔ Running        │ waiting: run quota│ chargebacks… │ Priya Raman  │ $0.00 │  41 m │ (7)
│ …                                                                                                                                    │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 148 open jobs · loads as you scroll · terminal jobs are under Recent · J K move · ↵ open · / filter · X select                     │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

1. Chips are saved views with live counts (from the same query with `count`); the URL holds the view. **Open**
   means non-terminal (`jobs_read.jobs.is_terminal = false`); terminal rows live under **Recent** (R2-035).
   Columns show a sort caret on the active sort; the list is virtualised with infinite scroll and a visible
   "148 jobs" count. Column priority (R2-029): Job, Title, Status, Phase stay; Repo, Requester, Cost,
   Updated drop in that order below 1200 px into the row's second line rather than clipping.
2. Query language: `status:`, `phase:`, `repo:` (glob), `requester:me|<handle>`, `bundle:rb_…`,
   `workflow:`, `risk:`, `cost:>10`, `has:pr`, free text over titles (pg_trgm).
3. Columns are fixed; "Comfortable" density (32 px) is a preference.
4. Running rows update live (SSE `job.updated` → refetch row). Rows never reorder while the pointer is
   over the table (prevents mis-clicks); a "3 updates" pill appears instead.
5. Waiting-on-human rows show the gate kind and the time to expiry; the accent diamond marks them.
6. Service-triggered jobs show the service and, on hover, "on behalf of Lena Fischer".
7. A job waiting for capacity stays `Running` (C-IDS status; only `JobSnapshot.waiting` is set) and the phase cell says "waiting: run quota 12/12" or "waiting: cluster capacity" (R2-020, R1V-142). Failed rows (under Recent or Failed 24h) show the failure reason in words with the CRD reason in mono on hover.

## States

| State | Rendering |
|---|---|
| Loading | Skeleton rows (8) at real column widths, no shimmer |
| Empty (no jobs ever in tenant) | "No jobs in payments yet. Start one with N, or label a GitHub issue `replicant` in an enabled repository (Tenant settings → Repositories)." |
| Empty (filter) | "No jobs match `status:failed repo:ledger-*`. Esc clears filters." |
| Error | Inline banner: "Couldn't load jobs (503 · request 01m5pq…). Retry" ; chips stay |
| Stale | Banner "Showing data from 2 min ago; Replicant is catching up" (lag header > 60 s) |
| No tenant membership | "You are not in any Replicant tenant. Tenant access comes from Okta groups (ask your team lead)." |
