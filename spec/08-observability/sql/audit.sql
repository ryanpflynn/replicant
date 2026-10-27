-- Schema "audit" in database "replicant" (C-PG: DDL owner WS-08, reviewed by WS-09; writers only via audit.emit()).
-- Reviewed target DDL; the replicant-db SQLAlchemy models and the Alembic migration must produce exactly this (D-26).
-- Target PostgreSQL 17 (replicant instance). Uses only core functions (no pgcrypto): gen_random_uuid(), sha256().
-- Executed end to end against PostgreSQL 16.2 (embedded test server) by sql/test_audit.py on 2026-10-24.
--
-- Roles are created by WS-10's role migration: replicant_migrator (owns objects), audit_owner (owns emit()),
-- audit_reader (auditors, control-plane-api audit views), grafana_ro, svc_audit_exporter (login, member of
-- audit_reader; the audit exporter, CCR-08-06), and one svc_<service> login per writer.

CREATE SCHEMA IF NOT EXISTS audit AUTHORIZATION audit_owner;
REVOKE ALL ON SCHEMA audit FROM PUBLIC;

-- ---------------------------------------------------------------------------------------------------------------
-- Identifier helper: aud_<ulid> per C-IDS section 1 (48-bit Unix ms + 80 random bits, lowercase Crockford base32).
-- The 80 random bits come from the fully random bytes of a v4 UUID (bytes 0-5 and 10-13; bytes 6 and 8 carry
-- version and variant bits and are skipped).
-- ---------------------------------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.ulid_body(p_ts timestamptz DEFAULT clock_timestamp())
RETURNS text
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $$
DECLARE
  alphabet constant text := '0123456789abcdefghjkmnpqrstvwxyz';
  ms       bigint := floor(extract(epoch FROM p_ts) * 1000)::bigint;
  u        bytea  := uuid_send(gen_random_uuid());
  bits     bit varying;
  out      text := '';
  i        int;
BEGIN
  bits := B'00'
       || ms::bit(48)
       || (get_byte(u, 0)::bit(8)  || get_byte(u, 1)::bit(8)  || get_byte(u, 2)::bit(8)
        || get_byte(u, 3)::bit(8)  || get_byte(u, 4)::bit(8)  || get_byte(u, 5)::bit(8)
        || get_byte(u, 10)::bit(8) || get_byte(u, 11)::bit(8) || get_byte(u, 12)::bit(8)
        || get_byte(u, 13)::bit(8));
  FOR i IN 0..25 LOOP
    out := out || substr(alphabet, substring(bits FROM i * 5 + 1 FOR 5)::bit(5)::int + 1, 1);
  END LOOP;
  RETURN out;
END
$$;

-- ---------------------------------------------------------------------------------------------------------------
-- Event type catalog (reference data ships as migrations, D-26). New types need a migration reviewed by WS-09.
-- ---------------------------------------------------------------------------------------------------------------
CREATE TABLE audit.event_types (
  event_type      text PRIMARY KEY CHECK (event_type ~ '^[a-z][a-z_]*(\.[a-z_]+)+$' AND length(event_type) <= 64),
  category        text NOT NULL CHECK (category IN ('intake', 'gate', 'job_control', 'release', 'registry',
                     'memory', 'identity', 'credential', 'artifact_access', 'github', 'admin', 'security',
                     'observability', 'projection')),
  retention_years smallint NOT NULL DEFAULT 7 CHECK (retention_years BETWEEN 1 AND 10),
  description     text NOT NULL
);

