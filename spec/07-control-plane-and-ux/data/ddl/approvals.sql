-- Schema approvals. Writer: replicant-projector (svc_replicant_projector). DDL owner: WS-07.
-- DERIVED gate projection (authority is Temporal: C-STATE 1.8.0 §1.1). Inbox queries join identity at read time.
-- Free text (comments, answers) is never stored here: it lives in the approval_text artifact (C-ART 1.9.0, R2-004)
-- and is referenced by key and digest.
CREATE SCHEMA IF NOT EXISTS approvals;

CREATE TABLE approvals.gates (
    gate_id          text COLLATE "C" PRIMARY KEY CONSTRAINT ck_gates_gate_id CHECK (gate_id ~ '^gate_[0-9a-hjkmnp-tv-z]{26}$'),
    job_id           text COLLATE "C" NOT NULL,                  -- job_… or a leak_purge workflow on replicant-ops (C-MSG 1.5.0)
    tenant_id        text COLLATE "C" NOT NULL,
    seq              bigint NOT NULL,
    kind             text NOT NULL CONSTRAINT ck_gates_kind CHECK (kind IN ('plan_approval', 'publish_approval', 'clarification', 'budget_extension', 'bundle_migration', 'leak_purge')),
    state            text NOT NULL CONSTRAINT ck_gates_state CHECK (state IN ('open', 'approved', 'rejected', 'changes_requested', 'answered', 'expired', 'cancelled')),
    risk_level       text,
    title            text,
    repo             text,
    requester        text NOT NULL,
    release_bundle_id text,                                    -- for the "bundle rolled back" packet banner (R2-021)
    opened_at        timestamptz NOT NULL,
    expires_at       timestamptz,
    next_reminder_at timestamptz,
    escalation_level integer NOT NULL DEFAULT 0,
    packet_revision  integer NOT NULL,
    presented_digest text NOT NULL CONSTRAINT ck_gates_digest CHECK (presented_digest ~ '^sha256:[0-9a-f]{64}$'),
    packet_key       text NOT NULL,
    channels         text[] NOT NULL,
    min_repo_permission text,
    requester_may_decide boolean NOT NULL DEFAULT false,
    min_approvers    integer NOT NULL DEFAULT 1 CONSTRAINT ck_gates_min_approvers CHECK (min_approvers BETWEEN 1 AND 5),   -- C-MSG 1.5.0
    approvals_recorded integer NOT NULL DEFAULT 0,                -- partially approved state (outcome `recorded`)
    suggested_approver text,
    batchable        boolean NOT NULL DEFAULT false,
    winning_decision jsonb,
    decided_at       timestamptz,
    -- Memory entries cited by the current packet revision, as mem_…@v references only (C-ART; never text). Filled by
    -- gates.build_packet for each revision; read by audit-exporter's reconciler (ruling on XD-W2-08-05).
    memory_refs      text[] NOT NULL DEFAULT '{}' CONSTRAINT ck_gates_memory_refs CHECK (
                       cardinality(memory_refs) = 0
                       OR array_to_string(memory_refs, ',') ~ '^mem_[0-9a-hjkmnp-tv-z]{26}@[1-9][0-9]*(,mem_[0-9a-hjkmnp-tv-z]{26}@[1-9][0-9]*)*$'),
    -- C-MSG 2.0.0 GateView.qa_reruns_used (CCR-05-04): publish_approval only, max 2
    qa_reruns_used   integer NOT NULL DEFAULT 0 CONSTRAINT ck_gates_qa_reruns CHECK (qa_reruns_used BETWEEN 0 AND 2),
    -- C-MSG 2.0.0 GateView.release_block (CCR-06-13): {release_bundle_id, rolled_back_at, actor, reason}
    release_block    jsonb CONSTRAINT ck_gates_release_block CHECK (release_block IS NULL OR
                       (release_block ? 'release_bundle_id' AND release_block ? 'rolled_back_at' AND release_block ? 'actor' AND release_block ? 'reason'))
);
CREATE INDEX gates_open_by_expiry ON approvals.gates (tenant_id, expires_at) WHERE state = 'open';
CREATE INDEX gates_by_job ON approvals.gates (job_id);
CREATE INDEX gates_open_by_bundle ON approvals.gates (release_bundle_id) WHERE state = 'open';

-- Inbox index: one row per eligibility grant at gate open (or widening). Membership resolved at read time.
CREATE TABLE approvals.gate_eligibility (
    gate_id   text COLLATE "C" NOT NULL REFERENCES approvals.gates (gate_id) ON DELETE CASCADE,
    tenant_id text COLLATE "C" NOT NULL,
    kind      text NOT NULL CONSTRAINT ck_gate_eligibility_kind CHECK (kind IN ('principal', 'okta_group', 'codeowner_principal', 'escalation_group', 'delegate')),
    value     text NOT NULL,
    since     timestamptz NOT NULL,
    PRIMARY KEY (gate_id, kind, value)
);
CREATE INDEX gate_eligibility_value ON approvals.gate_eligibility (kind, value);

