"""web.ui_preferences: per-user UI preferences (v0 WEB-UIPREF, DEVIATIONS WEB-UIPREF-D1). Holds the job view
(`{job_view: "simple" | "detailed"}`, schemas/ui-preferences.schema.json). Kept apart from notify.preferences, whose
schema is closed. Shaped like notify.preferences (per principal, not tenant-scoped, so no tenant RLS); written by
control-plane-api's svc_cpa_notify module role (the per-user /v1/me writer), read through svc_cpa_read.

Revision ID: 202610270001_ui_preferences
Revises: 202610250021_slack_threads
Create Date: 2026-10-27
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202610270001_ui_preferences"
down_revision: str | None = "202610250021_slack_threads"
branch_labels = None
depends_on = None

replicant_schema: str = "multi"  # new schema web + its roles; ck_migration_log_schema has no "web"
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None

UP = """
CREATE SCHEMA IF NOT EXISTS web AUTHORIZATION replicant_migrator;
REVOKE ALL ON SCHEMA web FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE replicant_migrator IN SCHEMA web REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'r_web_read') THEN CREATE ROLE r_web_read NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'r_web_write') THEN CREATE ROLE r_web_write NOLOGIN; END IF;
END
$$;
GRANT USAGE ON SCHEMA web TO r_web_read, r_web_write, svc_cpa_read, svc_cpa_notify;

CREATE TABLE IF NOT EXISTS web.ui_preferences (
    principal_id text PRIMARY KEY CHECK (principal_id ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
    version      integer NOT NULL DEFAULT 1,
    prefs        jsonb NOT NULL,                              -- schemas/ui-preferences.schema.json
    updated_at   timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE, DELETE ON web.ui_preferences TO r_web_write;
GRANT SELECT ON web.ui_preferences TO r_web_read;
GRANT r_web_read TO svc_cpa_read WITH INHERIT TRUE;
GRANT r_web_write TO svc_cpa_notify WITH INHERIT TRUE;
"""

DOWN = """
DROP TABLE IF EXISTS web.ui_preferences;
DROP SCHEMA IF EXISTS web;
"""


def upgrade() -> None:
    owner_ddl.execute(UP)


def downgrade() -> None:
    owner_ddl.execute(DOWN)