INSERT INTO audit.event_types (event_type, category, description) VALUES
  ('job.requested',                 'intake',          'JobRequest accepted by control-plane-api (job_id assigned)'),
  ('job.request_denied',            'intake',          'JobRequest rejected by authorization (unlinked identity, no repo permission, group)'),
  ('job.started',                   'intake',          'ReplicantJob workflow started, pinned to release_bundle_id'),
  ('job.terminal',                  'job_control',     'Job reached a terminal status'),
  ('job.cancel_requested',          'job_control',     'job.cancel Update accepted'),
  ('job.paused',                    'job_control',     'job.pause Update accepted'),
  ('job.resumed',                   'job_control',     'job.resume Update accepted'),
  ('job.steered',                   'job_control',     'job.steer Update accepted (message_id; text is never in audit)'),
  ('job.taken_over',                'job_control',     'job.takeover completed; takeover package issued'),
  ('job.action_denied',             'job_control',     'identity.check_action denied a job control action'),
  ('gate.opened',                   'gate',            'Gate opened with presented_digest and eligible principals'),
  ('gate.reminded',                 'gate',            'Reminder sent per gate_rules'),
  ('gate.escalated',                'gate',            'Escalation sent per gate_rules'),
  ('gate.decided',                  'gate',            'First valid decision recorded (decision_id, channel, presented_digest)'),
  ('gate.decision_denied',          'gate',            'identity.check_decision denied the decider'),
  ('gate.decision_superseded',      'gate',            'A later decision lost the first-wins race'),
  ('gate.decision_recorded',        'gate',            'An approve on a min_approvers > 1 gate was recorded; the gate stays open (C-MSG 1.5.0 V11)'),
  ('gate.decision_identity_contested', 'gate',        'control-plane-api: an accepted approval whose approver had lost eligibility before the decision; details gate_id, decision_id, principal_id, change, effective_at, action (notify or reopen_if_unpublished) (XD-W3-R1V-041-042)'),
  ('gate.decision_contested',       'gate',            'A decision arrived after the gate closed and was surfaced to the decider (WS-07)'),
  ('gate.expired',                  'gate',            'Gate expired per policy'),
  ('gate.auto_resolved',            'gate',            'Gate resolved automatically in evaluation mode (C-MSG 1.2.0 section 2.1)'),
  ('pr.published',                  'github',          'Publisher pushed a branch and opened or updated a PR'),
  ('pr.repair_triggered',           'github',          'Authorized review feedback started a repair cycle'),
  ('pr.repair_trigger_ignored',     'github',          'Feedback from a bot, Replicant, or an unauthorized actor was ignored'),
  ('github.token_minted',           'credential',      'github-token-broker issued an installation token (caller, repo, permissions)'),
  ('run.token_issued',              'credential',      'Runner issued a run token (jti, audiences)'),
  ('run.credentials_vended',        'credential',      'Runner vended prefix-scoped AWS credentials (AccessKeyId only)'),
  ('artifact.presigned',            'artifact_access', 'control-plane-api issued a presigned GET (bucket_class, key hash, principal)'),
  ('artifact.takeover_downloaded',  'artifact_access', 'Workspace archive presigned to the requester'),
  ('release.bundle_promoted',       'release',         'Bundle promotion state changed'),
  ('release.assignment_changed',    'release',         'Default, override or ramp assignment changed'),
  ('release.bundle_revoked',        'release',         'Bundle revoked; release.notice sent'),
  ('release.migration_offered',     'release',         'job.offer_bundle_migration sent'),
  ('registry.published',            'registry',        'Definition versions published from a config commit'),
  -- memory types: exactly the names WS-04 emits (04-memory-and-context/data-model.md section 8; R1-095)
  ('memory.entry_transition'         , 'memory',          'Memory entry lifecycle transition, by an admin or by the system under admin policy (D-14, D-25)'),
  ('memory.entry_edited'             , 'memory',          'Admin edited a memory entry (new version)'),
  ('memory.entry_promoted'           , 'memory',          'Admin promoted a memory entry (scope widened)'),
  ('memory.entry_merged'             , 'memory',          'Admin merged entries into a survivor'),
  ('memory.conflict_resolved'        , 'memory',          'Admin resolved a conflict between entries'),
  ('memory.quarantine_created'       , 'memory',          'Admin or automation quarantined entries'),
  ('memory.quarantine_released'      , 'memory',          'Quarantine released'),
  ('memory.purge_requested'          , 'memory',          'Hard delete (purge) requested; details.path = admin or leak'),
  ('memory.purge_completed'          , 'memory',          'Purge completed with deletion ledger IDs'),
  ('memory.export_created'           , 'memory',          'Admin memory export created'),
  ('memory.import_applied'           , 'memory',          'Admin memory import applied'),
  ('memory.governance_changed'       , 'memory',          'Memory governance settings changed'),
  ('memory.author_restricted'        , 'memory',          'Admin restricted an author from contributing memory'),
  ('memory.flag_created'             , 'memory',          'A user flagged an entry'),
  ('memory.correction_requested'     , 'memory',          'A user requested a correction of an entry'),
  ('memory.hold_cleared'             , 'memory',          'Admin cleared a hold on an entry'),
  ('memory.outcome_report_applied'   , 'memory',          'Outcome report (wrong fix, regression, revert) applied to entries'),
  ('memory.admin_action'             , 'memory',          'Generic admin action (seeded; unused by memory-service, kept for compatibility)'),
  ('memory.admin_denied'             , 'memory',          'memory-service second-layer re-check denied memory management (D-25); details.permission = memory:admin, details.route (XD-W2-04-02)'),
  ('identity.linked',               'identity',        'Okta <-> GitHub/Slack/Jira identity link created'),
  ('identity.unlinked',             'identity',        'Identity link removed'),
  ('identity.scim.user_provisioned','identity',        'SCIM user created or reactivated (WS-07)'),
  ('identity.scim.user_updated',    'identity',        'SCIM user attributes changed'),
  ('identity.scim.user_deprovisioned','identity',      'SCIM user deactivated or deleted'),
  ('identity.scim.group_membership_changed','identity','SCIM group membership added or removed'),
  ('authz.denied',                  'security',        'control-plane-api first-layer policy check denied an action; details.permission (memory:admin for /v1/admin/memory/**) and details.route (XD-W2-04-02)'),
  ('admin.role_binding_changed',    'admin',           'Group to permission binding changed'),
  ('admin.tenant_settings_changed', 'admin',           'Tenant operational settings changed'),
  ('admin.projection_rebuild',      'admin',           'Read-model rebuild executed'),
  ('breakglass.session_started',    'security',        'Break-glass database session approved and started (D-26)'),
  ('breakglass.session_ended',      'security',        'Break-glass session ended'),
  ('security.leak_purge_started',   'security',        'LeakPurge workflow started (leak_id, fingerprint, scope)'),
  ('security.leak_purge_approved',  'security',        'A replicant-admins member approved a LeakPurge (gate kind leak_purge, min_approvers 2; one event per approval)'),
  ('security.leak_purge_completed', 'security',        'LeakPurge finished with per-sink results'),
  ('security.leak_purge_blocked',   'security',        'LeakPurge found a legal-bucket object under legal hold (blocked_by_legal_hold; C-ART 2.0.0 rule 4a)'),
  ('temporal.payload_decoded',      'security',        'temporal-codec-server decoded payloads for an operator (XD-FIX-R1-287)'),
  ('approval_record.reconciled',    'gate',            'audit-exporter wrote a missing approval record from the full history export (XD-FIX-R2-013)'),
  ('projection.restore_repair',     'projection',      'replicant-projector reconciler repaired a job during an open restore, bypassing the seq guard; details job_id, restored_seq, snapshot_seq, restore_seq (XD-W3-R1V-041-042)'),
  ('data.cross_tenant_read',        'admin',           'Audited cross-tenant read through an ops.eval_* function (CCR-06-15): function, window, row count (XD-W2-10-01)'),
  ('observability.langfuse_expired','observability',   'Langfuse retention sweeper deleted traces older than 90 days'),
  ('observability.grafana_login',   'observability',   'Operator login to Grafana or Langfuse (from Okta System Log import)');

