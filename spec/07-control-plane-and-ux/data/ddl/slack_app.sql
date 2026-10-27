-- Schema slack_app. Writer: slack-integration (svc_slack_integration). DDL owner: WS-07.
CREATE SCHEMA IF NOT EXISTS slack_app;

CREATE TABLE slack_app.message_refs (
    gate_id          text NOT NULL CHECK (gate_id ~ '^gate_[0-9a-hjkmnp-tv-z]{26}$'),
    channel_id       text NOT NULL,
    message_ts       text NOT NULL,
    purpose          text NOT NULL CHECK (purpose IN ('gate_channel', 'gate_dm', 'escalation', 'reminder_thread')),
    packet_revision  integer NOT NULL,
    presented_digest text NOT NULL CHECK (presented_digest ~ '^sha256:[0-9a-f]{64}$'),
    final_state      text,                                   -- set once chat.update applied the resolution (idempotency)
    dead             boolean NOT NULL DEFAULT false,         -- message_not_found
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, message_ts)
);
CREATE INDEX message_refs_by_gate ON slack_app.message_refs (gate_id);

CREATE TABLE slack_app.interaction_dedupe (
    team_id     text NOT NULL,
    user_id     text NOT NULL,
    action_ts   text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (team_id, user_id, action_ts)
);

CREATE TABLE slack_app.pending_decisions (
    decision_id      text PRIMARY KEY CHECK (decision_id ~ '^dec_[0-9a-hjkmnp-tv-z]{26}$'),
    gate_id          text NOT NULL,
    principal_id     text NOT NULL,
    slack_user_id    text NOT NULL,
    decision         text NOT NULL,
    presented_digest text NOT NULL,
    comment          text,
    response_url     text,                                   -- valid 30 min, max 5 uses (approvals.md §4.4)
    response_url_expires_at timestamptz,
    state            text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'retry', 'accepted', 'superseded', 'denied', 'rejected', 'dead')),
    result           jsonb,
    attempts         integer NOT NULL DEFAULT 0,
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX slack_pending_decisions_due ON slack_app.pending_decisions (next_attempt_at) WHERE state IN ('pending', 'retry');

-- Grant model v2 (XD-FIX-R1-160, C-PG 2.0.0): WS-10 owns roles, logins and schema USAGE; this file grants on
-- parent tables only (never ON ALL TABLES, never on a partition, no default privileges): reads to
-- r_<schema>_read, DML to r_<schema>_write, column-narrowed reads to a named login. Never to the login
-- svc_control_plane_api (its modules inherit r_<schema>_write; svc_cpa_read inherits the read roles).
GRANT SELECT, INSERT, UPDATE, DELETE ON slack_app.message_refs, slack_app.interaction_dedupe, slack_app.pending_decisions
    TO r_slack_app_write;
GRANT SELECT ON slack_app.message_refs TO r_slack_app_read;
