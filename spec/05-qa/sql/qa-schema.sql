-- WS-05 DDL for schema `qa` in database `replicant` (C-PG 2.0.0; CCR-05-02 accepted). Grant model v2 (XD-FIX-R1-160).
-- Purpose: per-test result history used to tell flaky tests from regressions (failure-feedback.md section 4).
-- Derived and rebuildable from `qa_report` artifacts in S3 (r2y), so it is evidence input, never authority.
-- Writer: replicant-projector (activity qa.index_results on task queue replicant-projections) through r_qa_write.
-- Readers: replicant-worker (classification) and control-plane-api (UI flake views) through r_qa_read;
--          grafana_ro only through the aggregate view qa.flake_summary_daily (owned by qa_analytics_owner).
-- Tenant isolation (C-PG rule 4, R1-113): every table goes through ops.apply_tenant_rls() (ENABLE + FORCE RLS,
-- canonical 'operator:all-tenants' / 'system:all-tenants' scopes), and the tenant-facing view runs with
-- security_invoker = true so callers' RLS applies.
-- Retention (R1-128): all three tables are RANGE-partitioned by month on a timestamptz column and registered in
-- ops.partitioned_tables with retain_months = 4 (C-PG: 120 days, whole partitions); WS-10's maintenance mode drops
-- old partitions. The projector has no DELETE.
-- Target: PostgreSQL 17. Objects are owned by replicant_migrator; the Alembic revision must produce exactly this.
-- Roles r_qa_read, r_qa_write, r_cross_tenant_read, r_cross_tenant_write, grafana_ro and qa_analytics_owner are
-- created by WS-10's baseline (qa_analytics_owner requested in XD-FIX-R1-113).

CREATE SCHEMA IF NOT EXISTS qa AUTHORIZATION replicant_migrator;

