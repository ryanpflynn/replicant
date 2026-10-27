# Information architecture and screens

**Owner:** WS-07 · Rendered prototype: [../design/prototype/](../design/prototype/index.html) ·
Components and shared states: [../design/components.md](../design/components.md) (§9 defines the
shared empty / loading / error / stale / forbidden patterns every screen uses).

## Screens

| Screen | Wireframes | Prototype |
|---|---|---|
| Job list | [job-list.md](job-list.md) | [job-list.html](../design/prototype/job-list.html) |
| Job detail (timeline, plan, diff, QA, runs, transcripts, artifacts, memory used, observe links) | [job-detail.md](job-detail.md) | [job-detail.html](../design/prototype/job-detail.html) |
| Steer, pause, cancel, take over | [job-actions.md](job-actions.md) | palette state in job-detail |
| Approval inbox (gates and release sign-offs) | [approval-inbox.md](approval-inbox.md) | [approvals.html](../design/prototype/approvals.html) |
| Releases and versions | [releases.md](releases.md) | [releases.html](../design/prototype/releases.html) (+ rollback dialog) |
| Registries (agent, environment, graph, policy, node catalog; right-sizing proposals) | [registries.md](registries.md) | — |
| Memory curation (replicant-admins only) and the non-admin views | [memory.md](memory.md) | [memory.html](../design/prototype/memory.html), [memory-denied.html](../design/prototype/memory-denied.html) |
| Cost and quotas | [cost-quotas.md](cost-quotas.md) | — |
| Admin (tenants, approval channels, identity links, database/migrations, operations) and tenant settings | [admin.md](admin.md) | — |

## Navigation

The rail is computed from `GET /v1/me` `capabilities` and tenant roles. Hiding is UX only; every
route is enforced by the API (identity.md §8).

```
Non-admin engineer / approver            Replicant admin (+ operator)            Tenant admin (not replicant admin)
┌──────────────────────────┐             ┌──────────────────────────┐          ┌──────────────────────────┐
│ ▣ replicant          prod  │             │ ▣ replicant          prod  │          │ ▣ replicant          prod  │
│ WORK                     │             │ WORK                     │          │ WORK                     │
│  ≡ Jobs             148  │             │  ≡ Jobs             148  │          │  ≡ Jobs             148  │
│  ○ My jobs            6  │             │  ○ My jobs            2  │          │  ○ My jobs            3  │
│  ◇ Approvals         [4] │ ← accent    │  ◇ Approvals         [1] │          │  ◇ Approvals         [2] │
│ PLATFORM                 │             │ PLATFORM                 │          │ PLATFORM                 │
│  ▥ Releases (read)       │             │  ▥ Releases              │          │  ▥ Releases (read)       │
│  ▦ Registries (read)     │             │  ▦ Registries            │          │  ▦ Registries (read)     │
│  $ Cost & quotas         │             │  $ Cost & quotas         │          │  $ Cost & quotas         │
│ TENANT                   │             │ REPLICANT ADMIN            │ ← only   │ TENANT                   │
│  ✻ Tenant settings (ro)  │             │  ▤ Memory            23  │ memory:  │  ✻ Tenant settings (edit)│
│                          │             │  ✻ Admin                 │ admin:   │                          │
│                          │             │ OPERATORS                │ platform │ (no Memory item:         │
│                          │             │  ◷ Temporal UI ↗         │          │  D-25, tenant admins get │
│                          │             │                          │          │  no memory tools)        │
└──────────────────────────┘             └──────────────────────────┘          └──────────────────────────┘
```

Annotations:
1. The Approvals count is the only accent-coloured element in the rail (direction.md).
2. The "Replicant admin" group's **Memory** item renders only if `capabilities` contains `memory:admin`
   (membership in `replicant-admins` at request time). The **Admin** item renders only with `admin:platform`
   or `identity:admin` (R1-183); the two are independent, so a memory curator need not be a platform admin
   and vice versa. A user removed from the group sees it disappear on the next
   `GET /v1/me` (sent on focus and every 5 min) and gets 403 on any memory request immediately.
3. "Operators → Temporal UI" renders only for `temporal:ui`.
4. Below 1200 px the rail collapses to icons; counts become a small badge on the Approvals icon only.

## Global elements

| Element | Behaviour |
|---|---|
| Top bar | Breadcrumb with `Id` components; ⌘K search; theme toggle; live-status dot (filled = SSE connected, hollow = reconnecting) |
| Environment tag | `prod` (red outline), `staging`, `local` |
| Banners | Projection lag > 60 s; tenant kill switch active; bundle revoked notice on jobs; maintenance |
| Keyboard | components.md §12 |
| Deep links | Every object has a stable URL: `/jobs/:jobId`, `/approvals/:gateId`, `/releases/bundles/:rbId`, `/admin/memory/entries/:memId` |
