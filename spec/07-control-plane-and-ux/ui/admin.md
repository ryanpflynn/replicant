# Admin and tenant settings

## Tenant settings (`/tenant/:tenantId/settings`; edit = `tenant:ops:edit`, view = tenant members)

```
┌ Tenant settings · payments ──────────────────────────────────────────────────── [⌘K] ◐ ┐
│ General │ Approval channels │ Repositories │ Jira │ Schedules │ Policies in force │ Quotas│
├───────────────────────────────────────────────────────────────────────────────────────┤
│ Approval channels (operational; channel types come from the bundle's approval_channels)│ (1)
│ Slack approval channel     #payments-replicant (C07PAYFAC1)         [Change]            │
│ Escalation 1               #payments-leads   (C07PAYLEAD)                             │
│ Escalation 2               #secops-oncall    (C02SECOPS1)  critical only              │
│ GitHub escalation team     @acme-payments/ledger-core                                 │
│ Suggested approver         rotate by least recent decision                            │
│ [Test delivery]  posts a test message to each channel and reports permissions         │ (2)
├───────────────────────────────────────────────────────────────────────────────────────┤
│ Repositories  14 installed · 9 enabled                                                │
│ REPO                      ENABLED TRIGGERS          LABEL    TYPES ALLOWED   INDEXING │ (3)
│ acme-payments/ledger-api   ●      label, command    replicant  auto,bugfix,…   on       │
│ acme-payments/payouts-svc  ○      —                 —        —               off      │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ Policies in force (read-only here; edited in the config repo, tenant layer)           │ (4)
│ risk-rules.payments @6 · gate-rules (global @17) · approval-channels (global @5)      │
│ [Open tenant layer in config repo ↗]  [Preview a change: paste YAML → validate]       │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

1. ADR-013 split stated on the screen to avoid confusion about why some settings live elsewhere.
2. Test delivery uses slack-integration; reports `not_in_channel`/missing scopes in words.
3. Repo trigger settings are written by github-integration (`github_app.repo_settings`) via proxy;
   INDEXING toggles opt-in for zoekt/ecosystem broker callers (CCR-09-06).
4. Behavioural policy is never edited live (D-03); the preview calls `POST /v1/registry/validate`,
   which reports the ADR-013 tightening check.

## Platform admin (`/admin`; `admin:platform`, `identity:admin`, `audit:read`)

| Tab | Content | API |
|---|---|---|
| Tenants | List, create, offboard (tombstone) | `/v1/admin/tenants/**` |
| Kill switches | One table across scopes `global`, `tenant`, `repo`, `bundle` with modes `intake` (open/paused/closed), `starts`, `runs`, `publishing` (open/paused): state, `reason_code`, note, who, when, mirror generation and Signal batch status; engage/release dialog with `reason_code` (incident, security, cost, drill, maintenance) and a note; a red global banner while any global or tenant switch is engaged. Permission `ops:kill-switch` (tenant admins see and set only their tenant and repos). Architecture §2.9 (R1V-151) | `GET /v1/admin/kill-switches`, `PUT /v1/admin/kill-switches/{scope}/{scope_value}` |
| Access | Role bindings (group → role per tenant / platform), Replicant admin group name, versions with diffs | `/v1/admin/role-bindings` |
| Identity | Link status counts (active, pending, conflict, suspended), conflict resolution, unlink, SCIM sync health (last push, errors), event hook health | `/v1/admin/identity/links` |
| Database | Migration status: current revision, expected per service, pending, history with durations; projection health (lag histogram, last reconcile), projection rebuild; outbox inspection | `/v1/admin/migrations`, `/v1/admin/ops/**` |
| Operations | Named operations catalogue; dry run → run with reason; history with row counts and audit links | `/v1/admin/ops/operations` |
| Audit | Filterable audit view (actor, type, resource, time), export | `/v1/audit` (WS-08 views) |

```
┌ Admin / Database ──────────────────────────────────────────────────────────── [⌘K] ◐ ┐
│ Schema revision  a7c3e91f42d0 (head) · migrated 2026-10-24 06:02 by migrate Job (Argo PreSync)│
│ Services         control-plane-api a7c3… ✓  identity-service a7c3… ✓  github-integration a7c3… ✓ … │
│ Pending          none                                                                │
│ History          a7c3e91f42d0  add gate_eligibility.since       2026-10-24  1.8 s     │
│                  9b1f0c22e7a5  jobs_read trigram index (CONCURRENTLY) 2026-10-20 41 s │
│ Projections      lag p99 1.4 s · last reconcile 10:20:02 · 0 mismatches   [Rebuild…]  │
│ Outboxes         intake.pending_actions 0 · slack_app 0 · github_app 2 retry          │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

States: Loading skeletons; migration mismatch (danger banner "control-plane-api expects b2…, database is
at a7…: service not ready"); Error (region). No screen offers SQL (D-26).
