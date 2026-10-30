"""memory: evidence independence, PR-outcome holds, source trust and lifecycle review (DEVIATIONS MEM-D11..MEM-D16).

* provenance.independence_key is redefined (MEM-D11): sha256('v2|' || author || '|' || normalized excerpt) for rows with
  an author, sha256('v2|machine|' || kind || '|' || job) for reviewer/QA recurrence. The one-row-per-source
  uniqueness moves to the new `source_key_digest` (the old independence_key value, sha256 of the ledger source key), so
  repeats are kept as provenance but collapse onto one independence key.
* provenance.source_trust human | machine (MEM-D13); review_finding / qa_failure rows are machine.
* provenance.source_kind += human_edit; feedback_items.source_key prefix += edit: (parallel fix "human edits").
* entries.hold_reason += awaiting_merge; entries.pr_outcome (MEM-D12) records how the PR of a PR-derived candidate ended.
* state_transitions.actor_kind += system_outcome: memory-service auto-rejects a held candidate whose PR closed unmerged
  or whose fix did not survive to the merge. It can only reject (tightening), never activate.
* entry_flags: source += lifecycle, reason_code += paths_churned, lint_config_changed (MEM-D14).
* curator_actions.action += mark_reviewed (admin re-checked an entry that was due or flagged).
* job_outcomes.changed_paths: files the job's PR changed (path churn signal).
* Backfill: independence keys, counters and the Beta alpha of every entry are recomputed so repeated identical comments
  no longer count as independent evidence; machine-only candidates get the lower prior (beta + 1).

Revision ID: 202610300010_memory_trust
Revises: 202610270040_memory_v0
Create Date: 2026-10-30
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202610300010_memory_trust"
down_revision: str | None = "202610270040_memory_v0"
branch_labels = None
depends_on = None

replicant_schema: str = "memory"
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None

KINDS = ("'pr_review_comment', 'pr_review_body', 'pr_issue_comment', 'replicant_command', 'job_outcome', "
         "'qa_failure', 'repair_rejection', 'agent_proposal', 'curator', 'import', 'gate_comment', 'review_finding'")
PREFIXES = "gh-review-comment|gh-review|gh-issue-comment|agent|qa|outcome|gate|review|scan"
HOLDS = "'author_correction', 'codeowner_correction', 'human_flags', 'post_merge_reverted', 'post_merge_wrong_fix', 'agent_flags'"
ACTORS = "'admin', 'system_policy', 'system_safety', 'leak_purge'"
FLAG_SOURCES = "'ui_job_view', 'ui_public_view', 'pr_command', 'handoff'"
ACTIONS = ("'approve', 'reject', 'edit', 'create', 'disable', 'enable', 'deprecate', 'restore', 'promote_scope', "
           "'resolve_conflict', 'dismiss_conflict', 'quarantine', 'release_quarantine', 'bulk_disable', 'purge', 'export', "
           "'restrict_author', 'unrestrict_author', 'update_governance', 'second_approval'")
FLAG_REASONS = "'wrong', 'misread_comment', 'too_broad', 'outdated', 'harmful', 'unsafe', 'other'"

# normalization shared with memory_service.candidates.independence_key (lowercase, whitespace collapsed, trimmed)
NORM = "btrim(regexp_replace(lower(coalesce(p.source_excerpt, '')), '\\s+', ' ', 'g'))"
DIGEST = "'sha256:' || encode(sha256(convert_to({x}, 'UTF8')), 'hex')"

UP = f"""
ALTER TABLE memory.provenance ADD COLUMN source_key_digest text CHECK (source_key_digest ~ '^sha256:[0-9a-f]{{64}}$');
ALTER TABLE memory.provenance ADD COLUMN source_trust text NOT NULL DEFAULT 'human' CHECK (source_trust IN ('human', 'machine'));
DROP INDEX IF EXISTS memory.provenance_one_per_source;
CREATE UNIQUE INDEX provenance_one_per_source_key ON memory.provenance (entry_id, source_key_digest)
  WHERE withdrawn_at IS NULL AND source_key_digest IS NOT NULL;
CREATE INDEX provenance_independence ON memory.provenance (entry_id, independence_key) WHERE withdrawn_at IS NULL;

ALTER TABLE memory.provenance DROP CONSTRAINT IF EXISTS provenance_source_kind_check;
ALTER TABLE memory.provenance ADD CONSTRAINT provenance_source_kind_check
  CHECK (source_kind IN ({KINDS}, 'human_edit')) NOT VALID;
ALTER TABLE memory.provenance VALIDATE CONSTRAINT provenance_source_kind_check;
ALTER TABLE memory.feedback_items DROP CONSTRAINT IF EXISTS feedback_items_source_key_check;
ALTER TABLE memory.feedback_items ADD CONSTRAINT feedback_items_source_key_check
  CHECK (source_key ~ '^({PREFIXES}|edit):[A-Za-z0-9_:./-]{{1,200}}$') NOT VALID;
