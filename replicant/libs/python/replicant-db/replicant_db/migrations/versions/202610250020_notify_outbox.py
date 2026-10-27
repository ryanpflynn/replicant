"""Notification fan-out outbox (v0 NOTIFY, DEVIATIONS NOTIFY-D1): expand-only additions to WS-07's DDL.

* notify.deliveries gains the outbox columns the dispatcher needs (rendered payload, attempts, next attempt, last
  error). notifications.md N-1 says deliveries are retried and deduplicated, but notify.sql has no retry state.
* slack_app.notification_posts: slack-integration's idempotency record per delivery (one Slack post per
  notify.deliveries row, whatever the retries). slack_app.message_refs is gate-only.
* github_app.comment_refs.purpose admits 'notification' (issue comments posted for notify deliveries; the
  delivery_id goes in delivery_guid, so the unique index makes a redelivery a no-op).

Revision ID: 202610250020_notify_outbox
Revises: 202610240019_eval_reader
Create Date: 2026-10-25
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202610250020_notify_outbox"
down_revision: str | None = "202610240019_eval_reader"
branch_labels = None
depends_on = None

replicant_schema: str = "multi"
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None

UP = """
ALTER TABLE notify.deliveries
    ADD COLUMN IF NOT EXISTS payload         jsonb,
    ADD COLUMN IF NOT EXISTS attempts        integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS last_error      text;
CREATE INDEX IF NOT EXISTS deliveries_dispatch_due ON notify.deliveries (next_attempt_at)
    WHERE state = 'queued' AND channel <> 'digest';
CREATE INDEX IF NOT EXISTS deliveries_by_job ON notify.deliveries (job_id, event_key);

CREATE TABLE IF NOT EXISTS slack_app.notification_posts (
    delivery_id  text PRIMARY KEY CHECK (delivery_id ~ '^ntf_[0-9a-hjkmnp-tv-z]{26}$'),
    principal_id text NOT NULL,
    slack_user_id text NOT NULL,
    channel_id   text NOT NULL,
    message_ts   text NOT NULL,
    event_key    text NOT NULL,
    posted_at    timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE, DELETE ON slack_app.notification_posts TO r_slack_app_write;
GRANT SELECT ON slack_app.notification_posts TO r_slack_app_read;

ALTER TABLE github_app.comment_refs DROP CONSTRAINT IF EXISTS comment_refs_purpose_check;
ALTER TABLE github_app.comment_refs ADD CONSTRAINT comment_refs_purpose_check CHECK (purpose IN (
    'ack', 'gate', 'unlinked_reply', 'repair_reply', 'superseded_reply', 'check_run_gate', 'check_run_qa',
    'notification'));
"""

DOWN = """
ALTER TABLE github_app.comment_refs DROP CONSTRAINT IF EXISTS comment_refs_purpose_check;
DELETE FROM github_app.comment_refs WHERE purpose = 'notification';
ALTER TABLE github_app.comment_refs ADD CONSTRAINT comment_refs_purpose_check CHECK (purpose IN (
    'ack', 'gate', 'unlinked_reply', 'repair_reply', 'superseded_reply', 'check_run_gate', 'check_run_qa'));
DROP TABLE IF EXISTS slack_app.notification_posts;
DROP INDEX IF EXISTS notify.deliveries_by_job;
DROP INDEX IF EXISTS notify.deliveries_dispatch_due;
ALTER TABLE notify.deliveries DROP COLUMN IF EXISTS last_error, DROP COLUMN IF EXISTS next_attempt_at,
    DROP COLUMN IF EXISTS attempts, DROP COLUMN IF EXISTS payload;
"""


def upgrade() -> None:
    owner_ddl.execute(UP)


def downgrade() -> None:
    owner_ddl.execute(DOWN)
