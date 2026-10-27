-- Schema github_app. Writer: github-integration (svc_github_integration). DDL owner: WS-07.
-- Also serves the Gitea adapter locally (provider column), per XD-11-05.
CREATE SCHEMA IF NOT EXISTS github_app;

CREATE TABLE github_app.installations (
    provider        text NOT NULL DEFAULT 'github' CHECK (provider IN ('github', 'gitea')),
    host            text NOT NULL DEFAULT 'github.com',
    installation_id bigint NOT NULL,                        -- GitHub installation id; synthetic for Gitea
    registration    text NOT NULL CHECK (registration IN ('prod', 'staging', 'dev')),
    account_login   text NOT NULL,
    account_id      bigint,
    tenant_id       text,                                    -- set when a tenant admin claims the installation
    status          text NOT NULL CHECK (status IN ('active', 'suspended', 'deleted')),
    permissions     jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, host, installation_id)
);

CREATE TABLE github_app.repos (
    provider        text NOT NULL DEFAULT 'github',
    host            text NOT NULL DEFAULT 'github.com',
    repo_id         text NOT NULL,                           -- immutable provider id (node id or numeric as text)
    installation_id bigint NOT NULL,
    full_name       text NOT NULL,                           -- owner/name; renames update it
    tenant_id       text,
    default_branch  text,
    archived        boolean NOT NULL DEFAULT false,
    removed_at      timestamptz,
    indexing_enabled boolean NOT NULL DEFAULT false,         -- opt-in for zoekt/ecosystem broker callers (CCR-09-06)
    PRIMARY KEY (provider, host, repo_id)
);
CREATE INDEX repos_by_name ON github_app.repos (host, lower(full_name));

CREATE TABLE github_app.repo_settings (
    provider    text NOT NULL DEFAULT 'github',
    host        text NOT NULL DEFAULT 'github.com',
    repo_id     text NOT NULL,
    version     integer NOT NULL,
    settings    jsonb NOT NULL,                              -- github-app.md §7.1
    is_current  boolean NOT NULL DEFAULT true,
    updated_by  text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, host, repo_id, version)
);
CREATE UNIQUE INDEX repo_settings_current_uq ON github_app.repo_settings (provider, host, repo_id) WHERE is_current;

-- Durable inbound queue keyed by X-GitHub-Delivery (or Gitea delivery id). 7-day retention.
CREATE TABLE github_app.inbound_deliveries (
    delivery_guid   text PRIMARY KEY,
    provider        text NOT NULL DEFAULT 'github',
    event           text NOT NULL,
    hook_id         bigint,
    target_type     text,
    target_id       text,
    source          text NOT NULL CHECK (source IN ('webhook', 'deliveries_api', 'manual_replay')),
    payload         bytea NOT NULL,
    payload_sha256  text NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    state           text NOT NULL DEFAULT 'received'
                    CHECK (state IN ('received', 'processing', 'retry', 'done', 'ignored', 'dead')),
    outcome         text,                                    -- job_created, duplicate, pr_event_signalled, decision_sent, ignored_bot, unlinked, ...
    attempts        integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    claimed_by      text,
    claimed_at      timestamptz,
    last_error      text,
    processed_at    timestamptz
);
CREATE INDEX inbound_due ON github_app.inbound_deliveries (next_attempt_at) WHERE state IN ('received', 'retry');
CREATE INDEX inbound_retention ON github_app.inbound_deliveries (received_at);

CREATE TABLE github_app.reconciler_checkpoints (
    registration          text PRIMARY KEY,
    last_seen_delivery_id bigint,
    last_run_at           timestamptz
);

