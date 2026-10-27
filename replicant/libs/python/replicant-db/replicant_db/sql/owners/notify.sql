-- Schema notify. Writer: control-plane-api (svc_cpa_notify). DDL owner: WS-07.
CREATE SCHEMA IF NOT EXISTS notify;

CREATE TABLE notify.preferences (
    principal_id text PRIMARY KEY CHECK (principal_id ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
    version      integer NOT NULL DEFAULT 1,
    prefs        jsonb NOT NULL,                              -- schemas/notification-preferences.schema.json
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Delivery log: one row per (event, recipient, channel). Dedupe key makes notify activities idempotent.
CREATE TABLE notify.deliveries (
    delivery_id  text PRIMARY KEY,                            -- ntf_<derived from dedupe_key>
    dedupe_key   text NOT NULL UNIQUE,                        -- <event_key>:<subject id>:<recipient>:<channel>
    tenant_id    text NOT NULL,
    event_key    text NOT NULL,                               -- notifications.md §2
    job_id       text,
    gate_id      text,
    recipient    text NOT NULL,
    -- One delivery-channel vocabulary (R1-179). Preferences may also say 'originating' (resolved to the job's
    -- source channel before a delivery row is written) or 'none'; neither appears here.
    channel      text NOT NULL CONSTRAINT ck_deliveries_channel CHECK (channel IN ('ui', 'slack_dm', 'slack_thread', 'slack_channel', 'github_comment', 'jira_comment', 'digest')),
    state        text NOT NULL CHECK (state IN ('queued', 'sent', 'suppressed', 'failed', 'digested')),
    suppressed_reason text,
    external_ref text,                                        -- Slack ts, GitHub comment id
    created_at   timestamptz NOT NULL DEFAULT now(),
    sent_at      timestamptz
);
CREATE INDEX deliveries_recipient ON notify.deliveries (recipient, created_at DESC);
CREATE INDEX deliveries_digest_due ON notify.deliveries (recipient) WHERE state = 'queued' AND channel = 'digest';

-- In-app notification feed (UI bell), retained 90 days.
CREATE TABLE notify.feed (
    item_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recipient  text NOT NULL,
    tenant_id  text NOT NULL,
    event_key  text NOT NULL,
    summary    text NOT NULL CHECK (length(summary) <= 300),
    href       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    read_at    timestamptz
);
CREATE INDEX feed_unread ON notify.feed (recipient, created_at DESC) WHERE read_at IS NULL;

SELECT ops.apply_tenant_rls('notify.deliveries');
SELECT ops.apply_tenant_rls('notify.feed');

-- Grant model v2 (XD-FIX-R1-160, C-PG 2.0.0): WS-10 owns roles, logins and schema USAGE; this file grants on
-- parent tables only (never ON ALL TABLES, never on a partition, no default privileges): reads to
-- r_<schema>_read, DML to r_<schema>_write, column-narrowed reads to a named login. Never to the login
-- svc_control_plane_api (its modules inherit r_<schema>_write; svc_cpa_read inherits the read roles).
GRANT SELECT, INSERT, UPDATE, DELETE ON notify.preferences, notify.deliveries, notify.feed TO r_notify_write;
GRANT SELECT ON notify.preferences, notify.deliveries, notify.feed TO r_notify_read;