-- ---------------------------------------------------------------------------------------------------------------
-- Events: append-only, partitioned by month on occurred_at. No free text: every column is an ID, enum, digest,
-- or a bounded structured value. Human-written text (comments, steering, PR bodies) lives in Temporal history
-- and S3 and is referenced by ID or digest (audit.md section 3).
-- ---------------------------------------------------------------------------------------------------------------
CREATE TABLE audit.events (
  audit_event_id     text        NOT NULL CHECK (audit_event_id ~ '^aud_[0-9a-hjkmnp-tv-z]{26}$'),
  occurred_at        timestamptz NOT NULL,
  recorded_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
  event_type         text        NOT NULL REFERENCES audit.event_types (event_type),
  outcome            text        NOT NULL CHECK (outcome IN ('success', 'denied', 'failure', 'superseded', 'noop')),
  reason             text        CHECK (reason ~ '^[A-Za-z][A-Za-z0-9_:.-]{0,127}$'),
  tenant_id          text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  actor_principal_id text        NOT NULL CHECK (actor_principal_id ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
  on_behalf_of       text        CHECK (on_behalf_of ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
  channel            text        NOT NULL CHECK (channel IN ('ui', 'slack', 'github', 'cli', 'api', 'system', 'okta')),
  channel_ref        jsonb       CHECK (channel_ref IS NULL OR (jsonb_typeof(channel_ref) = 'object' AND pg_column_size(channel_ref) <= 1024)),
  source_service     text        NOT NULL CHECK (source_service ~ '^[a-z][a-z0-9-]{1,62}$'),
  job_id             text        CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
  run_id             text        CHECK (run_id ~ '^run_[0-9a-hjkmnp-tv-z]{26}$'),
  gate_id            text        CHECK (gate_id ~ '^gate_[0-9a-hjkmnp-tv-z]{26}$'),
  decision_id        text        CHECK (decision_id ~ '^dec_[0-9a-hjkmnp-tv-z]{26}$'),
  release_bundle_id  text        CHECK (release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
  target_kind        text        CHECK (target_kind ~ '^[a-z][a-z0-9_]{1,40}$'),
  target_id          text        CHECK (length(target_id) <= 256),
  digest             text        CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
  evidence_uri       text        CHECK (evidence_uri ~ '^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/.+' AND length(evidence_uri) <= 1100),
  trace_id           text        CHECK (trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)),
  details            jsonb       NOT NULL DEFAULT '{}'::jsonb
                                 CHECK (jsonb_typeof(details) = 'object' AND pg_column_size(details) <= 4096),
  -- Optional caller idempotency key, e.g. 'proj:<job_id>:<seq>' for projector gate items (XD-FIX-R1-157).
  -- A replay must pass the same occurred_at (the source event's time), so the unique index below catches it.
  idempotency_key    text        CHECK (idempotency_key ~ '^[a-z][a-z0-9_-]{0,15}:[A-Za-z0-9_:.@-]{1,200}$'),
  PRIMARY KEY (occurred_at, audit_event_id)
) PARTITION BY RANGE (occurred_at);

COMMENT ON TABLE audit.events IS
  'Append-only audit trail (C-PG rule 3). Written only through audit.emit(). Retention by partition drop (7 years).';

CREATE INDEX events_job_idx      ON audit.events (job_id, occurred_at)            WHERE job_id IS NOT NULL;
CREATE INDEX events_gate_idx     ON audit.events (gate_id, occurred_at)           WHERE gate_id IS NOT NULL;
CREATE INDEX events_decision_idx ON audit.events (decision_id)                    WHERE decision_id IS NOT NULL;
CREATE INDEX events_actor_idx    ON audit.events (actor_principal_id, occurred_at);
CREATE INDEX events_tenant_type_idx ON audit.events (tenant_id, event_type, occurred_at);
CREATE UNIQUE INDEX events_id_idx ON audit.events (audit_event_id, occurred_at);
CREATE UNIQUE INDEX events_idem_idx ON audit.events (idempotency_key, occurred_at) WHERE idempotency_key IS NOT NULL;

-- Default partition catches clock skew; a monitor alerts if it is ever non-empty.
CREATE TABLE audit.events_default PARTITION OF audit.events DEFAULT;
-- Partition-level isolation (XD-ORCH-04): RLS ENABLE + FORCE and no direct privileges on the partition itself.
SELECT ops.secure_partition('audit.events_default'::regclass);

-- Partition management, called by the migrate Job in scheduled maintenance mode (C-PG rule 3).
-- WS-10 maintenance normally creates audit partitions from ops.partitioned_tables (owner audit_owner); this function
-- is the equivalent for environments without that job. SECURITY DEFINER so partitions are owned by audit_owner, and
-- every new partition is secured at once (XD-ORCH-04 / XD-FIX-ORCH-04 item 2).
CREATE OR REPLACE FUNCTION audit.ensure_partitions(p_months_ahead int DEFAULT 3)
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, audit
AS $$
DECLARE
  m       date := date_trunc('month', now())::date;
  created int  := 0;
  name    text;
BEGIN
  FOR i IN 0..p_months_ahead LOOP
    name := format('events_%s', to_char(m + make_interval(months => i), 'YYYY_MM'));
    IF to_regclass('audit.' || name) IS NULL THEN
      EXECUTE format('CREATE TABLE audit.%I PARTITION OF audit.events FOR VALUES FROM (%L) TO (%L)',
                     name, m + make_interval(months => i), m + make_interval(months => i + 1));
      PERFORM ops.secure_partition(format('audit.%I', name)::regclass);
      created := created + 1;
    END IF;
  END LOOP;
  RETURN created;
END
$$;

-- Retention: drop monthly partitions older than the longest retention of any event type they could hold
-- (7 years). Run only after audit-exporter has confirmed the partition's S3 export (Object Lock) exists.
CREATE OR REPLACE FUNCTION audit.drop_expired_partitions(p_keep_years int DEFAULT 7)
RETURNS SETOF text
LANGUAGE plpgsql
SECURITY DEFINER        -- runs as the table owner so the row-count check is not filtered by RLS
SET search_path = pg_catalog, audit
AS $$
DECLARE
  r      record;
  cutoff date := (date_trunc('month', now()) - make_interval(years => p_keep_years))::date;
BEGIN
  FOR r IN
    SELECT c.relname
    FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhrelid
    JOIN pg_class p ON p.oid = i.inhparent
    JOIN pg_namespace n ON n.oid = p.relnamespace
    WHERE n.nspname = 'audit' AND p.relname = 'events' AND c.relname ~ '^events_[0-9]{4}_[0-9]{2}$'
      AND to_date(substr(c.relname, 8), 'YYYY_MM') < cutoff
      -- export-before-drop (R1-194): never drop a month whose Object-Locked export is not recorded with a
      -- matching row count
      AND EXISTS (SELECT 1 FROM audit.partition_exports pe
                  WHERE pe.partition_name = c.relname
                    AND pe.row_count = (SELECT count(*) FROM audit.events e
                                        WHERE e.occurred_at >= to_date(substr(c.relname, 8), 'YYYY_MM')
                                          AND e.occurred_at < to_date(substr(c.relname, 8), 'YYYY_MM') + interval '1 month'))
  LOOP
    EXECUTE format('DROP TABLE audit.%I', r.relname);
    RETURN NEXT r.relname;
  END LOOP;
END
$$;

-- ---------------------------------------------------------------------------------------------------------------
-- audit.emit(): the only write path. SECURITY DEFINER, fixed search_path, strict key whitelist.
-- Called inside the caller's business transaction so the audit row commits atomically with the change.
-- ---------------------------------------------------------------------------------------------------------------
-- Shared validation for audit.emit and audit.emit_restored: strict key whitelist and flat, bounded details.
CREATE OR REPLACE FUNCTION audit.validate_event(p_event jsonb, p_allowed text[])
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, audit
AS $$
DECLARE
  extra text[];
BEGIN
  IF p_event IS NULL OR jsonb_typeof(p_event) <> 'object' THEN
    RAISE EXCEPTION 'audit.emit: event must be a JSON object' USING ERRCODE = '22023';
  END IF;
  SELECT array_agg(k) INTO extra FROM jsonb_object_keys(p_event) AS k WHERE NOT (k = ANY (p_allowed));
  IF extra IS NOT NULL THEN
    RAISE EXCEPTION 'audit.emit: unknown keys %', extra USING ERRCODE = '22023';
  END IF;
  -- details may only hold scalars or arrays of scalars, keys snake_case, string values <= 256 chars.
  IF EXISTS (
    SELECT 1 FROM jsonb_each(coalesce(p_event -> 'details', '{}'::jsonb)) AS d(k, v)
    WHERE k !~ '^[a-z][a-z0-9_]{0,40}$'
       OR jsonb_typeof(v) = 'object'
       OR (jsonb_typeof(v) = 'string' AND length(v #>> '{}') > 256)
       OR (jsonb_typeof(v) = 'array' AND EXISTS (
             SELECT 1 FROM jsonb_array_elements(v) e
             WHERE jsonb_typeof(e) IN ('object', 'array') OR (jsonb_typeof(e) = 'string' AND length(e #>> '{}') > 256)))
  ) THEN
    RAISE EXCEPTION 'audit.emit: details must be flat, snake_case, bounded values' USING ERRCODE = '22023';
  END IF;
  IF (p_event ->> 'occurred_at')::timestamptz > clock_timestamp() + interval '5 minutes' THEN
    RAISE EXCEPTION 'audit.emit: occurred_at is in the future' USING ERRCODE = '22023';
  END IF;
END
$$;

-- Inserts one validated row; returns the stored audit_event_id (the existing one on an idempotent replay).
CREATE OR REPLACE FUNCTION audit.insert_event(p_event jsonb, p_id text, p_at timestamptz)
RETURNS text
LANGUAGE plpgsql
SET search_path = pg_catalog, audit
AS $$
DECLARE
  v_id text;
BEGIN
  INSERT INTO audit.events (audit_event_id, occurred_at, event_type, outcome, reason, tenant_id,
    actor_principal_id, on_behalf_of, channel, channel_ref, source_service, job_id, run_id, gate_id,
    decision_id, release_bundle_id, target_kind, target_id, digest, evidence_uri, trace_id, details,
    idempotency_key)
  VALUES (p_id, p_at, p_event ->> 'event_type', p_event ->> 'outcome', p_event ->> 'reason',
    p_event ->> 'tenant_id', p_event ->> 'actor_principal_id', p_event ->> 'on_behalf_of',
    p_event ->> 'channel', p_event -> 'channel_ref', p_event ->> 'source_service', p_event ->> 'job_id',
    p_event ->> 'run_id', p_event ->> 'gate_id', p_event ->> 'decision_id', p_event ->> 'release_bundle_id',
    p_event ->> 'target_kind', p_event ->> 'target_id', p_event ->> 'digest', p_event ->> 'evidence_uri',
    coalesce(p_event ->> 'trace_id',
             CASE WHEN p_event ? 'job_id'
                  THEN substr(encode(sha256(convert_to('replicant.job.trace.v1:' || (p_event ->> 'job_id'), 'UTF8')), 'hex'), 1, 32)
             END),
    coalesce(p_event -> 'details', '{}'::jsonb), p_event ->> 'idempotency_key')
  ON CONFLICT DO NOTHING
  RETURNING audit_event_id INTO v_id;
  IF v_id IS NULL THEN   -- replay: same idempotency key (or same audit_event_id) at the same occurred_at
    SELECT e.audit_event_id INTO v_id FROM audit.events e
    WHERE e.occurred_at = p_at
      AND (e.audit_event_id = p_id OR e.idempotency_key = p_event ->> 'idempotency_key');
  END IF;
  RETURN v_id;
END
$$;

-- ---------------------------------------------------------------------------------------------------------------
-- audit.emit(): the only write path. SECURITY DEFINER, fixed search_path, strict key whitelist.
-- Called inside the caller's business transaction so the audit row commits atomically with the change.
-- With idempotency_key (requires occurred_at), a replay returns the original audit_event_id and adds no row.
-- ---------------------------------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.emit(p_event jsonb)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, audit
AS $$
DECLARE
  v_at timestamptz;
BEGIN
  PERFORM audit.validate_event(p_event, ARRAY['occurred_at', 'event_type', 'outcome', 'reason', 'tenant_id',
    'actor_principal_id', 'on_behalf_of', 'channel', 'channel_ref', 'source_service', 'job_id', 'run_id',
    'gate_id', 'decision_id', 'release_bundle_id', 'target_kind', 'target_id', 'digest', 'evidence_uri',
    'trace_id', 'details', 'idempotency_key']);
  IF p_event ? 'idempotency_key' AND NOT p_event ? 'occurred_at' THEN
    RAISE EXCEPTION 'audit.emit: idempotency_key requires occurred_at' USING ERRCODE = '22023';
  END IF;
  v_at := coalesce((p_event ->> 'occurred_at')::timestamptz, clock_timestamp());
  RETURN audit.insert_event(p_event, 'aud_' || audit.ulid_body(v_at), v_at);
END
$$;

-- ---------------------------------------------------------------------------------------------------------------
-- audit.emit_restored(): restore form (CCR-10-09, XD-FIX-R1-237). Re-inserts an event from audit_event_export
-- with its original audit_event_id and occurred_at, only while a restore is open (ops.restore_events with
-- reconcile_state pending or running). Validation is identical to emit(); duplicates are ignored.
-- ---------------------------------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.emit_restored(p_event jsonb)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, audit
AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM ops.restore_events WHERE reconcile_state IN ('pending', 'running')) THEN
    RAISE EXCEPTION 'audit.emit_restored: no restore is open' USING ERRCODE = '55000';
  END IF;
  PERFORM audit.validate_event(p_event, ARRAY['audit_event_id', 'occurred_at', 'recorded_at', 'event_type',
    'outcome', 'reason', 'tenant_id', 'actor_principal_id', 'on_behalf_of', 'channel', 'channel_ref',
    'source_service', 'job_id', 'run_id', 'gate_id', 'decision_id', 'release_bundle_id', 'target_kind',
    'target_id', 'digest', 'evidence_uri', 'trace_id', 'details', 'idempotency_key']);
  IF NOT (p_event ? 'audit_event_id' AND p_event ? 'occurred_at') THEN
    RAISE EXCEPTION 'audit.emit_restored: audit_event_id and occurred_at are required' USING ERRCODE = '22023';
  END IF;
  PERFORM audit.insert_event(p_event, p_event ->> 'audit_event_id', (p_event ->> 'occurred_at')::timestamptz);
END
$$;

ALTER FUNCTION audit.emit(jsonb) OWNER TO audit_owner;
ALTER FUNCTION audit.emit_restored(jsonb) OWNER TO audit_owner;
ALTER FUNCTION audit.validate_event(jsonb, text[]) OWNER TO audit_owner;
ALTER FUNCTION audit.insert_event(jsonb, text, timestamptz) OWNER TO audit_owner;
REVOKE ALL ON FUNCTION audit.emit(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION audit.emit_restored(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION audit.validate_event(jsonb, text[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION audit.insert_event(jsonb, text, timestamptz) FROM PUBLIC;
-- Writers: every service login that audits is a member of the r_audit_emit marker (WS-10 role matrix, C-PG rule 3).
GRANT EXECUTE ON FUNCTION audit.emit(jsonb) TO r_audit_emit;
-- Restore form: only the r_audit_restore marker (held by replicant_migrator alone, WS-10) may call it.
GRANT EXECUTE ON FUNCTION audit.emit_restored(jsonb) TO r_audit_restore;
REVOKE ALL ON FUNCTION audit.ensure_partitions(int) FROM PUBLIC;
REVOKE ALL ON FUNCTION audit.drop_expired_partitions(int) FROM PUBLIC;
ALTER FUNCTION audit.drop_expired_partitions(int) OWNER TO audit_owner;
ALTER FUNCTION audit.ensure_partitions(int) OWNER TO audit_owner;
-- Only the migrate Job's maintenance mode calls these (C-PG rule 3).
GRANT EXECUTE ON FUNCTION audit.drop_expired_partitions(int), audit.ensure_partitions(int) TO replicant_migrator;

-- ---------------------------------------------------------------------------------------------------------------
-- Grants. No role holds INSERT, UPDATE or DELETE on audit tables (C-PG rule 3).
-- ---------------------------------------------------------------------------------------------------------------
ALTER TABLE audit.events OWNER TO audit_owner;
ALTER TABLE audit.event_types OWNER TO audit_owner;
REVOKE ALL ON ALL TABLES IN SCHEMA audit FROM PUBLIC;
GRANT USAGE ON SCHEMA audit TO audit_reader, grafana_ro;
GRANT SELECT ON audit.events, audit.event_types TO audit_reader;

-- Row-level security (XD-10-07, canonical scope vocabulary only). replicant.principal_scope holds a canonical value,
-- read through ops.principal_scope() (WS-10 schema-management.md section 4). Auditors with full access run under
-- 'operator:all-tenants'. Auditors limited to some tenants run under 'tenant' with the comma-separated tenant list in
-- the session setting replicant.audit_tenants (transaction-local, set by control-plane-api's audit views like the
-- other replicant.* settings). An empty or missing list shows nothing.
ALTER TABLE audit.events ENABLE ROW LEVEL SECURITY;
CREATE POLICY events_reader_scope ON audit.events FOR SELECT TO audit_reader
  USING (ops.principal_scope() = 'operator:all-tenants'
         OR (ops.principal_scope() = 'tenant'
             AND tenant_id = ANY (string_to_array(nullif(current_setting('replicant.audit_tenants', true), ''), ','))));
-- audit-exporter reads every tenant's rows under the canonical system scope only (orchestrator ruling on XD-W2-10-03;
-- WS-10 sets svc_audit_exporter's role default replicant.principal_scope = 'system:all-tenants').
CREATE POLICY events_exporter_all ON audit.events FOR SELECT TO svc_audit_exporter
  USING (ops.principal_scope() = 'system:all-tenants');

-- Aggregate-only view for Grafana (grafana_ro never reads rows, C-PG section 2).
CREATE VIEW audit.v_daily_counts WITH (security_barrier = true) AS
  SELECT date_trunc('day', occurred_at) AS day, tenant_id, event_type, outcome, count(*) AS events
  FROM audit.events
  GROUP BY 1, 2, 3, 4;
ALTER VIEW audit.v_daily_counts OWNER TO audit_owner;
GRANT SELECT ON audit.v_daily_counts TO grafana_ro;

-- Export bookkeeping: audit-exporter records each monthly partition export (NDJSON, zstd) and its SHA-256 so a
-- partition can be dropped only after its Object-Locked copy exists, and so tampering is detectable.
CREATE TABLE audit.partition_exports (
  partition_name text        PRIMARY KEY CHECK (partition_name ~ '^events_[0-9]{4}_[0-9]{2}$'),
  exported_at    timestamptz NOT NULL,
  row_count      bigint      NOT NULL CHECK (row_count >= 0),
  sha256         text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  object_key     text        NOT NULL CHECK (length(object_key) <= 1024),
  manifest_root  text        NOT NULL CHECK (manifest_root ~ '^[0-9a-f]{64}$')
);
ALTER TABLE audit.partition_exports OWNER TO audit_owner;
GRANT SELECT ON audit.partition_exports TO audit_reader;

CREATE OR REPLACE FUNCTION audit.record_partition_export(p_partition text, p_rows bigint, p_sha256 text,
                                                         p_key text, p_root text)
RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, audit
AS $$
  INSERT INTO audit.partition_exports VALUES (p_partition, clock_timestamp(), p_rows, p_sha256, p_key, p_root)
  ON CONFLICT (partition_name) DO NOTHING;
$$;
ALTER FUNCTION audit.record_partition_export(text, bigint, text, text, text) OWNER TO audit_owner;
REVOKE ALL ON FUNCTION audit.record_partition_export(text, bigint, text, text, text) FROM PUBLIC;
-- The audit exporter's login (C-PG CCR-08-06; WS-10 creates the role and makes it a member of audit_reader) is the
-- only caller. This function writes only audit.partition_exports (bookkeeping), never audit.events.
GRANT EXECUTE ON FUNCTION audit.record_partition_export(text, bigint, text, text, text) TO svc_audit_exporter;