ALTER TABLE memory.feedback_items VALIDATE CONSTRAINT feedback_items_source_key_check;

ALTER TABLE memory.entries DROP CONSTRAINT IF EXISTS entries_hold_reason_check;
ALTER TABLE memory.entries ADD CONSTRAINT entries_hold_reason_check
  CHECK (hold_reason IN ({HOLDS}, 'awaiting_merge')) NOT VALID;
ALTER TABLE memory.entries VALIDATE CONSTRAINT entries_hold_reason_check;
ALTER TABLE memory.entries ADD COLUMN pr_outcome text
  CHECK (pr_outcome IN ('awaiting_merge', 'merged_with_fix', 'merged_unverified', 'closed_unmerged', 'fix_reverted'));

ALTER TABLE memory.state_transitions DROP CONSTRAINT IF EXISTS state_transitions_actor_kind_check;
ALTER TABLE memory.state_transitions ADD CONSTRAINT state_transitions_actor_kind_check
  CHECK (actor_kind IN ({ACTORS}, 'system_outcome')) NOT VALID;
ALTER TABLE memory.state_transitions VALIDATE CONSTRAINT state_transitions_actor_kind_check;
ALTER TABLE memory.state_transitions ADD CONSTRAINT st_outcome_rejects
  CHECK (actor_kind <> 'system_outcome' OR (actor_principal_id = 'svc_memory-service' AND to_state = 'rejected')) NOT VALID;
ALTER TABLE memory.state_transitions VALIDATE CONSTRAINT st_outcome_rejects;

ALTER TABLE memory.entry_flags DROP CONSTRAINT IF EXISTS entry_flags_source_check;
ALTER TABLE memory.entry_flags ADD CONSTRAINT entry_flags_source_check CHECK (source IN ({FLAG_SOURCES}, 'lifecycle')) NOT VALID;
ALTER TABLE memory.entry_flags VALIDATE CONSTRAINT entry_flags_source_check;
ALTER TABLE memory.entry_flags DROP CONSTRAINT IF EXISTS entry_flags_reason_code_check;
ALTER TABLE memory.entry_flags ADD CONSTRAINT entry_flags_reason_code_check
  CHECK (reason_code IN ({FLAG_REASONS}, 'paths_churned', 'lint_config_changed')) NOT VALID;
ALTER TABLE memory.entry_flags VALIDATE CONSTRAINT entry_flags_reason_code_check;

ALTER TABLE memory.curator_actions DROP CONSTRAINT IF EXISTS curator_actions_action_check;
ALTER TABLE memory.curator_actions ADD CONSTRAINT curator_actions_action_check CHECK (action IN ({ACTIONS}, 'mark_reviewed')) NOT VALID;
ALTER TABLE memory.curator_actions VALIDATE CONSTRAINT curator_actions_action_check;

ALTER TABLE memory.job_outcomes ADD COLUMN changed_paths text[] NOT NULL DEFAULT '{{}}' CHECK (cardinality(changed_paths) <= 500);

"""

# Data backfill. Every memory table has FORCE ROW LEVEL SECURITY: without the admin:memory principal scope the
# migrator sees no rows (transaction-local set_config, cleared at the end).
DATA = f"""
SELECT set_config('replicant.principal_scope', 'admin:memory', true);
UPDATE memory.provenance SET source_key_digest = independence_key WHERE source_key_digest IS NULL;
UPDATE memory.provenance SET source_trust = 'machine' WHERE source_kind IN ('review_finding', 'qa_failure');
UPDATE memory.provenance p SET independence_key = {DIGEST.format(x="'v2|' || p.author_principal_id || '|' || " + NORM)}
 WHERE p.author_principal_id IS NOT NULL AND p.source_excerpt IS NOT NULL;
UPDATE memory.provenance p SET independence_key = {DIGEST.format(x="'v2|machine|' || p.source_kind || '|' || coalesce(p.job_id, '')")}
 WHERE p.source_trust = 'machine';
-- counters and alpha from independent evidence only (alpha loses the increments the collapsed repeats added)
WITH c AS (
  SELECT p.entry_id, count(*) AS n, count(DISTINCT p.independence_key) AS ind,
         count(DISTINCT p.author_principal_id) AS authors
    FROM memory.provenance p WHERE p.withdrawn_at IS NULL GROUP BY p.entry_id)
UPDATE memory.entries e SET evidence_count = c.n, independent_evidence_count = c.ind, distinct_author_count = c.authors,
       confidence_alpha = greatest(1, e.confidence_alpha - greatest(0, e.independent_evidence_count - c.ind))
  FROM c WHERE c.entry_id = e.entry_id;
