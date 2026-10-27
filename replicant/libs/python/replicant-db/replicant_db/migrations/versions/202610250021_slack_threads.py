"""slack_app.job_threads: the parent message of a job's thread in an approval/notification channel (v0 NOTIFY phase
2, DEVIATIONS NOTIFY-D13). The first channel event of a job posts the parent; later events reply in its thread.

Revision ID: 202610250021_slack_threads
Revises: 202610250020_notify_outbox
Create Date: 2026-10-25
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202610250021_slack_threads"
down_revision: str | None = "202610250020_notify_outbox"
branch_labels = None
depends_on = None

replicant_schema: str = "slack_app"
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None

UP = """
CREATE TABLE IF NOT EXISTS slack_app.job_threads (
    job_id     text NOT NULL CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
    channel_id text NOT NULL,
    parent_ts  text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, channel_id)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON slack_app.job_threads TO r_slack_app_write;
GRANT SELECT ON slack_app.job_threads TO r_slack_app_read;
-- the blocks a notification was posted with, so a gate message can be re-rendered in place (chat.update) when the
-- gate is decided in any channel (approvals.md §4.5)
ALTER TABLE slack_app.notification_posts ADD COLUMN IF NOT EXISTS blocks jsonb;
"""


def upgrade() -> None:
    owner_ddl.execute(UP)


def downgrade() -> None:
    owner_ddl.execute("ALTER TABLE slack_app.notification_posts DROP COLUMN IF EXISTS blocks; "
                      "DROP TABLE IF EXISTS slack_app.job_threads;")
