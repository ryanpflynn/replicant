-- Schema intake. Writer: control-plane-api (module role svc_cpa_intake). DDL owner: WS-07.
CREATE SCHEMA IF NOT EXISTS intake;

CREATE TABLE intake.job_requests (
    job_id            text COLLATE "C" PRIMARY KEY CONSTRAINT ck_job_requests_job_id CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id         text COLLATE "C" NOT NULL CONSTRAINT ck_job_requests_tenant CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'),
    request_id        text COLLATE "C" NOT NULL CONSTRAINT ck_job_requests_request_id CHECK (request_id ~ '^jrq_[0-9a-hjkmnp-tv-z]{26}$'),
    dedupe_key        text NOT NULL CONSTRAINT ck_job_requests_dedupe CHECK (length(dedupe_key) <= 256 AND dedupe_key ~ '^[a-z]+:[A-Za-z0-9._:/@#-]+$'),
    channel           text NOT NULL CONSTRAINT ck_job_requests_channel CHECK (channel IN ('github', 'jira', 'slack', 'ui', 'cli', 'schedule', 'api', 'eval')),
    requester         text NOT NULL CONSTRAINT ck_job_requests_requester CHECK (requester ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
    on_behalf_of      text CONSTRAINT ck_job_requests_obo CHECK (on_behalf_of ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
    submitted_by      text NOT NULL,                     -- authenticated submitter (person or channel adapter)
    workflow_type     text NOT NULL,
    workflow_type_started text NOT NULL DEFAULT 'ReplicantJob' CONSTRAINT ck_job_requests_wf_type CHECK (workflow_type_started IN ('ReplicantJob', 'MaintenanceBatch')),
    evaluation_mode   text CONSTRAINT ck_job_requests_eval CHECK (evaluation_mode IN ('eval', 'shadow')),   -- C-JOBREQ 1.2.0 options.evaluation
    pr_repo_id        text,                              -- set for pr_repair jobs: per-PR repair cap (R2-059)
    pr_number         integer,
    request           jsonb NOT NULL,                    -- the C-JOBREQ document as accepted
    release_bundle_id text NOT NULL CONSTRAINT ck_job_requests_bundle CHECK (release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
    -- Exactly WS-06's vocabulary (release-bundle.md §8.3, XD-FIX-R1-158). No fallback reason: intake never falls
    -- back to the default bundle (XD-FIX-R1-137a; WS-01 confirms in XD-W2-01-03).
    assignment_reason text NOT NULL CONSTRAINT ck_job_requests_assignment_reason CHECK (assignment_reason ~
        '^(default|eval_pin|shadow_pin|explicit_pin|ramp:(canary|holdback|experiment):[0-2]|tenant_override:(holdback_tenant|pinned_by_operator|layer_pending))(\+layer)?$'),
    -- C-IDS 1.10.0 §11 release_cohort closed set (CCR-06-04); derived from assignment_reason and evaluation_mode at intake:
    release_cohort    text NOT NULL CONSTRAINT ck_job_requests_cohort CHECK (release_cohort IN
        ('default', 'candidate', 'holdback', 'experiment', 'override', 'migrated', 'eval', 'shadow')),
    assignment_version integer NOT NULL,
    start_status      text NOT NULL DEFAULT 'pending' CONSTRAINT ck_job_requests_start_status CHECK (start_status IN ('pending', 'started')),
    start_attempts    integer NOT NULL DEFAULT 0,
    start_last_error  text,
    received_at       timestamptz NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    started_at        timestamptz,
    terminal_at       timestamptz,                       -- set from projection by a reconcile task; drives dedupe pruning
    dedupe_pruned     boolean NOT NULL DEFAULT false,
    CONSTRAINT ck_job_requests_pr CHECK ((pr_repo_id IS NULL) = (pr_number IS NULL))
);
-- C-JOBREQ: (tenant_id, dedupe_key) unique while not pruned (pruned 90 days after terminal; C-IDS).
CREATE UNIQUE INDEX job_requests_dedupe_uq ON intake.job_requests (tenant_id, dedupe_key) WHERE NOT dedupe_pruned;
CREATE INDEX job_requests_pending ON intake.job_requests (created_at) WHERE start_status = 'pending';
CREATE INDEX job_requests_pr ON intake.job_requests (pr_repo_id, pr_number) WHERE pr_repo_id IS NOT NULL;

-- Update outbox for UI/CLI human actions (C-MSG §3), including release.* Updates on ReleasePipeline
-- workflows (R1-174): the target is a workflow ID (job_… or rpl_…).
CREATE TABLE intake.pending_actions (
    action_ref      text PRIMARY KEY,                   -- decision_id | message_id | action_id (Temporal Update ID)
    kind            text NOT NULL CONSTRAINT ck_pending_actions_kind CHECK (kind IN (
                        'gate.decide', 'job.steer', 'job.cancel', 'job.pause', 'job.resume', 'job.takeover',
                        'job.offer_bundle_migration', 'batch.cancel', 'ops.retry_node',
                        'release.signoff', 'release.abort', 'release.hold')),
    workflow_id     text NOT NULL CONSTRAINT ck_pending_actions_target CHECK (workflow_id ~ '^(job|rpl)_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id       text,                               -- NULL for release pipelines (platform scope)
    actor           text NOT NULL,
    payload         jsonb NOT NULL,
    state           text NOT NULL DEFAULT 'pending' CONSTRAINT ck_pending_actions_state CHECK (state IN ('pending', 'retry', 'done', 'rejected', 'dead', 'abandoned')),
    result          jsonb,
    attempts        integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz
);
CREATE INDEX pending_actions_due ON intake.pending_actions (next_attempt_at) WHERE state IN ('pending', 'retry');

-- Local-only seed resume markers (exists in all environments as a table; only dev images have the endpoints).
CREATE TABLE intake.dev_seed_status (
    step       text PRIMARY KEY CONSTRAINT ck_dev_seed_status_step CHECK (step ~ '^[0-9]{2}-[a-z][a-z0-9-]{1,40}$'),
    state      text NOT NULL CONSTRAINT ck_dev_seed_status_state CHECK (state IN ('pending', 'done', 'failed')),
    detail     text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

SELECT ops.apply_tenant_rls('intake.job_requests');
-- pending_actions (the decision/steering outbox) carries tenant_id, NULL for release pipelines: RLS enabled and
-- forced (XD-FIX-R1-160). The outbox sender (svc_cpa_intake) drains every tenant's rows.
SELECT ops.apply_tenant_rls('intake.pending_actions');
CREATE POLICY rls_pending_actions_sole_writer ON intake.pending_actions AS PERMISSIVE FOR ALL TO svc_cpa_intake
    USING (true) WITH CHECK (true);

-- Grant model v2 (XD-FIX-R1-160, C-PG 2.0.0): WS-10 owns roles, logins and schema USAGE; this file grants on
-- parent tables only (never ON ALL TABLES, never on a partition, no default privileges): reads to
-- r_<schema>_read, DML to r_<schema>_write, column-narrowed reads to a named login. Never to the login
-- svc_control_plane_api (its modules inherit r_<schema>_write; svc_cpa_read inherits the read roles).
GRANT SELECT, INSERT, UPDATE ON intake.job_requests, intake.pending_actions, intake.dev_seed_status TO r_intake_write;
GRANT SELECT ON intake.job_requests TO r_intake_read;
-- grafana_ro (C-PG rule 6 reader of intake, R1-186): aggregate columns only, never the request body.
GRANT SELECT (job_id, tenant_id, channel, workflow_type, workflow_type_started, evaluation_mode, release_bundle_id,
              assignment_reason, release_cohort, start_status, received_at, created_at, started_at, terminal_at)
      ON intake.job_requests TO grafana_ro;
