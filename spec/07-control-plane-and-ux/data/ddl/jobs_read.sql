-- Schema jobs_read. Writer: replicant-projector (svc_replicant_projector). DDL owner: WS-07.
-- DERIVED read model of Temporal (C-STATE 1.8.0 §2). Rebuildable; never authority for decisions (C-STATE rule 5).
-- Requires extension pg_trgm (created instance-wide by WS-10's migrate Job, C-PG §1) and WS-10's ops.* helpers.
-- XD-10-04 adopted (R1-187): ID columns COLLATE "C"; job_timeline RANGE-partitioned on job_id (ULID order =
-- time order under "C"); monthly partitions created ahead by WS-10's maintenance (ops.ensure_ulid_partitions);
-- CHECKs named; session_purge_due_at added.
CREATE SCHEMA IF NOT EXISTS jobs_read;

CREATE TABLE jobs_read.jobs (
    job_id            text COLLATE "C" PRIMARY KEY CONSTRAINT ck_jobs_job_id CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id         text COLLATE "C" NOT NULL,
    seq               bigint NOT NULL CONSTRAINT ck_jobs_seq CHECK (seq >= 1),        -- projection seq guard
    as_of             timestamptz NOT NULL,                    -- snapshot workflow time
    parent_job_id     text COLLATE "C",
    workflow_kind     text NOT NULL DEFAULT 'ReplicantJob' CONSTRAINT ck_jobs_kind CHECK (workflow_kind IN ('ReplicantJob', 'MaintenanceBatch')),
    workflow_type     text NOT NULL,
    status            text NOT NULL CONSTRAINT ck_jobs_status CHECK (status IN ('queued', 'running', 'waiting_human', 'waiting_external', 'paused',
                                                      'succeeded', 'failed', 'cancelled', 'rejected', 'expired', 'taken_over')),
    is_terminal       boolean GENERATED ALWAYS AS (status IN ('succeeded', 'failed', 'cancelled', 'rejected', 'expired', 'taken_over')) STORED,
    phase             text NOT NULL,
    -- Sub-status for waiting states (R2-020): derived from the snapshot and the runner-call heartbeat
    -- details; NULL when nothing is waiting. UI copy is keyed on it; never shown without a source.
    sub_status        text CONSTRAINT ck_jobs_sub_status CHECK (sub_status IN (
                         'waiting_capacity_tenant', 'waiting_capacity_cluster', 'tenant_budget_exhausted',
                         'platform_budget_exhausted', 'dependency_outage', 'operator_hold', 'bundle_revoked',
                         'retrying', 'rebasing', 'kill_switch_starts', 'kill_switch_runs', 'kill_switch_publishing',
                         'no_progress_30m')),                -- JobSnapshot.waiting.reason (C-MSG 2.0.0) plus derived states
    -- PR the job targets (pr_repair: from intake.job_requests at intake; publish jobs: from JobSnapshot.pull_request).
    -- WS-03's usage ingestor reads these to fill cost.job_costs.repo_id / pr_number for cost.v_pr_spend (XD-W2-03-05).
    pr_repo_id        text,
    pr_number         integer,
    sub_status_detail jsonb,                                   -- JobSnapshot.waiting as projected (CCR-07-10): {reason, since, attempt, quota_used, quota_limit, resets_at, contact_group}. The Runner has no queue (XD-W2-02-04 ruling): cluster_capacity comes from WS-01 after 60 s Pending
    current_node      text,
    current_node_started_at timestamptz,
    title             text,
    requester         text NOT NULL,
    on_behalf_of      text,
    priority          text,
    risk_level        text CONSTRAINT ck_jobs_risk CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    evaluation_mode   text CONSTRAINT ck_jobs_eval CHECK (evaluation_mode IN ('eval', 'shadow')),   -- C-MSG JobSnapshot.evaluation
    repos             text[] NOT NULL DEFAULT '{}',            -- <host>/<owner>/<name>
    repo_ids          text[] NOT NULL DEFAULT '{}',            -- immutable provider ids (authorization, routing)
    release_bundle_id text NOT NULL,
    release_cohort    text,                                    -- copied from intake at job.created (XD-06-08)
    migrated          boolean NOT NULL DEFAULT false,          -- true after a consented or operator migration (XD-06-08)
    worker_build_id   text,
    memory_snapshot_id text,
    cycle             integer NOT NULL DEFAULT 0,
    repair_used       integer,
    repair_max        integer,
    paused            boolean NOT NULL DEFAULT false,
    kill_switches     jsonb NOT NULL DEFAULT '[]'::jsonb,      -- engaged ops.kill_switch states projected by the workflow
    open_gate_id      text,
    waiting_since     timestamptz,
    last_progress_at  timestamptz,                             -- stall banner input (R2-020)
    terminal_outcome  text,
    terminal_at       timestamptz,
    session_purge_due_at timestamptz,                          -- XD-10-05 / XD-10-04
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    snapshot          jsonb NOT NULL                           -- full JobSnapshot (any replicant.job-snapshot/v*)
);
CREATE INDEX jobs_tenant_updated ON jobs_read.jobs (tenant_id, updated_at DESC) WHERE evaluation_mode IS NULL;
CREATE INDEX jobs_tenant_open ON jobs_read.jobs (tenant_id, updated_at DESC) WHERE NOT is_terminal AND evaluation_mode IS NULL;
CREATE INDEX jobs_requester ON jobs_read.jobs (requester, updated_at DESC);
CREATE INDEX jobs_bundle_terminal ON jobs_read.jobs (release_bundle_id, terminal_at) WHERE terminal_at IS NOT NULL;
CREATE INDEX jobs_bundle_open ON jobs_read.jobs (release_bundle_id, worker_build_id, status) WHERE NOT is_terminal;
CREATE INDEX jobs_title_trgm ON jobs_read.jobs USING gin (title gin_trgm_ops);
CREATE INDEX jobs_repo_ids ON jobs_read.jobs USING gin (repo_ids);
CREATE INDEX jobs_session_purge ON jobs_read.jobs (session_purge_due_at) WHERE session_purge_due_at IS NOT NULL;

-- Pinned-job counts for the releases screen, from the projection (R1-188: no visibility query on page load).
CREATE VIEW jobs_read.v_pinned_counts AS
SELECT release_bundle_id, worker_build_id,
       count(*) FILTER (WHERE status IN ('queued', 'running', 'paused'))            AS running,
       count(*) FILTER (WHERE status IN ('waiting_human', 'waiting_external'))     AS waiting,
       count(*) FILTER (WHERE status = 'waiting_human')                            AS at_gate,
       count(*) FILTER (WHERE status = 'waiting_external')                         AS in_pr_review,
       min(created_at)                                                             AS oldest_started_at
  FROM jobs_read.jobs
 WHERE NOT is_terminal
 GROUP BY release_bundle_id, worker_build_id;

CREATE TABLE jobs_read.job_timeline (
    job_id    text COLLATE "C" NOT NULL,
    seq       bigint NOT NULL,
    tenant_id text COLLATE "C" NOT NULL,
    type      text NOT NULL CONSTRAINT ck_job_timeline_type CHECK (type ~ '^[a-z]+(\.[a-z_]+)+$'),
    at        timestamptz NOT NULL,
    summary   text NOT NULL CONSTRAINT ck_job_timeline_summary CHECK (length(summary) <= 300),    -- workflow-generated, never untrusted text
    actor     text,
    refs      jsonb,
    PRIMARY KEY (job_id, seq)
) PARTITION BY RANGE (job_id);

CREATE TABLE jobs_read.job_runs (
    run_id         text COLLATE "C" PRIMARY KEY CONSTRAINT ck_job_runs_run_id CHECK (run_id ~ '^run_[0-9a-hjkmnp-tv-z]{26}$'),
    job_id         text COLLATE "C" NOT NULL,
    tenant_id      text COLLATE "C" NOT NULL,
    seq            bigint NOT NULL,
    node_key       text NOT NULL,
    cycle          integer NOT NULL,
    ordinal        integer NOT NULL,
    role           text NOT NULL,
    phase          text NOT NULL,
    reason         text,
    result_outcome text CONSTRAINT ck_job_runs_outcome CHECK (result_outcome IS NULL OR result_outcome IN ('completed', 'no_changes', 'blocked', 'failed')),   -- C-RUNNER 3.0.0
    -- QA runs: role_result.verdict (XD-FIX-R1-116 erratum, XD-W2-06-03); QA first pass reads this, never result_outcome
    qa_verdict     text CONSTRAINT ck_job_runs_qa_verdict CHECK (qa_verdict IS NULL OR qa_verdict IN ('pass', 'pass_with_warnings', 'fail', 'inconclusive', 'not_applicable')),
    isolation      text,
    started_at     timestamptz,
    finished_at    timestamptz,
    artifact_manifest_key text,
    result_key     text
);
CREATE INDEX job_runs_by_job ON jobs_read.job_runs (job_id);
CREATE INDEX job_runs_failures ON jobs_read.job_runs (reason, finished_at) WHERE phase = 'Failed';

CREATE TABLE jobs_read.job_artifacts (
    artifact_id  text COLLATE "C" PRIMARY KEY CONSTRAINT ck_job_artifacts_id CHECK (artifact_id ~ '^art_[0-9a-hjkmnp-tv-z]{26}$'),  -- C-IDS (CCR-07-04)
    job_id       text COLLATE "C" NOT NULL,
    tenant_id    text COLLATE "C" NOT NULL,
    run_id       text COLLATE "C",
    type         text NOT NULL,
    name         text NOT NULL,
    bucket_class text NOT NULL CONSTRAINT ck_job_artifacts_bucket CHECK (bucket_class IN ('artifacts', 'transcripts', 'session', 'audit')),
    key          text NOT NULL,
    content_type text,
    size_bytes   bigint,
    sha256       text,
    redacted     boolean,
    created_at   timestamptz NOT NULL,
    UNIQUE (bucket_class, key)
);
CREATE INDEX job_artifacts_by_job ON jobs_read.job_artifacts (job_id, type);

-- R1-099: the projector knows only JobSnapshot.memory_snapshot_id (no entry list), so the read model keeps one row
-- per (job, snapshot). Entries, text and the per-entry blast radius come live from memory-service
-- (GET /internal/v1/jobs/{job_id}/memory, GET /v1/admin/memory/entries/{entry_id}/jobs); nothing about memory
-- content is stored here (C-ART rule, applied to the read model).
CREATE TABLE jobs_read.job_memory_usage (
    job_id             text COLLATE "C" NOT NULL,
    tenant_id          text COLLATE "C" NOT NULL,
    memory_snapshot_id text COLLATE "C" NOT NULL CONSTRAINT ck_job_memory_usage_snapshot CHECK (memory_snapshot_id ~ '^msnap_[0-9a-hjkmnp-tv-z]{26}$'),
    first_seen_seq     bigint NOT NULL,
    PRIMARY KEY (job_id, memory_snapshot_id)
);
CREATE INDEX job_memory_usage_snapshot ON jobs_read.job_memory_usage (memory_snapshot_id);

-- Steering log. actor comes from JobSnapshot.steering[].actor (CCR-07-07); text is never stored here.
CREATE TABLE jobs_read.job_steering (
    message_id text COLLATE "C" PRIMARY KEY CONSTRAINT ck_job_steering_id CHECK (message_id ~ '^msg_[0-9a-hjkmnp-tv-z]{26}$'),
    job_id     text COLLATE "C" NOT NULL,
    tenant_id  text COLLATE "C" NOT NULL,
    actor      text,                                            -- NULL until CCR-07-07 lands (older snapshots)
    outcome    text NOT NULL,
    run_id     text,
    at         timestamptz NOT NULL
);

CREATE TABLE jobs_read.job_prs (
    repo_id    text NOT NULL,
    pr_number  integer NOT NULL,
    job_id     text COLLATE "C" NOT NULL,
    tenant_id  text COLLATE "C" NOT NULL,
    repo       text NOT NULL,
    head_ref   text,
    head_sha   text,
    state      text NOT NULL CONSTRAINT ck_job_prs_state CHECK (state IN ('draft', 'open', 'merged', 'closed')),
    url        text,
    job_open   boolean NOT NULL,
    -- From pr.opened / pr.merged / pr.closed events (XD-W2-06-03; WS-06 30-day PR merge rate)
    opened_at  timestamptz NOT NULL,
    merged_at  timestamptz,
    closed_at  timestamptz,
    PRIMARY KEY (repo_id, pr_number)
);
CREATE INDEX job_prs_by_job ON jobs_read.job_prs (job_id);

CREATE TABLE jobs_read.batches (
    job_id             text COLLATE "C" PRIMARY KEY,
    tenant_id          text COLLATE "C" NOT NULL,
    children_total     integer NOT NULL,
    children_by_status jsonb NOT NULL,
    max_concurrency    integer,
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Tenant RLS via WS-10's helper (canonical scopes: tenant, operator:all-tenants for r_cross_tenant_read).
-- The projector sets replicant.tenant_id per event transaction, so it needs no cross-tenant scope.
SELECT ops.apply_tenant_rls('jobs_read.jobs');
SELECT ops.apply_tenant_rls('jobs_read.job_timeline');
SELECT ops.apply_tenant_rls('jobs_read.job_runs');
SELECT ops.apply_tenant_rls('jobs_read.job_artifacts');
SELECT ops.apply_tenant_rls('jobs_read.job_memory_usage');
SELECT ops.apply_tenant_rls('jobs_read.job_steering');
SELECT ops.apply_tenant_rls('jobs_read.job_prs');
SELECT ops.apply_tenant_rls('jobs_read.batches');

-- Grant model v2 (XD-FIX-R1-160, C-PG 2.0.0): WS-10 owns roles, logins and schema USAGE; this file grants on
-- parent tables only (never ON ALL TABLES, never on a partition, no default privileges): reads to
-- r_<schema>_read, DML to r_<schema>_write, column-narrowed reads to a named login. Never to the login
-- svc_control_plane_api (its modules inherit r_<schema>_write; svc_cpa_read inherits the read roles).
-- job_timeline is partitioned (ops.partitioned_tables, ULID range on job_id): grants on the parent only;
-- WS-10 maintenance secures every partition (ops.secure_partition, XD-ORCH-04).
GRANT SELECT, INSERT, UPDATE, DELETE ON jobs_read.jobs, jobs_read.job_timeline, jobs_read.job_runs, jobs_read.job_artifacts,
    jobs_read.job_memory_usage, jobs_read.job_steering, jobs_read.job_prs, jobs_read.batches TO r_jobs_read_write;
GRANT SELECT ON jobs_read.jobs, jobs_read.job_timeline, jobs_read.job_runs, jobs_read.job_artifacts,
    jobs_read.job_memory_usage, jobs_read.job_steering, jobs_read.job_prs, jobs_read.batches, jobs_read.v_pinned_counts
    TO r_jobs_read_read, grafana_ro;
