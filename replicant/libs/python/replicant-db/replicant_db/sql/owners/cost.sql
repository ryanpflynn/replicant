-- Schema `cost` (C-PG: writer usage-ingestor, DDL owner WS-03, reviewed by WS-08).
-- Target DDL; the replicant-db SQLAlchemy models and Alembic migrations must produce exactly this (D-26).
-- PostgreSQL 17+. Tenant isolation by RLS on replicant.tenant_id (C-PG rule 4; session-variable protocol by WS-10).

CREATE SCHEMA IF NOT EXISTS cost;

-- Price book: operational configuration (ADR-013: prices change WHERE money goes, not how agents behave).
-- Versioned; usage events record the version used. Maintained through control-plane-api admin endpoints
-- (D-26: no SQL), synced from the enterprise gateway's published price list where one exists.
CREATE TABLE cost.price_book (
    version              integer      NOT NULL,
    gateway_model_id     text         NOT NULL CHECK (char_length(gateway_model_id) <= 200 AND gateway_model_id !~ 'latest$'),
    effective_from       timestamptz  NOT NULL,
    input_micros_per_mtok        bigint NOT NULL CHECK (input_micros_per_mtok >= 0),
    output_micros_per_mtok       bigint NOT NULL CHECK (output_micros_per_mtok >= 0),
    cache_read_micros_per_mtok   bigint NOT NULL CHECK (cache_read_micros_per_mtok >= 0),
    cache_write_micros_per_mtok  bigint NOT NULL CHECK (cache_write_micros_per_mtok >= 0),
    reasoning_billed_as_output   boolean NOT NULL DEFAULT true,
    source               text         NOT NULL CHECK (source IN ('enterprise_gateway', 'provider_list', 'contract', 'manual')),
    created_at           timestamptz  NOT NULL DEFAULT now(),
    created_by           text         NOT NULL CHECK (created_by ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
    PRIMARY KEY (version, gateway_model_id)
);

-- One row per model request seen by replicant-gateway-shim (schema replicant.gateway-usage/v1). Append-only.
CREATE TABLE cost.usage_events (
    event_id             text         NOT NULL CHECK (event_id ~ '^gwu_[0-9a-hjkmnp-tv-z]{26}$'),
    occurred_at          timestamptz  NOT NULL,
    tenant_id            text         NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'),
    job_id               text         CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
    run_id               text         CHECK (run_id ~ '^run_[0-9a-hjkmnp-tv-z]{26}$'),
    role                 text         CHECK (role IN ('planner','implementer','qa','reviewer','repair','takeover','eval','eval-judge')),
    release_bundle_id    text         CHECK (release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
    caller_kind          text         NOT NULL CHECK (caller_kind IN ('run', 'service')),
    token_jti            text         CHECK (token_jti ~ '^rtk_[0-9a-hjkmnp-tv-z]{26}$'),
    grant_jti            text         CHECK (grant_jti ~ '^rgr_[0-9a-hjkmnp-tv-z]{26}$'),
    acting_for           text         CHECK (acting_for ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
    service_principal    text         CHECK (service_principal IN ('svc_replicant-worker','svc_memory-service','svc_eval-worker')),
    gateway_model_id     text         NOT NULL,
    protocol             text         NOT NULL CHECK (protocol IN ('anthropic_messages','anthropic_count_tokens','openai_responses','bedrock_invoke','vertex_raw_predict','embeddings')),
    purpose              text         NOT NULL DEFAULT 'agent' CHECK (purpose IN ('agent','worker_llm','embedding','eval_judge','qa_agent')),
    usage_source         text         NOT NULL DEFAULT 'parsed' CHECK (usage_source IN ('parsed','estimated','upstream_reported')),
    served_model         text,
    pipeline_id          text,
    -- Budget accounts (XD-FIX-R2-061): eval, harness, shadow and proposer jobs are charged to a registered account
    -- instead of the tenant quota. evaluation_mode: eval (eval cases, harness tasks, judge calls), shadow (shadow
    -- runs, account fgw:pipeline:<pipeline_id>), none (production work, charged to the tenant quota).
    evaluation_mode      text         NOT NULL DEFAULT 'none' CHECK (evaluation_mode IN ('eval','shadow','none')),
    budget_account       text         CHECK (budget_account ~ '^fgw:(eval|pipeline|proposal):[A-Za-z0-9_.:-]{1,120}$'),
    streaming            boolean      NOT NULL DEFAULT false,
    status               text         NOT NULL CHECK (status IN ('ok','upstream_error','rate_limited','overloaded','budget_denied','model_denied','auth_denied','client_aborted','timeout')),
    http_status          smallint     NOT NULL,
    upstream_request_id  text,
    input_tokens         bigint       NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens        bigint       NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cache_read_tokens    bigint       NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens   bigint       NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    reasoning_tokens     bigint       NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    cost_micros          bigint       NOT NULL DEFAULT 0 CHECK (cost_micros >= 0),
    cost_source          text         NOT NULL CHECK (cost_source IN ('price_book', 'upstream')),
    price_book_version   integer,
    latency_ms           integer      NOT NULL CHECK (latency_ms >= 0),
    ttfb_ms              integer,
    retry_attempt        smallint     NOT NULL DEFAULT 0,
    trace_id             text         CHECK (trace_id ~ '^[0-9a-f]{32}$'),
    span_id              text         CHECK (span_id ~ '^[0-9a-f]{16}$'),
    ingested_at          timestamptz  NOT NULL DEFAULT now(),
    CHECK ((caller_kind = 'run' AND run_id IS NOT NULL AND token_jti IS NOT NULL)
        OR (caller_kind = 'service' AND service_principal IS NOT NULL)),
    CHECK ((evaluation_mode = 'none') = (budget_account IS NULL)),
    PRIMARY KEY (event_id, occurred_at)
) PARTITION BY RANGE (occurred_at);

-- Monthly partitions: WS-10 maintenance is the single executor (C-PG rule 3). `cost.usage_events` is registered in
-- ops.partitioned_tables (key occurred_at, premake 3, retain 25 months); maintenance creates each partition
-- (`usage_events_<yyyy>_<mm>`) and runs ops.secure_partition on it (RLS ENABLE + FORCE, canonical tenant policy,
-- every grant revoked), so services reach rows only through this parent (XD-ORCH-04, XD-FIX-ORCH-04). This DDL creates
-- no partition and grants nothing on partitions.

CREATE INDEX usage_events_run_idx    ON cost.usage_events (run_id, occurred_at);
CREATE INDEX usage_events_job_idx    ON cost.usage_events (job_id, occurred_at);
CREATE INDEX usage_events_tenant_idx ON cost.usage_events (tenant_id, occurred_at);
CREATE INDEX usage_events_account_idx ON cost.usage_events (budget_account, occurred_at) WHERE budget_account IS NOT NULL;

-- Ingestion cursor (R1-077, XD-10-13). The shim writes every usage event to the durable usage log
-- s3://<org>-replicant-<env>-artifacts/platform/gateway-usage/<yyyy>/<mm>/<dd>/<hh>/<shim pod>-<seq:010d>.ndjson.zst
-- (retained 7 days, >= the 72 h XD-10-13 requires) before it acknowledges the event locally. usage-ingestor records,
-- per shim pod stream, the last object and line it has committed. After a database restore it re-reads the log from
-- the restored cursor; INSERT ... ON CONFLICT (event_id, occurred_at) DO NOTHING makes the replay idempotent.
CREATE TABLE cost.ingest_cursor (
    stream              text        PRIMARY KEY,             -- shim pod name (stable per StatefulSet ordinal)
    object_key          text        NOT NULL,
    line_offset         bigint      NOT NULL CHECK (line_offset >= 0),
    last_event_id       text        NOT NULL CHECK (last_event_id ~ '^gwu_[0-9a-hjkmnp-tv-z]{26}$'),
    last_occurred_at    timestamptz NOT NULL,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Resource usage per run (from run_result.usage, the manifest, and kubelet summary), input to profile-rightsizer.
CREATE TABLE cost.run_resource_usage (
    run_id                    text        PRIMARY KEY CHECK (run_id ~ '^run_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id                 text        NOT NULL,
    job_id                    text        NOT NULL CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
    role                      text        NOT NULL,
    workload_class            text        NOT NULL CHECK (workload_class IN ('coding','qa_build','review','utility')),
    release_bundle_id         text        NOT NULL,
    environment_name          text        NOT NULL,
    environment_version       integer     NOT NULL,
    environment_digest        text        NOT NULL CHECK (environment_digest ~ '^sha256:[0-9a-f]{64}$'),
    codebase_type             text        NOT NULL,
    resource_profile          text        NOT NULL,
    runtime_class             text        NOT NULL CHECK (runtime_class IN ('replicant-gvisor','replicant-kata-fc','replicant-kata-clh')),
    cli                       text        NOT NULL,
    cli_version               text        NOT NULL,
    requested_cpu_m           integer     NOT NULL,
    limit_cpu_m               integer     NOT NULL,
    requested_memory_bytes    bigint      NOT NULL,
    limit_memory_bytes        bigint      NOT NULL,
    workspace_limit_bytes     bigint      NOT NULL,
    peak_cpu_m_60s            integer,              -- max over the run of the 60 s average CPU (Prometheus recording rule)
    cpu_throttled_ratio       real,                 -- throttled_periods / periods over the run
    peak_memory_bytes         bigint,               -- run_result.usage.peak_memory_bytes
    peak_workspace_bytes      bigint,               -- run_result.usage.peak_workspace_bytes
    peak_ephemeral_bytes      bigint,               -- kubelet /stats/summary pod ephemeral-storage high-water mark
    host_overhead_memory_bytes bigint,              -- pod cgroup peak minus container peak (runtime overhead evidence for WS-02)
    wall_seconds              real        NOT NULL,
    phase                     text        NOT NULL,
    failure_reason            text,
    finished_at               timestamptz NOT NULL,
    ingested_at               timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX run_resource_usage_profile_idx
    ON cost.run_resource_usage (environment_name, resource_profile, runtime_class, finished_at);

-- Rollups (maintained by usage-ingestor within 60 s; rebuildable from usage_events).
CREATE TABLE cost.run_costs (
    run_id              text        PRIMARY KEY,
    tenant_id           text        NOT NULL,
    job_id              text        NOT NULL,
    role                text        NOT NULL,
    model_calls         integer     NOT NULL DEFAULT 0,
    input_tokens        bigint      NOT NULL DEFAULT 0,
    output_tokens       bigint      NOT NULL DEFAULT 0,
    cache_read_tokens   bigint      NOT NULL DEFAULT 0,
    cache_write_tokens  bigint      NOT NULL DEFAULT 0,
    llm_cost_micros     bigint      NOT NULL DEFAULT 0,
    compute_cost_micros bigint      NOT NULL DEFAULT 0,
    budget_micros       bigint,
    denied_requests     integer     NOT NULL DEFAULT 0,
    evaluation_mode     text        NOT NULL DEFAULT 'none' CHECK (evaluation_mode IN ('eval','shadow','none')),
    budget_account      text,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cost.job_costs (
    job_id              text        PRIMARY KEY CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id           text        NOT NULL,
    llm_cost_micros     bigint      NOT NULL DEFAULT 0,
    worker_llm_cost_micros bigint   NOT NULL DEFAULT 0,
    compute_cost_micros bigint      NOT NULL DEFAULT 0,
    budget_micros       bigint,
    extensions          smallint    NOT NULL DEFAULT 0,
    evaluation_mode     text        NOT NULL DEFAULT 'none' CHECK (evaluation_mode IN ('eval','shadow','none')),
    budget_account      text,
    -- parent_job_id from ReplicantJob state via cost.record_run (XD-FIX-R2-062). repo_id / pr_number copied by usage-ingestor
    -- from jobs_read.jobs.pr_repo_id / pr_number (projector-filled, XD-ORCH-05) on every job_costs upsert (XD-FIX-R2-059).
    parent_job_id       text        CHECK (parent_job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
    repo_id             text,
    pr_number           integer     CHECK (pr_number > 0),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX job_costs_parent_idx ON cost.job_costs (parent_job_id) WHERE parent_job_id IS NOT NULL;
CREATE INDEX job_costs_pr_idx ON cost.job_costs (repo_id, pr_number) WHERE pr_number IS NOT NULL;

CREATE TABLE cost.tenant_daily_costs (
    tenant_id           text        NOT NULL,
    day                 date        NOT NULL,
    gateway_model_id    text        NOT NULL,
    role                text        NOT NULL,
    requests            bigint      NOT NULL DEFAULT 0,
    input_tokens        bigint      NOT NULL DEFAULT 0,
    output_tokens       bigint      NOT NULL DEFAULT 0,
    cache_read_tokens   bigint      NOT NULL DEFAULT 0,
    cost_micros         bigint      NOT NULL DEFAULT 0,
    evaluation_mode     text        NOT NULL DEFAULT 'none' CHECK (evaluation_mode IN ('eval','shadow','none')),
    PRIMARY KEY (tenant_id, day, gateway_model_id, role, evaluation_mode)
);

-- Read model for the control-plane UI (R1-078, XD-07-08): one row per job, cost columns in USD.
CREATE VIEW cost.job_rollups WITH (security_invoker = true, security_barrier = true) AS
SELECT j.job_id,
       j.tenant_id,
       (j.llm_cost_micros + j.worker_llm_cost_micros)::numeric / 1000000 AS llm_cost_usd,
       j.compute_cost_micros::numeric / 1000000                         AS compute_cost_usd,
       (j.llm_cost_micros + j.worker_llm_cost_micros + j.compute_cost_micros)::numeric / 1000000 AS total_cost_usd,
       j.budget_micros::numeric / 1000000                               AS budget_usd,
       j.extensions,
       j.evaluation_mode,
       j.budget_account,
       j.updated_at
FROM cost.job_costs j;

-- Tenant gateway spend per UTC day that counts against the tenant quota (XD-FIX-R2-055): control-plane-api intake
-- refuses with 429 before a job starts. Eval and shadow spend is charged to budget accounts, not the quota (R2-061).
CREATE VIEW cost.v_daily_spend WITH (security_invoker = true, security_barrier = true) AS
SELECT d.tenant_id,
       d.day,
       sum(d.cost_micros)::numeric / 1000000 AS spend_usd
FROM cost.tenant_daily_costs d
WHERE d.evaluation_mode = 'none'
GROUP BY d.tenant_id, d.day;

-- Lifetime spend per pull request across all jobs on it (XD-FIX-R2-059: intake checks budgets.prLifetimeUsd).
CREATE VIEW cost.v_pr_spend WITH (security_invoker = true, security_barrier = true) AS
SELECT j.repo_id,
       j.pr_number,
       sum(j.llm_cost_micros + j.worker_llm_cost_micros + j.compute_cost_micros)::numeric / 1000000 AS spend_usd
FROM cost.job_costs j
WHERE j.pr_number IS NOT NULL
GROUP BY j.repo_id, j.pr_number;

-- Nightly reconciliation against the enterprise gateway's own usage export (gateway.md section 9.3).
CREATE TABLE cost.reconciliation_runs (
    reconciliation_id   text        PRIMARY KEY CHECK (reconciliation_id ~ '^rec_[0-9a-hjkmnp-tv-z]{26}$'),
    day                 date        NOT NULL,
    tenant_id           text        NOT NULL,   -- one row per tenant and day; platform totals are a SUM over rows
    replicant_cost_micros bigint      NOT NULL,
    upstream_cost_micros bigint,
    replicant_requests    bigint      NOT NULL,
    upstream_requests   bigint,
    delta_ratio         real,
    status              text        NOT NULL CHECK (status IN ('ok','drift','upstream_missing','failed')),
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- Right-sizing proposals (schema replicant.rightsizing-proposal/v1).
CREATE TABLE cost.rightsizing_proposals (
    proposal_id         text        PRIMARY KEY CHECK (proposal_id ~ '^rsp_[0-9a-hjkmnp-tv-z]{26}$'),
    environment_name    text        NOT NULL,
    environment_tenant_id text,
    environment_version integer     NOT NULL,
    profile             text        NOT NULL,
    runtime_class       text        NOT NULL,
    trigger             text        NOT NULL,
    document            jsonb       NOT NULL,
    state               text        NOT NULL DEFAULT 'open' CHECK (state IN ('open','pr_opened','merged','rejected','superseded')),
    pr_url              text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    state_changed_at    timestamptz NOT NULL DEFAULT now(),
    state_changed_by    text
);
CREATE UNIQUE INDEX rightsizing_one_open_per_profile
    ON cost.rightsizing_proposals (environment_name, coalesce(environment_tenant_id, ''), profile, runtime_class)
    WHERE state IN ('open', 'pr_opened');

-- Row level security (C-PG rule 4; XD-FIX-R1-160, XD-10-20). WS-10's canonical tenant policies on every table with
-- tenant_id: ENABLE + FORCE, `tenant_id = ops.current_tenant()`, cross-tenant read only for r_cross_tenant_read under
-- scope `operator:all-tenants` (grafana_ro, control-plane-api operator module), cross-tenant write only for
-- r_cross_tenant_write under `system:all-tenants` (migrations, restore reconcile). usage-ingestor writes each tenant's
-- events and rollups with `replicant.tenant_id` set to that tenant (gateway.md §9.2). No role bypasses RLS.
SELECT ops.apply_tenant_rls('cost.usage_events');
SELECT ops.apply_tenant_rls('cost.run_resource_usage');
SELECT ops.apply_tenant_rls('cost.run_costs');
SELECT ops.apply_tenant_rls('cost.job_costs');
SELECT ops.apply_tenant_rls('cost.tenant_daily_costs');
SELECT ops.apply_tenant_rls('cost.reconciliation_runs');

-- Grants (C-PG rule 1, grant model v2, XD-FIX-R1-160). Parents and views only, named tables only (never ON ALL TABLES
-- IN SCHEMA, never on a partition: XD-FIX-ORCH-04). WS-10 grants schema USAGE and role memberships.
-- Writer usage-ingestor: r_cost_write. usage_events is append-only (replays are INSERT ... ON CONFLICT DO NOTHING).
GRANT SELECT, INSERT ON cost.usage_events TO r_cost_write;
GRANT SELECT, INSERT, UPDATE ON cost.price_book, cost.ingest_cursor, cost.run_resource_usage, cost.run_costs,
                                cost.job_costs, cost.tenant_daily_costs, cost.reconciliation_runs,
                                cost.rightsizing_proposals
    TO r_cost_write;
GRANT DELETE ON cost.run_costs, cost.job_costs, cost.tenant_daily_costs TO r_cost_write;   -- rollup rebuild (§9.1 step 4)
-- Readers: r_cost_read (inherited by svc_cpa_read and replicant-worker: budget allocation, cost.batch_spend, the
-- WS-01 compute check against run_resource_usage), plus grafana_ro (C-PG rule 6 lists `cost`).
GRANT SELECT ON cost.price_book, cost.usage_events, cost.run_resource_usage, cost.run_costs, cost.job_costs,
                cost.tenant_daily_costs, cost.reconciliation_runs, cost.rightsizing_proposals,
                cost.job_rollups, cost.v_daily_spend, cost.v_pr_spend
    TO r_cost_read, grafana_ro;
-- control-plane-api read surface named by WS-07 (XD-07-08, XD-FIX-R2-055, XD-FIX-R2-059); also covered by r_cost_read.
GRANT SELECT ON cost.job_rollups, cost.rightsizing_proposals, cost.v_daily_spend, cost.v_pr_spend TO svc_cpa_read;
