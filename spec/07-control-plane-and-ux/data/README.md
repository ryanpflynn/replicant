# WS-07 data model (Replicant Postgres)

**Constraints:** D-09, D-26 · **Contract:** C-PG (WS-07 is DDL owner of `tenancy`, `identity`, `intake`,
`jobs_read`, `approvals`, `notify`, `github_app`, `slack_app`, `jira_app`)

| File | Schema | Writer role (one writer per schema, C-PG rule 1) | Notes |
|---|---|---|---|
| [ddl/tenancy.sql](ddl/tenancy.sql) | `tenancy` | `svc_cpa_tenancy` (control-plane-api) | Operational config only (ADR-013); versioned settings |
| [ddl/identity.sql](ddl/identity.sql) | `identity` | `svc_identity_service` | SCIM replica, links (incl. `gitea`), role bindings (no `replicant_admin` role: D-25), sessions, delegations, membership history |
| [ddl/intake.sql](ddl/intake.sql) | `intake` | `svc_cpa_intake` (control-plane-api) | JobRequests with `(tenant_id, dedupe_key)` uniqueness; Update outbox; dev seed markers |
| [ddl/jobs_read.sql](ddl/jobs_read.sql) | `jobs_read` | `svc_replicant_projector` | Derived read model with seq guard; requires `pg_trgm` (created by WS-10's migrator) |
| [ddl/approvals.sql](ddl/approvals.sql) | `approvals` | `svc_replicant_projector` | Derived gate projection and inbox eligibility index |
| [ddl/notify.sql](ddl/notify.sql) | `notify` | `svc_cpa_notify` (control-plane-api) | Preferences, delivery log (dedupe), in-app feed |
| [ddl/github_app.sql](ddl/github_app.sql) | `github_app` | `svc_github_integration` | Installations, repos (+ `indexing_enabled` opt-in for indexer broker callers), inbound delivery queue, decision outbox, comment refs |
| [ddl/slack_app.sql](ddl/slack_app.sql) | `slack_app` | `svc_slack_integration` | Message refs per gate, interaction dedupe, decision outbox |
| [ddl/jira_app.sql](ddl/jira_app.sql) | `jira_app` | `svc_jira_integration` | Webhook dedupe, issue ↔ job links |

control-plane-api has one login, `svc_control_plane_api`, and switches with `SET ROLE` to the NOLOGIN module
roles `svc_cpa_intake`, `svc_cpa_tenancy`, `svc_cpa_notify`, `svc_cpa_registry` and `svc_cpa_read` per module
(architecture.md §2.1, WS-10 ADR-WS10-004): the per-module mitigation C-PG names for its "four schemas" risk,
within one connection budget. Grants follow WS-10's grant model v2 (XD-FIX-R1-160; R1V-146).

Validation: `uv run --with pgserver --with 'psycopg[binary]' python data/validate_ddl.py` applies
every file to a throwaway PostgreSQL (bundled by `pgserver`; it lacks contrib, so the trigram index
is skipped there), then checks the projection seq guard, JobRequest dedupe uniqueness, the D-25
role-binding constraint, and the presence of 15 RLS policies. Result on 2026-10-24: **all schemas
applied, smoke checks passed**. WS-10's CI applies the same DDL through Alembic on PostgreSQL 17/18
with `squawk` linting (D-26).

Retention jobs (run by the `migrate` Job's maintenance mode or a named admin operation, never ad hoc
SQL): `github_app.inbound_deliveries` 7 days; `slack_app.*` 90 days after the gate closes;
`intake.job_requests.dedupe_pruned` set 90 days after `terminal_at`; `notify.feed` 90 days;
`identity.sessions` revoked/expired rows 30 days.