-- One row per (QA run, test) final outcome. observed_at = the qa_report's timing.finished_at, so a re-index of
-- the same run produces the same key (idempotent ON CONFLICT DO NOTHING).
CREATE TABLE qa.test_results (
    observed_at         timestamptz NOT NULL,
    tenant_id           text COLLATE "C" NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'),
    repo_id             text        NOT NULL CHECK (length(repo_id) BETWEEN 1 AND 64),
    test_id             text        NOT NULL CHECK (length(test_id) BETWEEN 1 AND 512),
    test_key            bytea       NOT NULL CHECK (length(test_key) = 32),  -- sha256(repo_id || '\n' || test_id)
    stage               text        NOT NULL CHECK (stage IN ('unit', 'integration', 'e2e_repo', 'e2e_generated', 'routes', 'smoke', 'health')),
    job_id              text        NOT NULL CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
    run_id              text        NOT NULL CHECK (run_id ~ '^run_[0-9a-hjkmnp-tv-z]{26}$'),
    release_bundle_id   text        NOT NULL CHECK (release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
    environment_name    text        NOT NULL,
    environment_version integer     NOT NULL CHECK (environment_version >= 1),
    isolation           text        NOT NULL CHECK (isolation IN ('gvisor', 'kata-fc', 'kata-clh', 'local-docker', 'local-podman')),
    final_status        text        NOT NULL CHECK (final_status IN ('passed', 'failed', 'flaky', 'skipped', 'error')),
    change_attempts     smallint    NOT NULL CHECK (change_attempts BETWEEN 0 AND 10),
    change_failures     smallint    NOT NULL CHECK (change_failures BETWEEN 0 AND change_attempts),
    base_attempts       smallint    NOT NULL DEFAULT 0 CHECK (base_attempts BETWEEN 0 AND 10),
    base_failures       smallint    NOT NULL DEFAULT 0 CHECK (base_failures BETWEEN 0 AND base_attempts),
    cause               text        NOT NULL CHECK (cause IN ('none', 'code', 'flaky_new', 'flaky_known', 'pre_existing', 'environment', 'test_defect', 'unknown')),
    duration_ms         integer     CHECK (duration_ms >= 0),
    CONSTRAINT pk_test_results PRIMARY KEY (observed_at, run_id, test_key)
) PARTITION BY RANGE (observed_at);

CREATE INDEX test_results_key_time ON qa.test_results (tenant_id, test_key, observed_at DESC);
CREATE INDEX test_results_run ON qa.test_results (run_id);

-- Idempotency of qa.index_results: the projector inserts here first (ON CONFLICT DO NOTHING RETURNING run_id)
-- and writes test_results / test_stats_daily only when the insert returned a row, in the same transaction.
CREATE TABLE qa.indexed_runs (
    observed_at    timestamptz NOT NULL,                 -- the report's timing.finished_at (deterministic per run)
    run_id         text        NOT NULL CHECK (run_id ~ '^run_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id      text COLLATE "C" NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'),
    job_id         text        NOT NULL CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
    report_key     text        NOT NULL CHECK (length(report_key) <= 1024),
    report_sha256  text        NOT NULL CHECK (report_sha256 ~ '^[0-9a-f]{64}$'),
    tests_indexed  integer     NOT NULL CHECK (tests_indexed >= 0),
    indexed_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_indexed_runs PRIMARY KEY (observed_at, run_id)
) PARTITION BY RANGE (observed_at);

-- Daily rollup, UPSERT per (tenant, test_key, day_start) in the same transaction as the indexed_runs insert.
CREATE TABLE qa.test_stats_daily (
    day_start        timestamptz NOT NULL CHECK (day_start = date_trunc('day', day_start, 'UTC')),
    tenant_id        text COLLATE "C" NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'),
    test_key         bytea    NOT NULL CHECK (length(test_key) = 32),
    repo_id          text     NOT NULL,
    test_id          text     NOT NULL,
    runs             integer  NOT NULL DEFAULT 0 CHECK (runs >= 0),
    -- A run counts as flaky evidence when the same tree produced both a pass and a failure (final_status 'flaky').
    flaky_runs       integer  NOT NULL DEFAULT 0 CHECK (flaky_runs >= 0),
    base_attempts    integer  NOT NULL DEFAULT 0 CHECK (base_attempts >= 0),
    base_failures    integer  NOT NULL DEFAULT 0 CHECK (base_failures >= 0),
    code_failures    integer  NOT NULL DEFAULT 0 CHECK (code_failures >= 0),
    env_failures     integer  NOT NULL DEFAULT 0 CHECK (env_failures >= 0),
    CONSTRAINT pk_test_stats_daily PRIMARY KEY (day_start, tenant_id, test_key)
) PARTITION BY RANGE (day_start);

-- Initial partitions; WS-10's maintenance creates later months (premake 3) and drops after 4 months.
CREATE TABLE qa.test_results_2026_10 PARTITION OF qa.test_results FOR VALUES FROM ('2026-10-01 00:00:00+00') TO ('2026-11-01 00:00:00+00');
CREATE TABLE qa.test_results_2026_11 PARTITION OF qa.test_results FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');
CREATE TABLE qa.indexed_runs_2026_10 PARTITION OF qa.indexed_runs FOR VALUES FROM ('2026-10-01 00:00:00+00') TO ('2026-11-01 00:00:00+00');
CREATE TABLE qa.indexed_runs_2026_11 PARTITION OF qa.indexed_runs FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');
CREATE TABLE qa.test_stats_daily_2026_10 PARTITION OF qa.test_stats_daily FOR VALUES FROM ('2026-10-01 00:00:00+00') TO ('2026-11-01 00:00:00+00');
CREATE TABLE qa.test_stats_daily_2026_11 PARTITION OF qa.test_stats_daily FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');

INSERT INTO ops.partitioned_tables
  (table_name, key_kind, key_column, ulid_prefix, interval_months, premake, retain_months, owner_role, detach_before_drop, export_before_drop)
VALUES
  ('qa.test_results',    'timestamptz', 'observed_at', NULL, 1, 3, 4, 'replicant_migrator', true, false),
  ('qa.indexed_runs',    'timestamptz', 'observed_at', NULL, 1, 3, 4, 'replicant_migrator', true, false),
  ('qa.test_stats_daily','timestamptz', 'day_start',   NULL, 1, 3, 4, 'replicant_migrator', true, false)
ON CONFLICT (table_name) DO NOTHING;

-- Tenant isolation: ENABLE + FORCE RLS and the standard policies (WS-10 ddl/20-ops.sql).
SELECT ops.apply_tenant_rls('qa.test_results');
SELECT ops.apply_tenant_rls('qa.indexed_runs');
SELECT ops.apply_tenant_rls('qa.test_stats_daily');
-- RLS on a partitioned parent does not protect direct reads of its partitions, and WS-10's default privileges
-- grant r_qa_read SELECT on every new table (verified: tools/test_qa_rls.sh). Every partition therefore gets the
-- same policies and loses direct grants; WS-10's partition maintenance must do the same for each partition it
-- creates (XD-FIX-R1-113).
-- Partition-level isolation (XD-ORCH-04, XD-FIX-ORCH-04): RLS on a parent does not cover direct reads of a
-- partition. ops.secure_partition (WS-10 ddl/20-ops.sql) applies ENABLE + FORCE + the tenant policies to the
-- partition and revokes every privilege but the owner's. WS-10 maintenance does the same for partitions it creates
-- later and sweeps all leaf partitions hourly. Grants below are on the parents only.
DO $$
DECLARE p regclass;
BEGIN
  FOR p IN SELECT i.inhrelid::regclass FROM pg_inherits i
           WHERE i.inhparent IN ('qa.test_results'::regclass, 'qa.indexed_runs'::regclass, 'qa.test_stats_daily'::regclass)
  LOOP
    PERFORM ops.secure_partition(p);
  END LOOP;
END $$;

-- Classifier input (failure-feedback.md section 4.3). security_invoker: callers' RLS applies (R1-113).
-- Code-caused failures are excluded from the flake rate: a test that caught a regression is not "flaky".
CREATE VIEW qa.test_stats_30d WITH (security_invoker = true, security_barrier = true) AS
SELECT tenant_id,
       test_key,
       min(repo_id)                                                                AS repo_id,
       min(test_id)                                                                AS test_id,
       sum(runs)::integer                                                          AS runs,
       sum(flaky_runs)::integer                                                    AS flaky_runs,
       sum(base_attempts)::integer                                                 AS base_attempts,
       sum(base_failures)::integer                                                 AS base_failures,
       sum(code_failures)::integer                                                 AS code_failures,
       (sum(flaky_runs)::numeric / NULLIF(sum(runs) - sum(code_failures), 0))      AS flake_rate,
       (sum(base_failures)::numeric / NULLIF(sum(base_attempts), 0))               AS base_failure_rate
FROM qa.test_stats_daily
WHERE day_start >= date_trunc('day', now(), 'UTC') - interval '30 days'
GROUP BY tenant_id, test_key;

-- Aggregate-only view for Grafana (no test names, which are repository content). Same pattern as WS-04's
-- memory.v_analytics_*: owned by qa_analytics_owner (NOLOGIN, member of r_cross_tenant_read, SELECT on the
-- base table only), so it runs with the owner's rights; grafana_ro's role-default scope
-- 'operator:all-tenants' lets the role-targeted _xt_read policy pass for the owner.
CREATE VIEW qa.flake_summary_daily WITH (security_barrier = true) AS
SELECT tenant_id, day_start AS day, count(*) AS tests, sum(runs) AS runs, sum(flaky_runs) AS flaky_runs,
       sum(base_failures) AS base_failures, sum(code_failures) AS code_failures, sum(env_failures) AS env_failures
FROM qa.test_stats_daily
GROUP BY tenant_id, day_start;

-- Grants. Owner grants are authoritative and narrower than WS-10's default privileges (triage XD-ORCH-03):
-- the projector never deletes (retention is partition drop by the migrate Job).
REVOKE ALL ON qa.test_results, qa.indexed_runs, qa.test_stats_daily FROM r_qa_write, r_qa_read;
REVOKE ALL ON qa.test_stats_30d, qa.flake_summary_daily FROM PUBLIC;
GRANT USAGE ON SCHEMA qa TO r_qa_write, r_qa_read, qa_analytics_owner, grafana_ro;
GRANT SELECT, INSERT, UPDATE ON qa.test_results, qa.indexed_runs, qa.test_stats_daily TO r_qa_write;
GRANT SELECT ON qa.test_results, qa.indexed_runs, qa.test_stats_daily, qa.test_stats_30d TO r_qa_read;
GRANT SELECT ON qa.test_stats_30d TO r_qa_write;
GRANT SELECT ON qa.test_stats_daily TO qa_analytics_owner;
GRANT CREATE ON SCHEMA qa TO qa_analytics_owner;              -- needed only for the ownership change
ALTER VIEW qa.flake_summary_daily OWNER TO qa_analytics_owner;
REVOKE CREATE ON SCHEMA qa FROM qa_analytics_owner;
GRANT SELECT ON qa.flake_summary_daily TO grafana_ro;