UPDATE memory.entries e SET confidence_beta = e.confidence_beta + 1
 WHERE e.state = 'candidate' AND EXISTS (SELECT 1 FROM memory.provenance p WHERE p.entry_id = e.entry_id)
   AND NOT EXISTS (SELECT 1 FROM memory.provenance p WHERE p.entry_id = e.entry_id AND p.source_trust = 'human');
-- open PR-derived candidates whose job has no recorded outcome yet wait for the merge
UPDATE memory.entries e SET activation_hold = true, hold_reason = 'awaiting_merge', pr_outcome = 'awaiting_merge'
 WHERE e.state = 'candidate' AND NOT e.activation_hold
   AND EXISTS (SELECT 1 FROM memory.provenance p WHERE p.entry_id = e.entry_id AND p.job_id IS NOT NULL
                 AND (p.source_kind IN ('pr_review_comment', 'pr_review_body', 'pr_issue_comment', 'replicant_command',
                                        'repair_rejection') OR coalesce(p.cycle, 0) >= 1)
                 AND NOT EXISTS (SELECT 1 FROM memory.job_outcomes o WHERE o.job_id = p.job_id));
-- approved entries get a review date
UPDATE memory.entries SET review_due_at = state_changed_at + interval '90 days' WHERE state = 'active' AND review_due_at IS NULL;
SELECT set_config('replicant.principal_scope', '', true);
"""

DOWN = f"""
SELECT set_config('replicant.principal_scope', 'admin:memory', true);
ALTER TABLE memory.curator_actions DROP CONSTRAINT IF EXISTS curator_actions_action_check;
ALTER TABLE memory.curator_actions ADD CONSTRAINT curator_actions_action_check CHECK (action IN ({ACTIONS})) NOT VALID;
ALTER TABLE memory.job_outcomes DROP COLUMN IF EXISTS changed_paths;
ALTER TABLE memory.entry_flags DROP CONSTRAINT IF EXISTS entry_flags_reason_code_check;
ALTER TABLE memory.entry_flags ADD CONSTRAINT entry_flags_reason_code_check CHECK (reason_code IN ({FLAG_REASONS})) NOT VALID;
ALTER TABLE memory.entry_flags DROP CONSTRAINT IF EXISTS entry_flags_source_check;
ALTER TABLE memory.entry_flags ADD CONSTRAINT entry_flags_source_check CHECK (source IN ({FLAG_SOURCES})) NOT VALID;
ALTER TABLE memory.state_transitions DROP CONSTRAINT IF EXISTS st_outcome_rejects;
ALTER TABLE memory.state_transitions DROP CONSTRAINT IF EXISTS state_transitions_actor_kind_check;
ALTER TABLE memory.state_transitions ADD CONSTRAINT state_transitions_actor_kind_check CHECK (actor_kind IN ({ACTORS})) NOT VALID;
ALTER TABLE memory.entries DROP COLUMN IF EXISTS pr_outcome;
UPDATE memory.entries SET activation_hold = false, hold_reason = NULL WHERE hold_reason = 'awaiting_merge';
ALTER TABLE memory.entries DROP CONSTRAINT IF EXISTS entries_hold_reason_check;
ALTER TABLE memory.entries ADD CONSTRAINT entries_hold_reason_check CHECK (hold_reason IN ({HOLDS})) NOT VALID;
ALTER TABLE memory.feedback_items DROP CONSTRAINT IF EXISTS feedback_items_source_key_check;
ALTER TABLE memory.feedback_items ADD CONSTRAINT feedback_items_source_key_check
  CHECK (source_key ~ '^({PREFIXES}):[A-Za-z0-9_:./-]{{1,200}}$') NOT VALID;
ALTER TABLE memory.provenance DROP CONSTRAINT IF EXISTS provenance_source_kind_check;
ALTER TABLE memory.provenance ADD CONSTRAINT provenance_source_kind_check CHECK (source_kind IN ({KINDS})) NOT VALID;
UPDATE memory.provenance SET independence_key = source_key_digest WHERE source_key_digest IS NOT NULL;
DROP INDEX IF EXISTS memory.provenance_independence;
DROP INDEX IF EXISTS memory.provenance_one_per_source_key;
CREATE UNIQUE INDEX provenance_one_per_source ON memory.provenance (entry_id, independence_key) WHERE withdrawn_at IS NULL;
ALTER TABLE memory.provenance DROP COLUMN IF EXISTS source_trust;
ALTER TABLE memory.provenance DROP COLUMN IF EXISTS source_key_digest;
"""


def upgrade() -> None:
    owner_ddl.execute(UP)
    owner_ddl.execute(DATA)


def downgrade() -> None:
    owner_ddl.execute(DOWN)
