-- Schema tenancy. Writer: control-plane-api (module role svc_cpa_tenancy). DDL owner: WS-07 (WS-09 reviews).
-- Operational configuration only (ADR-013). Reviewed target DDL; SQLAlchemy models + Alembic must produce it (D-26).
-- RLS uses WS-10's ops.apply_tenant_rls() and the canonical principal_scope vocabulary (C-PG 1.2.0 rule 4).
-- CHECK constraints are named by the replicant-db naming convention ck_<table>_<name> at Alembic generation (XD-10-04).
CREATE SCHEMA IF NOT EXISTS tenancy;

CREATE TABLE tenancy.tenants (
    tenant_id        text COLLATE "C" PRIMARY KEY
                     CONSTRAINT ck_tenants_tenant_id CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
    display_name     text NOT NULL CONSTRAINT ck_tenants_display_name CHECK (length(display_name) <= 100),
    status           text NOT NULL DEFAULT 'onboarding'
                     CONSTRAINT ck_tenants_status CHECK (status IN ('onboarding', 'active', 'suspended', 'offboarding', 'offboarded')),
    release_ring     text NOT NULL DEFAULT 'standard' CONSTRAINT ck_tenants_release_ring CHECK (release_ring IN ('early', 'standard', 'conservative')),
    okta_group_prefix text,
    kms_key_alias    text,                         -- alias/replicant/<env>/tenant/<tenant_id> (WS-09/WS-10)
    onboarding_checklist jsonb NOT NULL DEFAULT '{}'::jsonb,   -- onboarding.md items with completion evidence (R2-001 CI exclusion etc.)
    created_at       timestamptz NOT NULL DEFAULT now(),
    created_by       text NOT NULL CONSTRAINT ck_tenants_created_by CHECK (created_by ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
    offboarded_at    timestamptz,                  -- tombstone: tenant_id is never reused (C-IDS)
    CONSTRAINT ck_tenants_offboarded CHECK ((status = 'offboarded') = (offboarded_at IS NOT NULL))
);

-- One current row per tenant plus full history (versioned; optimistic concurrency via version / If-Match).
-- settings.quotas.monthly_budget_usd and daily_budget_usd are REQUIRED (schemas/tenant-settings.schema.json, R2-055).
CREATE TABLE tenancy.tenant_settings (
    tenant_id   text COLLATE "C" NOT NULL REFERENCES tenancy.tenants (tenant_id),
    version     integer NOT NULL CONSTRAINT ck_tenant_settings_version CHECK (version >= 1),
    settings    jsonb NOT NULL,
    is_current  boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL,
    reason      text,
    CONSTRAINT ck_tenant_settings_budget_present CHECK (
        settings ? 'quotas'
        AND (settings -> 'quotas') ? 'monthly_budget_usd'
        AND (settings -> 'quotas') ? 'daily_budget_usd'),
    PRIMARY KEY (tenant_id, version)
);
CREATE UNIQUE INDEX tenant_settings_current_uq ON tenancy.tenant_settings (tenant_id) WHERE is_current;

-- Platform caps and defaults that bound tenant-set quotas (R2-055). Seeded by this migration; edited
-- only through PUT /v1/admin/platform-caps (admin:platform, audited).
CREATE TABLE tenancy.platform_caps (
    cap_name   text PRIMARY KEY CONSTRAINT ck_platform_caps_name CHECK (cap_name ~ '^[a-z][a-z0-9_]{2,63}$'),
    value      numeric NOT NULL CONSTRAINT ck_platform_caps_value CHECK (value >= 0),
    unit       text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL
);
INSERT INTO tenancy.platform_caps (cap_name, value, unit, updated_by) VALUES
    ('tenant_monthly_usd_default', 5000,  'usd',  'svc_migrate'),   -- applied at onboarding when a tenant admin sets nothing
    ('tenant_monthly_usd_max',     50000, 'usd',  'svc_migrate'),   -- ceiling a tenant admin may set
    ('tenant_daily_usd_default',   400,   'usd',  'svc_migrate'),
    ('tenant_daily_usd_max',       4000,  'usd',  'svc_migrate'),
    ('platform_daily_usd_max',     30000, 'usd',  'svc_migrate'),   -- platform-wide daily ceiling across tenants
    ('platform_monthly_usd_max',   600000,'usd',  'svc_migrate'),
    ('tenant_concurrent_runs_max', 40,    'runs', 'svc_migrate'),
    ('pr_repair_jobs_per_pr_max',  3,     'jobs', 'svc_migrate')    -- R2-059 floor if repair_limits omits it
ON CONFLICT (cap_name) DO NOTHING;

-- Kill switches (R2-016). Engage/release order: S3 mirror (s3://acme-replicant-<env>-ops/kill-switches/v1/state.json,
-- schema replicant.kill-switches/v1, plus ConfigMap replicant-workers/replicant-kill-switches), then the Temporal batch
-- Signal ops.kill_switch, then this table (WS-09 secrets.md §12, XD-FIX-R2-016). Readers use the mirror; this table
-- is the record and the UI's source. One row per (scope, scope_key, mode). Rows written after a Postgres outage
-- or by the break-glass CLI are back-filled by the reconciler (source = 'out_of_band_backfill').
CREATE TABLE tenancy.kill_switches (
    switch_id   text PRIMARY KEY,                 -- '<scope>:<key>:<mode>', e.g. 'global:*:starts', 'tenant:payments:publishing'
    scope       text NOT NULL CONSTRAINT ck_kill_switches_scope CHECK (scope IN ('global', 'tenant', 'repo', 'bundle')),
    scope_key   text NOT NULL,                    -- '*', tenant_id, '<host>/<owner>/<name>', release_bundle_id
    mode        text NOT NULL CONSTRAINT ck_kill_switches_mode CHECK (mode IN ('intake', 'starts', 'runs', 'publishing')),
    tenant_id   text COLLATE "C",                 -- set for tenant and repo scope (RLS visibility); NULL for global/bundle
    engaged     boolean NOT NULL,
    intake_state text CONSTRAINT ck_kill_switches_intake_state CHECK (intake_state IS NULL OR intake_state IN ('paused', 'closed')),
    reason_code text NOT NULL CONSTRAINT ck_kill_switches_reason CHECK (reason_code IN ('incident', 'security', 'cost', 'drill', 'maintenance')),
    note        text CONSTRAINT ck_kill_switches_note_len CHECK (note IS NULL OR length(note) <= 2000),
    notice_id   text NOT NULL CONSTRAINT ck_kill_switches_notice CHECK (notice_id ~ '^ntc_[0-9a-hjkmnp-tv-z]{26}$'),
    issued_by   text NOT NULL,
    issued_at   timestamptz NOT NULL,
    source      text NOT NULL DEFAULT 'api' CONSTRAINT ck_kill_switches_source CHECK (source IN ('api', 'out_of_band_backfill')),
    mirror_generation   bigint NOT NULL,          -- generation of state.json that carried this state
    signal_batch_job_id text,                     -- Temporal batch operation id
    CONSTRAINT ck_kill_switches_id CHECK (switch_id = scope || ':' || scope_key || ':' || mode),
    CONSTRAINT ck_kill_switches_intake_mode CHECK ((mode = 'intake') = (intake_state IS NOT NULL) OR NOT engaged)
);

-- Schedules are owned by Temporal (C-STATE); this is only the ownership/audit index for the UI.
-- ID form per C-IDS: Temporal Schedule ID sch_<ulid> (CCR-07-08 registers the prefix with a ULID body).
CREATE TABLE tenancy.schedule_index (
    schedule_id text COLLATE "C" PRIMARY KEY CONSTRAINT ck_schedule_index_id CHECK (schedule_id ~ '^sch_[0-9a-hjkmnp-tv-z]{26}$'),
    display_name text NOT NULL,
    tenant_id   text COLLATE "C" NOT NULL REFERENCES tenancy.tenants (tenant_id),
    owner       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);

SELECT ops.apply_tenant_rls('tenancy.tenant_settings');
SELECT ops.apply_tenant_rls('tenancy.schedule_index');
-- XD-FIX-R1-160 / INDEX.md §4.1: tenants and kill_switches carry tenant_id, so RLS is enabled and forced.
-- tenants: a service sees its own tenant's row; operators read all through r_cross_tenant_read; tenant
-- onboarding (svc_cpa_tenancy, the only writer) needs every row.
SELECT ops.apply_tenant_rls('tenancy.tenants');
CREATE POLICY rls_tenants_sole_writer ON tenancy.tenants AS PERMISSIVE FOR ALL TO svc_cpa_tenancy
    USING (true) WITH CHECK (true);
-- kill_switches: global and bundle rows have tenant_id NULL and must be visible to every enforcing service;
-- tenant and repo rows only to that tenant. The only writer is control-plane-api's tenancy module.
SELECT ops.apply_tenant_rls('tenancy.kill_switches');
CREATE POLICY rls_kill_switches_platform_read ON tenancy.kill_switches AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (tenant_id IS NULL);
CREATE POLICY rls_kill_switches_sole_writer ON tenancy.kill_switches AS PERMISSIVE FOR ALL TO svc_cpa_tenancy
    USING (true) WITH CHECK (true);

-- Grant model v2 (XD-FIX-R1-160, C-PG 2.0.0): WS-10 owns roles, logins and schema USAGE; this file grants on
-- parent tables only (never ON ALL TABLES, never on a partition, no default privileges): reads to
-- r_<schema>_read, DML to r_<schema>_write, column-narrowed reads to a named login. Never to the login
-- svc_control_plane_api (its modules inherit r_<schema>_write; svc_cpa_read inherits the read roles).
GRANT SELECT ON tenancy.tenants, tenancy.tenant_settings, tenancy.schedule_index, tenancy.platform_caps,
                tenancy.kill_switches TO r_tenancy_read;
GRANT SELECT, INSERT, UPDATE ON tenancy.tenants, tenancy.tenant_settings, tenancy.schedule_index, tenancy.platform_caps,
                tenancy.kill_switches TO r_tenancy_write;
-- No grafana_ro grant on tenancy (C-PG rule 6 does not list it): Grafana reads the gauges control-plane-api
-- exports instead, replicant_tenants{status}, replicant_kill_switches_engaged{kind,type} (kind = scope, type = mode) and
-- replicant_platform_cap_usd{kind} (kind = cap name); C-IDS §11 label keys only (WS-08 catalogue, XD-W3-R1V-054).