-- Every decision the workflow handled (accepted, superseded, denied, recorded), from GateView.decision_log
-- in the snapshot (CCR-07-07). The comment is referenced, never copied (approval_text artifact).
CREATE TABLE approvals.gate_decisions (
    decision_id  text COLLATE "C" PRIMARY KEY CONSTRAINT ck_gate_decisions_id CHECK (decision_id ~ '^dec_[0-9a-hjkmnp-tv-z]{26}$'),
    gate_id      text COLLATE "C" NOT NULL REFERENCES approvals.gates (gate_id) ON DELETE CASCADE,
    tenant_id    text COLLATE "C" NOT NULL,
    outcome      text NOT NULL CONSTRAINT ck_gate_decisions_outcome CHECK (outcome IN ('accepted', 'superseded', 'denied', 'recorded')),
    decision     text NOT NULL CONSTRAINT ck_gate_decisions_decision CHECK (decision IN ('approve', 'reject', 'request_changes', 'answer', 'rerun_qa')),
    principal_id text NOT NULL,
    channel      text NOT NULL CONSTRAINT ck_gate_decisions_channel CHECK (channel IN ('ui', 'cli', 'api', 'slack', 'github', 'gitea', 'system')),   -- C-MSG 2.0.0 decision_channel
    delegate_of  text,
    denial_reason text,
    text_key     text,                                          -- approval_text artifact key (comment/answer), if any
    -- sha256 of the approval_text object. The writer is replicant-worker's gates.write_approval_record (C-STATE 2.0.0,
    -- WS-01 gates.md), which reports it in GateView.decision_log[].text_sha256 (C-MSG 2.0.0); replicant-projector, the only
    -- writer of this table, copies it here. Set exactly when text exists. audit-exporter's reconciler reads it here and
    -- never decrypts tenant content (ruling on XD-W2-08-05; R1V-046).
    text_sha256  text CONSTRAINT ck_gate_decisions_text_sha256 CHECK (text_sha256 IS NULL OR text_sha256 ~ '^[0-9a-f]{64}$'),
    decided_at   timestamptz NOT NULL,
    presented_digest text NOT NULL,
    CONSTRAINT ck_gate_decisions_text_pair CHECK ((text_key IS NULL) = (text_sha256 IS NULL))
);
CREATE INDEX gate_decisions_by_principal ON approvals.gate_decisions (principal_id, decided_at DESC);
CREATE INDEX gate_decisions_by_gate ON approvals.gate_decisions (gate_id, decided_at);

CREATE TABLE approvals.gate_reminders (
    gate_id   text COLLATE "C" NOT NULL REFERENCES approvals.gates (gate_id) ON DELETE CASCADE,
    tenant_id text COLLATE "C" NOT NULL,
    kind      text NOT NULL CONSTRAINT ck_gate_reminders_kind CHECK (kind IN ('reminder', 'escalation', 'final_reminder')),
    ordinal   integer NOT NULL,
    fired_at  timestamptz NOT NULL,
    target    text,
    PRIMARY KEY (gate_id, kind, ordinal)
);

SELECT ops.apply_tenant_rls('approvals.gates');
SELECT ops.apply_tenant_rls('approvals.gate_eligibility');
SELECT ops.apply_tenant_rls('approvals.gate_decisions');
SELECT ops.apply_tenant_rls('approvals.gate_reminders');

-- Grant model v2 (XD-FIX-R1-160, C-PG 2.0.0): WS-10 owns roles, logins and schema USAGE; this file grants on
-- parent tables only (never ON ALL TABLES, never on a partition, no default privileges): reads to
-- r_<schema>_read, DML to r_<schema>_write, column-narrowed reads to a named login. Never to the login
-- svc_control_plane_api (its modules inherit r_<schema>_write; svc_cpa_read inherits the read roles).
GRANT SELECT, INSERT, UPDATE, DELETE ON approvals.gates, approvals.gate_eligibility, approvals.gate_decisions,
    approvals.gate_reminders TO r_approvals_write;
GRANT SELECT ON approvals.gates, approvals.gate_eligibility, approvals.gate_decisions, approvals.gate_reminders
    TO r_approvals_read, grafana_ro;
-- audit-exporter's approval-record reconciler (ruling on XD-W2-08-05, XD-W2-10-08): column-limited reads only;
-- digests and references, never text. WS-10 then drops approvals from the exporter's capability roles.
GRANT SELECT (gate_id, job_id, tenant_id, packet_key, packet_revision, presented_digest, memory_refs)
      ON approvals.gates TO svc_audit_exporter;
GRANT SELECT (decision_id, gate_id, tenant_id, outcome, decision, principal_id, channel, delegate_of, text_key,
              text_sha256, decided_at, presented_digest)
      ON approvals.gate_decisions TO svc_audit_exporter;
