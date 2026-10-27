-- WS-06 — DDL for schema `evals` (C-PG §2: writer eval-worker, role svc_eval_worker;
-- readers control-plane-api, grafana_ro). System of record for eval runs, cases, scores,
-- comparisons, judge calibration and online metric windows (C-STATE §1.4). Langfuse holds a
-- derived export (scores, experiments) for trace-level drill-down only.
-- PostgreSQL 17+. Eval data is platform data: rows carry tenant_id 'replicant-eval' (C-PG rule 4),
-- and source_tenant_id records where a golden task's content came from (for consent and erasure).

CREATE SCHEMA IF NOT EXISTS evals;

-- ---------------------------------------------------------------------------------------------
-- Golden tasks and sets (eval-harness.md §2)
-- ---------------------------------------------------------------------------------------------
CREATE TABLE evals.golden_tasks (
    golden_task_id     text        PRIMARY KEY CHECK (golden_task_id ~ '^gtask_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id          text        NOT NULL DEFAULT 'replicant-eval' CHECK (tenant_id = 'replicant-eval'),
    source_tenant_id   text        NULL CHECK (source_tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'),
    source_kind        text        NOT NULL CHECK (source_kind IN ('historical_pr', 'replicant_success',
                                     'replicant_failure', 'pr_review_thread', 'synthetic_mutation', 'synthetic_authored')),
    source_job_id      text        NULL CHECK (source_job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
    workflow_type      text        NOT NULL CHECK (workflow_type IN ('bugfix', 'feature', 'api_change', 'docs',
                                     'hotfix', 'maintenance', 'pr_repair', 'qa_only')),
    codebase_type      text        NOT NULL,
    difficulty         text        NOT NULL DEFAULT 'unknown' CHECK (difficulty IN ('easy', 'medium', 'hard', 'unknown')),
    state              text        NOT NULL DEFAULT 'candidate'
                                   CHECK (state IN ('candidate', 'verifying', 'quarantined', 'active', 'retired', 'erased')),
    state_reason       text        NULL,
    task_key           text        NOT NULL,   -- eval bucket: golden/<id>/task.json
    task_sha256        text        NOT NULL CHECK (task_sha256 ~ '^[0-9a-f]{64}$'),
    supersedes         text        NULL CHECK (supersedes ~ '^gtask_[0-9a-hjkmnp-tv-z]{26}$'),
    langfuse_dataset_item_id text  NULL,
    flake_runs         integer     NOT NULL DEFAULT 0,   -- oracle/reference re-runs done during verification
    flake_failures     integer     NOT NULL DEFAULT 0,
    verified_by        text[]      NOT NULL DEFAULT '{}',
    consent_audit_event_id text    NULL CHECK (consent_audit_event_id ~ '^aud_[0-9a-hjkmnp-tv-z]{26}$'),
    data_classification text       NOT NULL CHECK (data_classification IN ('internal', 'confidential', 'restricted')),
    expires_at         timestamptz NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    created_by         text        NOT NULL,
    retired_at         timestamptz NULL
);
CREATE INDEX golden_tasks_strata_idx ON evals.golden_tasks (state, workflow_type, codebase_type);
CREATE INDEX golden_tasks_source_tenant_idx ON evals.golden_tasks (source_tenant_id) WHERE source_tenant_id IS NOT NULL;

CREATE TABLE evals.golden_sets (
    golden_set_id        text        PRIMARY KEY CHECK (golden_set_id ~ '^[a-z][a-z0-9-]{2,40}@[1-9][0-9]*$'),  -- e.g. release-2026q4@3
    tenant_id            text        NOT NULL DEFAULT 'replicant-eval',
    langfuse_dataset_name text       NOT NULL,     -- e.g. replicant/golden/release-2026q4
    langfuse_dataset_id  text        NULL,
    langfuse_version_ts  timestamptz NULL,         -- dataset version timestamp pinned for this set version
    frozen_at            timestamptz NULL,         -- set membership immutable once frozen
    notes                text        NULL,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE evals.golden_set_members (
    golden_set_id  text    NOT NULL REFERENCES evals.golden_sets (golden_set_id),
    golden_task_id text    NOT NULL REFERENCES evals.golden_tasks (golden_task_id),
    tenant_id      text    NOT NULL DEFAULT 'replicant-eval',
    in_smoke       boolean NOT NULL DEFAULT false,
    in_standard    boolean NOT NULL DEFAULT false,
    in_full        boolean NOT NULL DEFAULT false,
    in_holdout     boolean NOT NULL DEFAULT false,
    stratum        text    NOT NULL,   -- '<workflow_type>/<codebase_type>'
    weight         numeric(8,6) NOT NULL DEFAULT 1,  -- post-stratification weight to the production mix
    PRIMARY KEY (golden_set_id, golden_task_id),
    CONSTRAINT holdout_is_exclusive CHECK (NOT (in_holdout AND (in_smoke OR in_standard OR in_full))),
    CONSTRAINT nested_tiers CHECK ((NOT in_smoke OR in_standard) AND (NOT in_standard OR in_full))
);

-- Rolling per-task reliability of the default bundle (stable-set membership, difficulty estimate).
CREATE TABLE evals.task_reliability (
    golden_task_id    text        NOT NULL REFERENCES evals.golden_tasks (golden_task_id),
    release_bundle_id text        NOT NULL CHECK (release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id         text        NOT NULL DEFAULT 'replicant-eval',
    runs              integer     NOT NULL,
    resolved          integer     NOT NULL,
    last_run_at       timestamptz NOT NULL,
    PRIMARY KEY (golden_task_id, release_bundle_id)
);

-- ---------------------------------------------------------------------------------------------
-- Judges and calibration (eval-harness.md §4.2, §5)
-- ---------------------------------------------------------------------------------------------
CREATE TABLE evals.judges (
    judge_name     text        NOT NULL CHECK (judge_name ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'),
    judge_version  integer     NOT NULL CHECK (judge_version >= 1),
    tenant_id      text        NOT NULL DEFAULT 'replicant-eval',
    digest         text        NOT NULL CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
    rubric         text        NOT NULL,
    document       jsonb       NOT NULL,       -- JudgeDefinition (schemas/judge-definition.schema.json)
    state          text        NOT NULL DEFAULT 'uncalibrated'
                               CHECK (state IN ('uncalibrated', 'gating', 'decision', 'suspect', 'retired')),
    state_changed_at timestamptz NOT NULL DEFAULT now(),
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (judge_name, judge_version),
    UNIQUE (digest)
);

CREATE TABLE evals.anchor_sets (
    anchor_set     text        PRIMARY KEY CHECK (anchor_set ~ '^[a-z][a-z0-9-]{2,62}$'),
    tenant_id      text        NOT NULL DEFAULT 'replicant-eval',
    rubric         text        NOT NULL,
    frozen_at      timestamptz NULL,
    item_count     integer     NOT NULL DEFAULT 0
);

CREATE TABLE evals.anchor_items (
    anchor_set     text        NOT NULL REFERENCES evals.anchor_sets (anchor_set),
    item_key       text        NOT NULL,     -- eval bucket: anchors/<set>/<item>.json (input + output under judgement)
    tenant_id      text        NOT NULL DEFAULT 'replicant-eval',
    is_known_negative boolean  NOT NULL DEFAULT false,
    PRIMARY KEY (anchor_set, item_key)
);

CREATE TABLE evals.human_labels (
    label_id       text        PRIMARY KEY CHECK (label_id ~ '^lbl_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id      text        NOT NULL DEFAULT 'replicant-eval',
    subject_kind   text        NOT NULL CHECK (subject_kind IN ('anchor_item', 'eval_case', 'online_job')),
    subject_ref    text        NOT NULL,     -- anchor_set/item_key, eval_run_id/golden_task_id, or job_id
    rubric         text        NOT NULL,
    criterion      text        NOT NULL,
    value          numeric     NOT NULL,
    labeler        text        NOT NULL CHECK (labeler ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
    blind          boolean     NOT NULL DEFAULT true,   -- labeler did not see judge output or arm identity
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX human_labels_subject_idx ON evals.human_labels (subject_kind, subject_ref, rubric, criterion);

CREATE TABLE evals.judge_calibrations (
    judge_name       text        NOT NULL,
    judge_version    integer     NOT NULL,
    anchor_set       text        NOT NULL REFERENCES evals.anchor_sets (anchor_set),
    run_at           timestamptz NOT NULL DEFAULT now(),
    tenant_id        text        NOT NULL DEFAULT 'replicant-eval',
    kind             text        NOT NULL CHECK (kind IN ('initial', 'weekly_drift', 'on_demand')),
    n_items          integer     NOT NULL,
    weighted_kappa   numeric(5,4) NULL,   -- quadratic-weighted Cohen's kappa vs human consensus (ordinal)
    kappa            numeric(5,4) NULL,   -- Cohen's kappa (binary criteria)
    krippendorff_alpha numeric(5,4) NULL, -- judge + all human labelers
    human_human_kappa numeric(5,4) NULL,
    self_agreement   numeric(5,4) NULL,   -- exact agreement with this judge's own outputs at calibration time
    mean_shift       numeric(6,4) NULL,   -- mean score now minus at calibration (same items)
    known_negatives_passed boolean NOT NULL,
    served_model_mismatches integer NOT NULL DEFAULT 0,
    verdict          text        NOT NULL CHECK (verdict IN ('ok', 'suspect', 'fail')),
    details_key      text        NULL,
    PRIMARY KEY (judge_name, judge_version, anchor_set, run_at),
    FOREIGN KEY (judge_name, judge_version) REFERENCES evals.judges (judge_name, judge_version)
);

-- ---------------------------------------------------------------------------------------------
-- Eval runs, cases, scores (eval-harness.md §3–§4)
-- One eval_run = one arm x one repetition over one task subset (Langfuse: one experiment).
-- ---------------------------------------------------------------------------------------------
CREATE TABLE evals.eval_runs (
    eval_run_id        text        PRIMARY KEY CHECK (eval_run_id ~ '^eval_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id          text        NOT NULL DEFAULT 'replicant-eval',
    comparison_id      text        NULL CHECK (comparison_id ~ '^cmp_[0-9a-hjkmnp-tv-z]{26}$'),
    purpose            text        NOT NULL CHECK (purpose IN ('promotion', 'sentinel', 'proposal', 'holdout',
                                     'calibration_support', 'adhoc')),
    arm                text        NOT NULL CHECK (arm IN ('candidate', 'baseline', 'single')),
    rep                smallint    NOT NULL CHECK (rep BETWEEN 1 AND 5),
    release_bundle_id  text        NOT NULL CHECK (release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
    golden_set_id      text        NOT NULL REFERENCES evals.golden_sets (golden_set_id),
    subset             text        NOT NULL CHECK (subset IN ('smoke', 'standard', 'full', 'holdout', 'targeted')),
    targeted_filter    jsonb       NULL,
    judge_refs         jsonb       NOT NULL DEFAULT '[]'::jsonb,  -- [{judge_name, judge_version, digest}]
    state              text        NOT NULL DEFAULT 'pending'
                                   CHECK (state IN ('pending', 'running', 'stopped_harm', 'stopped_budget',
                                     'completed', 'invalid', 'cancelled')),
    max_usd            numeric(10,2) NOT NULL,
    max_parallel_cases integer     NOT NULL,
    spent_usd          numeric(10,2) NOT NULL DEFAULT 0,
    workflow_id        text        NOT NULL,   -- EvalSuite workflow id = eval_run_id
    langfuse_experiment_id text    NULL,
    requested_by       text        NOT NULL,   -- on_behalf_of human for the eval JobRequests
    started_at         timestamptz NULL,
    completed_at       timestamptz NULL,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX eval_runs_bundle_idx ON evals.eval_runs (release_bundle_id, golden_set_id, subset, completed_at DESC);

CREATE TABLE evals.cases (
    eval_run_id      text        NOT NULL REFERENCES evals.eval_runs (eval_run_id),
    golden_task_id   text        NOT NULL REFERENCES evals.golden_tasks (golden_task_id),
    tenant_id        text        NOT NULL DEFAULT 'replicant-eval',
    job_id           text        NULL CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
    memory_snapshot_id text      NULL CHECK (memory_snapshot_id ~ '^msnap_[0-9a-hjkmnp-tv-z]{26}$'),
    status           text        NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'running', 'scored', 'harness_error', 'timeout',
                                   'infra_error', 'preempted', 'budget_skipped', 'cancelled')),
    harness_job_id   text        NULL CHECK (harness_job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),  -- scoring harness job (R1-134)
    error_class      text        NULL,     -- for harness/infra/timeout: e.g. 'Preempted', 'NodeLost', 'gateway_5xx', 'served_model_mismatch'
    attempts         smallint    NOT NULL DEFAULT 0,
    resolved         boolean     NULL,     -- primary metric (eval-harness.md §4.1); NULL unless status = 'scored'
    job_terminal_status text     NULL,
    cost_usd         numeric(10,4) NULL,   -- from the cost ledger (authoritative), by job_id
    judge_cost_usd   numeric(10,4) NULL,   -- from the cost ledger, budget account fgw:eval:<eval_run_id>, role eval-judge (R2-063)
    judge_calls      integer     NOT NULL DEFAULT 0,
    wall_seconds     integer     NULL,
    agent_seconds    integer     NULL,
    model_calls      integer     NULL,
    trace_id         text        NULL CHECK (trace_id ~ '^[0-9a-f]{32}$'),
    outputs_key      text        NULL,     -- eval bucket: runs/<eval_run_id>/<golden_task_id>/
    started_at       timestamptz NULL,
    finished_at      timestamptz NULL,
    PRIMARY KEY (eval_run_id, golden_task_id)
);
CREATE INDEX cases_job_idx ON evals.cases (job_id) WHERE job_id IS NOT NULL;

CREATE TABLE evals.scores (
    eval_run_id     text        NOT NULL,
    golden_task_id  text        NOT NULL,
    metric          text        NOT NULL,   -- 'D5_hidden_tests', 'judge.diff_quality.minimality', ...
    tenant_id       text        NOT NULL DEFAULT 'replicant-eval',
    scorer          text        NOT NULL,   -- 'deterministic:<check>@<version>' or 'judge:<name>@<version>'
    data_type       text        NOT NULL CHECK (data_type IN ('BOOLEAN', 'NUMERIC', 'CATEGORICAL')),
    value_num       numeric     NULL,
    value_cat       text        NULL,
    details_key     text        NULL,
    langfuse_score_id text      NULL,       -- deterministic: '<eval_run_id>:<golden_task_id>:<metric>'
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (eval_run_id, golden_task_id, metric),
    FOREIGN KEY (eval_run_id, golden_task_id) REFERENCES evals.cases (eval_run_id, golden_task_id),
    CONSTRAINT one_value CHECK ((value_num IS NULL) <> (value_cat IS NULL))
);

CREATE TABLE evals.comparisons (
    comparison_id        text        PRIMARY KEY CHECK (comparison_id ~ '^cmp_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id            text        NOT NULL DEFAULT 'replicant-eval',
    candidate_bundle_id  text        NOT NULL,
    baseline_bundle_id   text        NOT NULL,
    tier                 text        NOT NULL CHECK (tier IN ('T1', 'T2', 'T3')),
    golden_set_id        text        NOT NULL REFERENCES evals.golden_sets (golden_set_id),
    hypothesis           text        NOT NULL CHECK (hypothesis IN ('non_inferiority', 'superiority')),
    margin_pts           numeric(5,2) NOT NULL,
    policy_name          text        NOT NULL,
    policy_version       integer     NOT NULL,
    attempt              smallint    NOT NULL DEFAULT 1 CHECK (attempt BETWEEN 1 AND 2),  -- R2-056: one re-run after 'invalid'
    verdict              text        NULL CHECK (verdict IN ('pass', 'fail', 'invalid', 'needs_signoff')),
    verdict_reason       text        NULL CHECK (verdict_reason IN ('gates_passed', 'gate_failed', 'interim_harm',
                                       'cost_regression', 'harness_errors', 'spend_ceiling', 'needs_signoff')),
    stats                jsonb       NULL,   -- full statistics document (eval-harness.md §6.5)
    gate_results         jsonb       NULL,   -- per G-OFF-* gate: value, threshold, pass
    decided_at           timestamptz NULL,
    created_at           timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------
-- Release pipelines (projection of the ReleasePipeline workflow for listing; Temporal is the
-- source of truth, C-STATE §1.1) and sign-off projection.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE evals.release_pipelines (
    pipeline_id          text        PRIMARY KEY CHECK (pipeline_id ~ '^rpl_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id            text        NOT NULL DEFAULT 'replicant-system',
    env                  text        NOT NULL,
    candidate_bundle_id  text        NOT NULL,
    baseline_bundle_id   text        NOT NULL,
    stage                text        NOT NULL CHECK (stage IN ('offline', 'shadow', 'canary', 'holdback', 'done',
                                       'rolled_back', 'aborted')),  -- = C-MSG ReleaseSnapshot.stage
    canary_stage         text        NULL,      -- policy stage name while stage = 'canary' (ring-early, pct-10, pct-50)
    status               text        NOT NULL CHECK (status IN ('running', 'waiting_signoff', 'promoted',
                                       'failed', 'rolled_back', 'aborted')),
    projection_seq       integer     NOT NULL,
    snapshot             jsonb       NOT NULL,
    updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE evals.release_signoffs (
    pipeline_id   text        NOT NULL REFERENCES evals.release_pipelines (pipeline_id),
    decision_id   text        NOT NULL CHECK (decision_id ~ '^dec_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id     text        NOT NULL DEFAULT 'replicant-system',
    signoff_point text        NOT NULL,
    presented_digest text     NOT NULL CHECK (presented_digest ~ '^sha256:[0-9a-f]{64}$'),  -- evidence packet the signer saw (R1-144)
    decision      text        NOT NULL CHECK (decision IN ('approve', 'reject')),
    actor         text        NOT NULL CHECK (actor ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
    comment       text        NULL,
    decided_at    timestamptz NOT NULL,
    PRIMARY KEY (pipeline_id, decision_id)
);

-- ---------------------------------------------------------------------------------------------
-- Online metric windows (online-metrics.md §4) and guardrail evaluations (promotion.md §6)
-- ---------------------------------------------------------------------------------------------
CREATE TABLE evals.online_windows (
    env              text        NOT NULL,
    metric           text        NOT NULL,
    release_bundle_id text       NOT NULL,   -- base bundle (derived bundles roll up to their base)
    cohort           text        NOT NULL CHECK (cohort IN ('default', 'candidate', 'holdback', 'experiment', 'override', 'migrated')),
    window_start     timestamptz NOT NULL,
    window_end       timestamptz NOT NULL,
    tenant_id        text        NOT NULL DEFAULT 'replicant-system',  -- or a real tenant for per-tenant windows
    n                integer     NOT NULL,
    successes        integer     NULL,       -- for rates
    value            numeric     NOT NULL,   -- rate, median, p50, ratio
    ci_low           numeric     NULL,
    ci_high          numeric     NULL,
    computed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (env, metric, release_bundle_id, cohort, tenant_id, window_start, window_end)
);

CREATE TABLE evals.guardrail_evaluations (
    pipeline_id      text        NOT NULL,
    trigger_id       text        NOT NULL CHECK (trigger_id ~ '^R[0-9]{1,2}$'),
    evaluated_at     timestamptz NOT NULL,
    tenant_id        text        NOT NULL DEFAULT 'replicant-system',
    candidate_n      integer     NOT NULL,
    control_n        integer     NOT NULL,
    candidate_value  numeric     NULL,
    control_value    numeric     NULL,
    posterior_prob   numeric(6,5) NULL,
    fired            boolean     NOT NULL,
    action_taken     text        NULL,
    PRIMARY KEY (pipeline_id, trigger_id, evaluated_at)
);

-- ---------------------------------------------------------------------------------------------
-- Self-improvement (self-improvement.md)
-- ---------------------------------------------------------------------------------------------
CREATE TABLE evals.improvement_proposals (
    proposal_id        text        PRIMARY KEY CHECK (proposal_id ~ '^prop_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id          text        NOT NULL DEFAULT 'replicant-system',
    kind               text        NOT NULL CHECK (kind IN ('prompt', 'agent_definition', 'environment_definition',
                                     'graph', 'policy', 'golden_task', 'memory_policy')),
    target_ref         text        NOT NULL,
    failure_cluster    text        NOT NULL,
    evidence           jsonb       NOT NULL,
    state              text        NOT NULL CHECK (state IN ('drafted', 'bundle_built', 'evaluating', 'rejected_eval',
                                     'awaiting_review', 'accepted', 'declined', 'promoted', 'post_promotion_unconfirmed',
                                     'confirmed', 'expired')),
    candidate_bundle_id text       NULL,
    comparison_id      text        NULL,
    holdout_comparison_id text     NULL,
    proposer_job_id    text        NULL CHECK (proposer_job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
    reviewed_by        text        NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Failure taxonomy (self-improvement.md §2). Deterministic codes first; the LLM labeler may only add.
CREATE TABLE evals.failure_classifications (
    subject_kind      text        NOT NULL CHECK (subject_kind IN ('job', 'eval_case')),
    subject_ref       text        NOT NULL,   -- job_id, or eval_run_id:golden_task_id
    code              text        NOT NULL CHECK (code IN ('F-ENV', 'F-BUILD', 'F-TEST', 'F-SCOPE', 'F-MISREAD',
                                    'F-STYLE', 'F-INCOMPLETE', 'F-TOOL', 'F-BUDGET', 'F-RISK', 'F-COST', 'F-SAFETY')),
    tenant_id         text        NOT NULL,
    source            text        NOT NULL CHECK (source IN ('deterministic', 'llm')),
    workflow_type     text        NOT NULL,
    codebase_type     text        NOT NULL,
    release_bundle_id text        NOT NULL,
    cost_usd          numeric(10,4) NULL,
    human_minutes     integer     NULL,
    occurred_at       timestamptz NOT NULL,
    PRIMARY KEY (subject_kind, subject_ref, code)
);
CREATE INDEX failure_classifications_recent_idx ON evals.failure_classifications (occurred_at DESC, code);

CREATE VIEW evals.v_failure_clusters AS
SELECT code, workflow_type, codebase_type,
       count(*)                                         AS failures,
       coalesce(sum(cost_usd), 0)                       AS cost_usd,
       coalesce(sum(human_minutes), 0)                  AS human_minutes,
       count(*) * (coalesce(avg(cost_usd), 0) + coalesce(avg(human_minutes), 0) * 1.50) AS impact,
       (array_agg(subject_ref ORDER BY occurred_at DESC))[1:20] AS examples
FROM evals.failure_classifications
WHERE occurred_at > now() - interval '28 days'
GROUP BY code, workflow_type, codebase_type;

-- Component suites (R1-151, XD-04-07): WS-04 component evals run by eval-worker on a schedule.
CREATE TABLE evals.component_results (
    suite            text        NOT NULL CHECK (suite IN ('memory_extraction', 'memory_grounding', 'memory_dedupe',
                                   'memory_retrieval', 'memory_capacity_curve', 'memory_safety_classifier', 'memory_matcher')),
    run_id           text        NOT NULL CHECK (run_id ~ '^eval_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id        text        NOT NULL DEFAULT 'replicant-eval',
    subject_ref      text        NOT NULL,   -- release_bundle_id, or the memory-service build or model under test
    metric           text        NOT NULL,
    value            numeric     NOT NULL,
    ci_low           numeric     NULL,
    ci_high          numeric     NULL,
    n                integer     NOT NULL,
    details_key      text        NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (suite, run_id, metric)
);

-- Spend ceilings (R2-056, R2-061, R2-063): spend per period and category, refreshed every 10 min from the
-- cost ledger by budget account (fgw:eval:<eval_run_id>, fgw:pipeline:<pipeline_id>, fgw:proposal:<prop_id>).
CREATE TABLE evals.spend_periods (
    period_kind      text        NOT NULL CHECK (period_kind IN ('week', 'month')),
    period_start     date        NOT NULL,
    category         text        NOT NULL CHECK (category IN ('eval', 'shadow', 'judge', 'proposal', 'component', 'total')),
    tenant_id        text        NOT NULL DEFAULT 'replicant-eval',
    limit_usd        numeric(12,2) NOT NULL,
    spent_usd        numeric(12,2) NOT NULL DEFAULT 0,
    reserved_usd     numeric(12,2) NOT NULL DEFAULT 0,   -- in-flight suites' remaining maxUsd
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (period_kind, period_start, category)
);

-- Per-entry memory outcome counters are WS-04's (memory.entry_outcome_stats; XD-W2-07-05): jobs_read only
-- knows each job's memory_snapshot_id, so WS-06 keeps no per-entry table.

-- ---------------------------------------------------------------------------------------------
-- RLS and grants
-- ---------------------------------------------------------------------------------------------
ALTER TABLE evals.golden_tasks ENABLE ROW LEVEL SECURITY;
CREATE POLICY golden_tasks_platform ON evals.golden_tasks FOR SELECT
    USING (current_setting('replicant.principal_scope', true) IN ('operator:all-tenants', 'eval:platform'));
CREATE POLICY golden_tasks_writer ON evals.golden_tasks FOR ALL TO svc_eval_worker USING (true) WITH CHECK (true);

-- R1-153: holdout membership is visible to curators and eval-worker only (eval-harness.md §2.5).
ALTER TABLE evals.golden_set_members ENABLE ROW LEVEL SECURITY;
CREATE POLICY golden_set_members_gating ON evals.golden_set_members FOR SELECT
    USING (NOT in_holdout AND current_setting('replicant.principal_scope', true) IN ('operator:all-tenants', 'eval:platform'));
CREATE POLICY golden_set_members_holdout ON evals.golden_set_members FOR SELECT
    USING (in_holdout AND current_setting('replicant.principal_scope', true) = 'eval:platform');
CREATE POLICY golden_set_members_writer ON evals.golden_set_members FOR ALL TO svc_eval_worker USING (true) WITH CHECK (true);


-- Every remaining evals table with tenant_id: ENABLE + FORCE RLS; platform eval rows visible to eval-worker
-- (eval:platform) and to operator views (operator:all-tenants); writer policy for svc_eval_worker.
DO $rls$
DECLARE t text;
BEGIN
    FOR t IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'evals' AND c.relkind = 'r'
    LOOP
        EXECUTE format('ALTER TABLE evals.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE evals.%I FORCE ROW LEVEL SECURITY', t);
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'evals' AND tablename = t) THEN
            EXECUTE format($p$CREATE POLICY %I ON evals.%I FOR SELECT USING (
                tenant_id = NULLIF(current_setting('replicant.tenant_id', true), '')
                OR current_setting('replicant.principal_scope', true) IN ('operator:all-tenants', 'system:all-tenants', 'eval:platform'))$p$,
                t || '_read', t);
            EXECUTE format($p$CREATE POLICY %I ON evals.%I FOR ALL TO svc_eval_worker USING (true) WITH CHECK (true)$p$,
                t || '_writer', t);
        END IF;
    END LOOP;
END
$rls$;

GRANT USAGE ON SCHEMA evals TO svc_eval_worker, svc_control_plane_api, grafana_ro;
-- XD-FIX-ORCH-04: explicit table lists (never ON ALL TABLES IN SCHEMA); evals has no partitions.
GRANT SELECT, INSERT, UPDATE ON evals.golden_tasks, evals.golden_sets, evals.golden_set_members, evals.task_reliability,
    evals.judges, evals.anchor_sets, evals.anchor_items, evals.human_labels, evals.judge_calibrations,
    evals.eval_runs, evals.cases, evals.scores, evals.comparisons, evals.release_pipelines, evals.release_signoffs,
    evals.online_windows, evals.guardrail_evaluations, evals.improvement_proposals, evals.failure_classifications,
    evals.component_results, evals.spend_periods TO svc_eval_worker;
GRANT SELECT ON evals.v_failure_clusters TO svc_eval_worker;
GRANT SELECT ON evals.golden_tasks, evals.golden_sets, evals.golden_set_members, evals.task_reliability,
    evals.judges, evals.anchor_sets, evals.anchor_items, evals.human_labels, evals.judge_calibrations,
    evals.eval_runs, evals.cases, evals.scores, evals.comparisons, evals.release_pipelines, evals.release_signoffs,
    evals.online_windows, evals.guardrail_evaluations, evals.improvement_proposals, evals.failure_classifications,
    evals.component_results, evals.spend_periods, evals.v_failure_clusters TO svc_control_plane_api;
-- grafana_ro sees aggregates only for golden tasks (no task content columns).
GRANT SELECT ON evals.eval_runs, evals.comparisons, evals.online_windows, evals.guardrail_evaluations,
    evals.judge_calibrations, evals.release_pipelines TO grafana_ro;
REVOKE UPDATE ON evals.human_labels, evals.judge_calibrations, evals.guardrail_evaluations FROM svc_eval_worker;
