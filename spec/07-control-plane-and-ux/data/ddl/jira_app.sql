-- Schema jira_app. Writer: jira-integration (svc_jira_integration). DDL owner: WS-07.
CREATE SCHEMA IF NOT EXISTS jira_app;

CREATE TABLE jira_app.webhook_dedupe (
    site        text NOT NULL,
    event_id    text NOT NULL,                               -- Jira webhook event identifier (see triggers.md §3)
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site, event_id)
);

CREATE TABLE jira_app.issue_links (
    site        text NOT NULL,
    issue_key   text NOT NULL CHECK (issue_key ~ '^[A-Z][A-Z0-9_]+-[1-9][0-9]*$'),
    job_id      text NOT NULL CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id   text NOT NULL,
    comment_id  text,                                        -- living status comment
    created_at  timestamptz NOT NULL DEFAULT now(),
    terminal_at timestamptz,
    PRIMARY KEY (site, issue_key, job_id)
);

-- issue_links carries tenant_id: RLS enabled and forced (XD-FIX-R1-160). Jira webhooks are routed before the
-- tenant is known, so jira-integration (the only writer) sees every row.
SELECT ops.apply_tenant_rls('jira_app.issue_links');
CREATE POLICY rls_issue_links_sole_writer ON jira_app.issue_links AS PERMISSIVE FOR ALL TO svc_jira_integration
    USING (true) WITH CHECK (true);

-- Grant model v2 (XD-FIX-R1-160, C-PG 2.0.0): WS-10 owns roles, logins and schema USAGE; this file grants on
-- parent tables only (never ON ALL TABLES, never on a partition, no default privileges): reads to
-- r_<schema>_read, DML to r_<schema>_write, column-narrowed reads to a named login. Never to the login
-- svc_control_plane_api (its modules inherit r_<schema>_write; svc_cpa_read inherits the read roles).
GRANT SELECT, INSERT, UPDATE, DELETE ON jira_app.issue_links, jira_app.webhook_dedupe TO r_jira_app_write;
GRANT SELECT ON jira_app.issue_links TO r_jira_app_read;