-- Decision outbox (C-MSG §3) for /replicant commands and check-run actions.
CREATE TABLE github_app.pending_decisions (
    decision_id      text PRIMARY KEY CHECK (decision_id ~ '^dec_[0-9a-hjkmnp-tv-z]{26}$'),
    gate_id          text NOT NULL,
    delivery_guid    text NOT NULL,
    principal_id     text NOT NULL,
    decision         text NOT NULL,
    presented_digest text NOT NULL,
    comment          text,
    state            text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'retry', 'accepted', 'superseded', 'denied', 'rejected', 'dead')),
    result           jsonb,
    attempts         integer NOT NULL DEFAULT 0,
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX gh_pending_decisions_due ON github_app.pending_decisions (next_attempt_at) WHERE state IN ('pending', 'retry');

-- Comments and check runs the App posted (edited in place; stale-view detection uses presented_digest).
CREATE TABLE github_app.comment_refs (
    ref_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider         text NOT NULL DEFAULT 'github',
    repo_id          text NOT NULL,
    number           integer NOT NULL,                       -- issue or PR number
    purpose          text NOT NULL CHECK (purpose IN ('ack', 'gate', 'unlinked_reply', 'repair_reply', 'superseded_reply', 'check_run_gate', 'check_run_qa')),
    job_id           text,
    gate_id          text,
    external_id      bigint NOT NULL,                        -- comment id or check run id
    presented_digest text,
    delivery_guid    text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX comment_refs_once ON github_app.comment_refs (repo_id, number, purpose, coalesce(gate_id, ''), coalesce(delivery_guid, ''));

-- Rate limiting of replies to unlinked users (one per user/repo/24 h).
CREATE TABLE github_app.unlinked_replies (
    provider   text NOT NULL,
    user_id    bigint NOT NULL,
    repo_id    text NOT NULL,
    replied_at timestamptz NOT NULL,
    PRIMARY KEY (provider, user_id, repo_id)
);

-- installations and repos carry tenant_id (NULL until a tenant admin claims the installation): RLS enabled and
-- forced (XD-FIX-R1-160). Webhook routing happens before the tenant is known, so github-integration (the only
-- writer) sees every row; the token broker's column-limited read needs every row too (CCR-07-11).
SELECT ops.apply_tenant_rls('github_app.installations');
CREATE POLICY rls_installations_sole_writer ON github_app.installations AS PERMISSIVE FOR ALL TO svc_github_integration
    USING (true) WITH CHECK (true);
SELECT ops.apply_tenant_rls('github_app.repos');
CREATE POLICY rls_repos_sole_writer ON github_app.repos AS PERMISSIVE FOR ALL TO svc_github_integration
    USING (true) WITH CHECK (true);
CREATE POLICY rls_repos_token_broker ON github_app.repos AS PERMISSIVE FOR SELECT TO svc_github_token_broker USING (true);

-- Grant model v2 (XD-FIX-R1-160, C-PG 2.0.0): WS-10 owns roles, logins and schema USAGE; this file grants on
-- parent tables only (never ON ALL TABLES, never on a partition, no default privileges): reads to
-- r_<schema>_read, DML to r_<schema>_write, column-narrowed reads to a named login. Never to the login
-- svc_control_plane_api (its modules inherit r_<schema>_write; svc_cpa_read inherits the read roles).
GRANT SELECT, INSERT, UPDATE, DELETE ON github_app.installations, github_app.repos, github_app.repo_settings,
    github_app.inbound_deliveries, github_app.pending_decisions, github_app.comment_refs, github_app.reconciler_checkpoints,
    github_app.unlinked_replies TO r_github_app_write;
GRANT SELECT ON github_app.installations, github_app.repos, github_app.repo_settings TO r_github_app_read;
-- github-token-broker filters indexer callers on opted-in repos (CCR-09-06, R1-186; C-PG 1.4.0 via CCR-07-11).
GRANT USAGE ON SCHEMA github_app TO svc_github_token_broker;
GRANT SELECT (provider, host, repo_id, installation_id, tenant_id, archived, removed_at, indexing_enabled) ON github_app.repos TO svc_github_token_broker;
