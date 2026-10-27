"""memory: v0 source vocabularies for memory-service (DEVIATIONS MEM-D3, MEM-D4).

* provenance.source_kind += `gate_comment` (approver request_changes / reject comments) and `review_finding`
  (recurring reviewer-agent findings); repo scans use the existing `import`, QA recurrence `qa_failure`.
* provenance.source_url accepts http:// as well (local Gitea serves http on 127.0.0.1:3300 / gitea:3300).
* feedback_items.source_key += prefixes `gate:`, `review:`, `scan:` (ledger rows for those sources).
Expand-only CHECK swaps (NOT VALID then VALIDATE, so existing rows are checked without a long lock).

Revision ID: 202610270040_memory_v0
Revises: 202610270030_audit_events_force
Create Date: 2026-10-27
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202610270040_memory_v0"
down_revision: str | None = "202610270030_audit_events_force"
branch_labels = None
depends_on = None

replicant_schema: str = "memory"
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None

KINDS = ("'pr_review_comment', 'pr_review_body', 'pr_issue_comment', 'replicant_command', 'job_outcome', "
         "'qa_failure', 'repair_rejection', 'agent_proposal', 'curator', 'import'")

UP = f"""
ALTER TABLE memory.provenance DROP CONSTRAINT IF EXISTS provenance_source_kind_check;
ALTER TABLE memory.provenance ADD CONSTRAINT provenance_source_kind_check
  CHECK (source_kind IN ({KINDS}, 'gate_comment', 'review_finding')) NOT VALID;
ALTER TABLE memory.provenance VALIDATE CONSTRAINT provenance_source_kind_check;
ALTER TABLE memory.provenance DROP CONSTRAINT IF EXISTS provenance_source_url_check;
ALTER TABLE memory.provenance ADD CONSTRAINT provenance_source_url_check
  CHECK (source_url ~ '^https?://[a-z0-9.:-]+/' AND length(source_url) <= 2048) NOT VALID;
ALTER TABLE memory.provenance VALIDATE CONSTRAINT provenance_source_url_check;
ALTER TABLE memory.feedback_items DROP CONSTRAINT IF EXISTS feedback_items_source_key_check;
ALTER TABLE memory.feedback_items ADD CONSTRAINT feedback_items_source_key_check
  CHECK (source_key ~ '^(gh-review-comment|gh-review|gh-issue-comment|agent|qa|outcome|gate|review|scan):[A-Za-z0-9_:./-]{{1,200}}$') NOT VALID;
ALTER TABLE memory.feedback_items VALIDATE CONSTRAINT feedback_items_source_key_check;
"""

DOWN = f"""
ALTER TABLE memory.provenance DROP CONSTRAINT IF EXISTS provenance_source_kind_check;
ALTER TABLE memory.provenance ADD CONSTRAINT provenance_source_kind_check CHECK (source_kind IN ({KINDS})) NOT VALID;
ALTER TABLE memory.provenance DROP CONSTRAINT IF EXISTS provenance_source_url_check;
ALTER TABLE memory.provenance ADD CONSTRAINT provenance_source_url_check
  CHECK (source_url ~ '^https://[a-z0-9.-]+/' AND length(source_url) <= 2048) NOT VALID;
ALTER TABLE memory.feedback_items DROP CONSTRAINT IF EXISTS feedback_items_source_key_check;
ALTER TABLE memory.feedback_items ADD CONSTRAINT feedback_items_source_key_check
  CHECK (source_key ~ '^(gh-review-comment|gh-review|gh-issue-comment|agent|qa|outcome):[A-Za-z0-9_:./-]{{1,200}}$') NOT VALID;
"""


def upgrade() -> None:
    owner_ddl.execute(UP)


def downgrade() -> None:
    owner_ddl.execute(DOWN)
