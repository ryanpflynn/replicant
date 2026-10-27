-- =============================================================================
-- WS-10 · Schema `ops` (DDL owner: WS-10; writer: migrate Job only; C-PG §2)
-- =============================================================================
-- Contents:
--   ops.alembic_version          Alembic version table (version_table_schema='ops')
--   ops.migration_runs           one row per migrate Job execution (status shown in admin UI)
--   ops.migration_log            one row per applied revision, with compat class and checksum
--   ops.maintenance_runs         partition create/drop, matview refresh, reindex, retention
--   ops.partitioned_tables       reference data: which tables are partitioned and how
--   ops.restore_events           restore + reconcile bookkeeping (recovery.md §3)
--   RLS helpers                  ops.current_tenant(), ops.principal_scope(), ops.apply_tenant_rls(),
--                                ops.secure_partition() (partition-level isolation, XD-ORCH-04)
--   ID helpers                   ops.ulid_floor(prefix, ts) for ULID-range partition bounds
--   Views                        ops.schema_status (metric source for Grafana and control-plane-api)
-- Validated on PostgreSQL 17.11 (podman, 2026-10-24).
-- =============================================================================

\set ON_ERROR_STOP on
-- squawk-ignore-file prefer-text-field, prefer-bigint-over-smallint, prefer-bigint-over-int
-- (ops holds tiny bookkeeping tables written only by the migrate Job; varchar(32) matches
-- Alembic's own version_num type.)
SET ROLE replicant_migrator;

-- Alembic creates this table itself, but with version_num varchar(32). Replicant's revision
-- IDs are "<yyyymmddhhmm>_<schema>" (≤ 32 chars) so the default fits; it is pre-created here so
-- its name, owner and grants are deterministic and so the startup check can rely on it.
CREATE TABLE IF NOT EXISTS ops.alembic_version (
  version_num varchar(32) NOT NULL,
  CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

CREATE TABLE IF NOT EXISTS ops.migration_runs (
  run_seq          bigint GENERATED ALWAYS AS IDENTITY,
  started_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
  finished_at      timestamptz,
  mode             text NOT NULL,
  from_revision    varchar(32),
  target_revision  varchar(32) NOT NULL,
  to_revision      varchar(32),
  outcome          text NOT NULL DEFAULT 'running',
  applied          varchar(32)[] NOT NULL DEFAULT '{}',
  image_digest     text NOT NULL,
  git_sha          text NOT NULL,
  k8s_job          text,
  argocd_app       text,
  argocd_sync_rev  text,
  lock_wait_ms     integer,
  error_class      text,
  error_summary    text,
  CONSTRAINT pk_migration_runs PRIMARY KEY (run_seq),
  CONSTRAINT ck_migration_runs_mode CHECK (mode IN ('apply','maintenance','restore_reconcile','verify')),
  CONSTRAINT ck_migration_runs_outcome CHECK (outcome IN ('running','succeeded','noop','failed',
                                                          'lock_timeout','refused_incompatible',
                                                          'refused_checksum','refused_contract_gate')),
  CONSTRAINT ck_migration_runs_image_digest CHECK (image_digest ~ '^sha256:[0-9a-f]{64}$'),
  CONSTRAINT ck_migration_runs_git_sha CHECK (git_sha ~ '^[0-9a-f]{40}$'),
  CONSTRAINT ck_migration_runs_error_summary_len CHECK (length(error_summary) <= 4000)
);
CREATE INDEX IF NOT EXISTS ix_migration_runs_started_at ON ops.migration_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS ops.migration_log (
  revision                 varchar(32) NOT NULL,
  down_revision            varchar(32),
  schema_name              text NOT NULL,
  compat                   text NOT NULL,
  requires_reader_revision varchar(32),
  description              text NOT NULL,
  sql_sha256               text NOT NULL,
  applied_at               timestamptz NOT NULL DEFAULT clock_timestamp(),
  applied_in_run           bigint NOT NULL,
  duration_ms              integer NOT NULL,
  downgraded_at            timestamptz,
  CONSTRAINT pk_migration_log PRIMARY KEY (revision),
  CONSTRAINT fk_migration_log_applied_in_run_migration_runs FOREIGN KEY (applied_in_run)
    REFERENCES ops.migration_runs (run_seq),
  CONSTRAINT ck_migration_log_compat CHECK (compat IN ('expand','contract','data','reference','grants')),
  CONSTRAINT ck_migration_log_schema CHECK (schema_name IN ('ops','tenancy','identity','intake','registry',
      'jobs_read','approvals','memory','ecosystem','cost','evals','audit','notify','github_app','slack_app',
      'jira_app','qa','multi')),
  CONSTRAINT ck_migration_log_contract_gate CHECK (compat <> 'contract' OR requires_reader_revision IS NOT NULL),
  CONSTRAINT ck_migration_log_sql_sha256 CHECK (sql_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS ops.maintenance_runs (
  run_seq      bigint GENERATED ALWAYS AS IDENTITY,
  task         text NOT NULL,
  target       text NOT NULL,
  started_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
  finished_at  timestamptz,
  outcome      text NOT NULL DEFAULT 'running',
  details      jsonb NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT pk_maintenance_runs PRIMARY KEY (run_seq),
  CONSTRAINT ck_maintenance_runs_task CHECK (task IN ('partition_create','partition_secure','partition_detach','partition_drop',
      'matview_refresh','reindex_concurrently','vacuum_analyze','retention_purge','bloat_scan','grants_verify')),
  CONSTRAINT ck_maintenance_runs_outcome CHECK (outcome IN ('running','succeeded','skipped','failed'))
);
CREATE INDEX IF NOT EXISTS ix_maintenance_runs_task_started_at ON ops.maintenance_runs (task, started_at DESC);

-- Reference data (rows ship as migrations, D-26). The maintenance CronJob reads this table to
-- create partitions ahead of time and to drop expired ones (C-PG rule 3: retention by partition
-- drop, executed only by the migrate image in maintenance mode).
CREATE TABLE IF NOT EXISTS ops.partitioned_tables (
  table_name        text NOT NULL,            -- schema-qualified, e.g. 'audit.events'
  key_kind          text NOT NULL,            -- 'timestamptz' (RANGE on a time column) or 'ulid' (RANGE on a ULID text id)
  key_column        text NOT NULL,
  ulid_prefix       text,                     -- 'job_', 'aud_' ... when key_kind='ulid'
  interval_months   smallint NOT NULL DEFAULT 1,
  premake           smallint NOT NULL DEFAULT 3,
  retain_months     smallint NOT NULL,        -- drop partitions whose upper bound is older than this
  owner_role        text NOT NULL DEFAULT 'replicant_migrator',
  detach_before_drop boolean NOT NULL DEFAULT true,
  export_before_drop boolean NOT NULL DEFAULT false, -- audit: verify S3 export exists before drop
  CONSTRAINT pk_partitioned_tables PRIMARY KEY (table_name),
  CONSTRAINT ck_partitioned_tables_key_kind CHECK (key_kind IN ('timestamptz','ulid')),
  CONSTRAINT ck_partitioned_tables_ulid_prefix CHECK ((key_kind = 'ulid') = (ulid_prefix IS NOT NULL)),
  CONSTRAINT ck_partitioned_tables_retain CHECK (retain_months BETWEEN 1 AND 120),
  CONSTRAINT ck_partitioned_tables_owner CHECK (owner_role IN ('replicant_migrator','audit_owner'))
);

CREATE TABLE IF NOT EXISTS ops.restore_events (
  restore_seq        bigint GENERATED ALWAYS AS IDENTITY,
  detected_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
  restore_point      timestamptz NOT NULL,     -- the PITR target time / snapshot time
  source             text NOT NULL,            -- 'pitr' | 'snapshot' | 'global_failover'
  incident_ref       text NOT NULL,            -- ticket / incident id
  reconcile_state    text NOT NULL DEFAULT 'pending',
  steps              jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{step, state, started_at, finished_at, counts}]
  CONSTRAINT pk_restore_events PRIMARY KEY (restore_seq),
  CONSTRAINT ck_restore_events_source CHECK (source IN ('pitr','snapshot','global_failover','clone')),
  CONSTRAINT ck_restore_events_state CHECK (reconcile_state IN ('pending','running','completed','failed'))
);

-- Read by Grafana (grafana_ro) and control-plane-api; source of replicant_db_* metrics.
CREATE OR REPLACE VIEW ops.schema_status AS
SELECT
  (SELECT version_num FROM ops.alembic_version LIMIT 1)                         AS current_revision,
  (SELECT max(started_at) FROM ops.migration_runs WHERE outcome IN ('succeeded','noop')) AS last_success_at,
  (SELECT row_to_json(r) FROM (SELECT run_seq, mode, outcome, target_revision, started_at, finished_at,
                                      error_class, git_sha
                               FROM ops.migration_runs ORDER BY run_seq DESC LIMIT 1) r)  AS last_run,
  (SELECT count(*) FROM ops.migration_runs
     WHERE outcome NOT IN ('running','succeeded','noop') AND started_at > now() - interval '24 hours') AS failures_24h;

-- ---------------------------------------------------------------------------
-- RLS session-variable protocol (C-PG rule 4; schema-management.md §4)
--   replicant.tenant_id        tenant of the current transaction ('' = none → fail closed)
--   replicant.principal_scope  canonical vocabulary (schema-management.md §4.2):
--                              'tenant' (default)
--                              'operator:all-tenants'  cross-tenant READ, honored only for r_cross_tenant_read
--                              'system:all-tenants'    cross-tenant READ+WRITE, honored only for r_cross_tenant_write
--                              'admin:memory'          D-25 curation inside memory-service (WS-04 policies)
--                              'eval:platform'         eval-worker platform rows (WS-06 policies)
-- Both are set with set_config(..., is_local => true) at transaction start by replicant-db.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops.current_tenant() RETURNS text
  LANGUAGE sql STABLE PARALLEL SAFE
  AS $$ SELECT nullif(current_setting('replicant.tenant_id', true), '') $$;

CREATE OR REPLACE FUNCTION ops.principal_scope() RETURNS text
  LANGUAGE sql STABLE PARALLEL SAFE
  AS $$ SELECT coalesce(nullif(current_setting('replicant.principal_scope', true), ''), 'tenant') $$;

GRANT EXECUTE ON FUNCTION ops.current_tenant(), ops.principal_scope() TO PUBLIC;

-- Applies the standard four policies to a table that has `tenant_id text NOT NULL`.
-- Policies are role-targeted so that, for roles without cross-tenant membership, the planner
-- sees only `tenant_id = ops.current_tenant()` (index-friendly, no OR).
CREATE OR REPLACE FUNCTION ops.apply_tenant_rls(p_table regclass, p_tenant_column name DEFAULT 'tenant_id')
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
  t text;
  base name;
BEGIN
  SELECT format('%I.%I', n.nspname, c.relname), c.relname INTO t, base
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = p_table;
  EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', t);
  EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', t);
  EXECUTE format('DROP POLICY IF EXISTS %I ON %s', 'rls_'||base||'_tenant', t);
  EXECUTE format('DROP POLICY IF EXISTS %I ON %s', 'rls_'||base||'_xt_read', t);
  EXECUTE format('DROP POLICY IF EXISTS %I ON %s', 'rls_'||base||'_xt_write', t);
  EXECUTE format($f$CREATE POLICY %I ON %s AS PERMISSIVE FOR ALL TO PUBLIC
                    USING (%I = ops.current_tenant()) WITH CHECK (%I = ops.current_tenant())$f$,
                 'rls_'||base||'_tenant', t, p_tenant_column, p_tenant_column);
  EXECUTE format($f$CREATE POLICY %I ON %s AS PERMISSIVE FOR SELECT TO r_cross_tenant_read
                    USING (ops.principal_scope() = 'operator:all-tenants')$f$,
                 'rls_'||base||'_xt_read', t);
  EXECUTE format($f$CREATE POLICY %I ON %s AS PERMISSIVE FOR ALL TO r_cross_tenant_write
                    USING (ops.principal_scope() = 'system:all-tenants') WITH CHECK (ops.principal_scope() = 'system:all-tenants')$f$,
                 'rls_'||base||'_xt_write', t);
END
$$;
REVOKE ALL ON FUNCTION ops.apply_tenant_rls(regclass, name) FROM PUBLIC;

-- Partition-level isolation (XD-ORCH-04). RLS on a partitioned parent is not applied when a partition is
-- queried directly, and a GRANT ... ON ALL TABLES IN SCHEMA (or any default privilege) also reaches every
-- partition. ops.secure_partition closes both on one leaf partition:
--   * a partition with a tenant column gets ops.apply_tenant_rls (ENABLE + FORCE + the standard policies);
--     one without gets ENABLE + FORCE and no policy, which denies every direct read and write;
--   * every privilege held by anyone but the owner is revoked, so a service can reach the rows only
--     through the parent, where the parent's policies apply.
-- Rows routed through the parent are unaffected: PostgreSQL applies only the parent's policies and
-- privileges to them. Called by maintenance for each partition it creates and, every run, for every leaf
-- partition in Replicant schemas (sweep). Runs as the partition owner (maintenance SETs the role).
CREATE OR REPLACE FUNCTION ops.secure_partition(p_part regclass, p_tenant_column name DEFAULT 'tenant_id')
RETURNS integer
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
  t text;
  owner_oid oid;
  g record;
  revoked integer := 0;
BEGIN
  SELECT format('%I.%I', n.nspname, c.relname), c.relowner INTO t, owner_oid
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.oid = p_part AND c.relispartition;
  IF t IS NULL THEN
    RAISE EXCEPTION 'ops.secure_partition: % is not a partition', p_part;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = p_part AND attname = p_tenant_column AND NOT attisdropped) THEN
    PERFORM ops.apply_tenant_rls(p_part, p_tenant_column);
  ELSE
    EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', t);
  END IF;
  FOR g IN
    SELECT DISTINCT a.grantee
    FROM pg_class c, LATERAL aclexplode(c.relacl) a
    WHERE c.oid = p_part AND a.grantee <> owner_oid
  LOOP
    IF g.grantee = 0 THEN
      EXECUTE format('REVOKE ALL ON TABLE %s FROM PUBLIC', t);
    ELSE
      EXECUTE format('REVOKE ALL ON TABLE %s FROM %I', t, pg_get_userbyid(g.grantee));
    END IF;
    revoked := revoked + 1;
  END LOOP;
  RETURN revoked;
END
$$;
REVOKE ALL ON FUNCTION ops.secure_partition(regclass, name) FROM PUBLIC;
-- audit partitions are owned by audit_owner, so maintenance runs the helpers as that role.
GRANT EXECUTE ON FUNCTION ops.apply_tenant_rls(regclass, name), ops.secure_partition(regclass, name) TO audit_owner;

-- ---------------------------------------------------------------------------
-- ULID helpers. Replicant IDs are '<prefix>_<26 lowercase Crockford base32>' (C-IDS §1). The
-- first 10 body characters encode the 48-bit millisecond timestamp, and the alphabet is in
-- ASCII order, so under COLLATE "C" the IDs sort by creation time. That lets ULID-keyed tables
-- (PK on job_id, seq) be RANGE-partitioned by month without adding a timestamp to the key.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops.ulid_floor(p_prefix text, p_ts timestamptz) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
SET search_path = pg_catalog
AS $$
DECLARE
  alphabet constant text := '0123456789abcdefghjkmnpqrstvwxyz';
  ms bigint := floor(extract(epoch FROM p_ts) * 1000)::bigint;
  out text := '';
  i int;
BEGIN
  IF ms < 0 OR ms >= 281474976710656 THEN  -- 2^48
    RAISE EXCEPTION 'timestamp out of ULID range: %', p_ts;
  END IF;
  FOR i IN 1..10 LOOP
    out := substr(alphabet, (ms % 32)::int + 1, 1) || out;
    ms := ms / 32;
  END LOOP;
  RETURN p_prefix || out || '0000000000000000';
END
$$;
GRANT EXECUTE ON FUNCTION ops.ulid_floor(text, timestamptz) TO PUBLIC;

-- Reference rows for partitioned tables (DDL owners: see ddl/INDEX.md). Partition names are
-- <table>_YYYY_MM (the convention WS-04's DDL already uses). Rows for tables whose DDL is still
-- pending (cost, audit) assume the column names in ddl/INDEX.md §3 and are corrected by the
-- owning workstream's first revision.
INSERT INTO ops.partitioned_tables
  (table_name, key_kind, key_column, ulid_prefix, interval_months, premake, retain_months, owner_role, detach_before_drop, export_before_drop)
VALUES
  ('audit.events',              'timestamptz', 'occurred_at', NULL,   1, 3, 84, 'audit_owner',      true, true),   -- 7 years (C-STATE); DDL pending WS-08
  ('cost.usage_events',         'timestamptz', 'occurred_at', NULL,   1, 3, 25, 'replicant_migrator', true, false),  -- 2 years + 1 month; DDL pending WS-03
  ('memory.retrievals',         'timestamptz', 'at',          NULL,   1, 3, 13, 'replicant_migrator', true, false),  -- WS-04 data-model.md: 13 months
  ('memory.usage_events',       'timestamptz', 'at',          NULL,   1, 3, 13, 'replicant_migrator', true, false),  -- WS-04 data-model.md: 13 months
  ('jobs_read.job_timeline',    'ulid',        'job_id',      'job_', 1, 3, 26, 'replicant_migrator', true, false),  -- WS-07 jobs_read.sql (adopted XD-10-04)
  ('qa.test_results',           'timestamptz', 'observed_at', NULL,   1, 3, 4,  'replicant_migrator', true, false),  -- C-PG 1.1.0: 120-day retention (≈ 4 months, whole partitions)
  ('memory.snapshot_items',     'timestamptz', 'created_at',  NULL,   1, 3, 25, 'replicant_migrator', true, false)   -- XD-FIX-R2-051: 2-year snapshot lifetime + 1 month
ON CONFLICT (table_name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Monitoring without connections (D-26; postgres.md §9). SECURITY DEFINER so that grafana_ro
-- sees aggregates only: normalized statements (no parameters) and connection counts (no query
-- text). The owner (replicant_migrator) holds pg_read_all_stats via bootstrap.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops.top_statements(p_limit integer DEFAULT 50)
RETURNS TABLE (queryid bigint, usename name, calls bigint, total_exec_ms double precision,
               mean_exec_ms double precision, rows bigint, shared_blks_read bigint, query text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT s.queryid, r.rolname, s.calls, s.total_exec_time, s.mean_exec_time, s.rows, s.shared_blks_read,
         left(s.query, 500)
  FROM public.pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid
  WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  ORDER BY s.total_exec_time DESC
  LIMIT least(p_limit, 200)
$$;
REVOKE ALL ON FUNCTION ops.top_statements(integer) FROM PUBLIC;

CREATE OR REPLACE FUNCTION ops.connection_summary()
RETURNS TABLE (usename name, application_name text, state text, connections bigint,
               oldest_xact_age_s double precision, waiting bigint)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
  SELECT a.usename, a.application_name, coalesce(a.state, 'unknown'), count(*),
         max(extract(epoch FROM now() - a.xact_start)), count(*) FILTER (WHERE a.wait_event_type = 'Lock')
  FROM pg_stat_activity a
  WHERE a.datname = current_database() AND a.backend_type = 'client backend'
  GROUP BY 1, 2, 3
$$;
REVOKE ALL ON FUNCTION ops.connection_summary() FROM PUBLIC;

CREATE OR REPLACE VIEW ops.top_statements AS SELECT * FROM ops.top_statements(50);
CREATE OR REPLACE VIEW ops.connection_summary AS SELECT * FROM ops.connection_summary();
GRANT EXECUTE ON FUNCTION ops.top_statements(integer), ops.connection_summary() TO r_ops_read;

RESET ROLE;
