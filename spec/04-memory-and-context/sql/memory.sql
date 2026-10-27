-- =====================================================================================
-- Schema `memory` — reviewed target DDL (WS-04). Contract C-PG 2.0.0: single writer
-- `memory-service` (role svc_memory_service). Objects owned by replicant_migrator.
--
-- The SQLAlchemy 2.0 models in ../models/replicant_db/models/memory.py and the Alembic
-- migrations MUST produce exactly this DDL (D-26). CI compares the migrated schema with
-- this file (WS-10 drift check).
--
-- Target: PostgreSQL 17.x / 18.x with pgvector >= 0.8 (RDS ships 0.8.2; upstream 0.8.7)
--   [verified: https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md, 2026-10-24]
-- Validated: applied cleanly to PostgreSQL 18 + pgvector 0.8.1 (pgvector/pgvector:0.8.1-pg18)
--   on top of WS-10's ops RLS helpers and cross-tenant roles, and exercised by
--   ../sql/test_memory.sql on 2026-10-24 (revision 2: WS-09 G-04/G-19 applied).
-- Revision 3 (wave-1 fixes, 2026-10-24): R1-095..112, R2-008/017/018/028/051/052; see
--   ../README.md "Fix round" for the mapping.
-- Depends on (WS-10): schema ops with ops.current_tenant(), ops.principal_scope(); roles
--   replicant_migrator, svc_memory_service, grafana_ro, r_cross_tenant_read, r_cross_tenant_write,
--   memory_analytics_owner (requested, XD-04-12).
--
-- Conventions
--   * IDs are text with CHECK regexes (C-IDS §1). No uuid columns.
--   * Closed vocabularies are text + CHECK (not PG enums) so migrations can extend them
--     with an expand/contract CHECK swap instead of ALTER TYPE.
--   * Every table holding tenant data has tenant_id NOT NULL and RLS (C-PG rule 4).
--     Org-scoped memory is stored with tenant_id = 'replicant-system' (reserved, C-IDS §2).
--   * Generated columns are declared STORED explicitly: PostgreSQL 18 defaults to VIRTUAL
--     and virtual generated columns cannot be indexed
--     [verified: https://www.postgresql.org/docs/18/release-18.html, 2026-10-24].
-- =====================================================================================

-- Extensions are created once by WS-10's baseline migration (schema public):
--   CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector
--   CREATE EXTENSION IF NOT EXISTS pg_trgm;
--   CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS memory AUTHORIZATION replicant_migrator;
SET search_path = memory, public;

-- -------------------------------------------------------------------------------------
-- RLS helpers. Session-variable protocol and vocabulary are WS-10's (schema-management.md §4.2,
-- ddl/20-ops.sql): ops.current_tenant() and ops.principal_scope() read replicant.tenant_id and
-- replicant.principal_scope (set_config(..., true) at transaction start by replicant-db).
-- Values honored by memory policies:
--   'tenant' (default)      own tenant rows + org rows (tenant_id 'replicant-system') readable; own rows writable
--   'admin:memory'          set by memory-service ONLY after identity-service confirmed D-25 admin
--                           membership for THIS request (no cached grant); cross-tenant curation
--   'operator:all-tenants'  honored only for members of r_cross_tenant_read (analytics view owner)
--   'system:all-tenants'    honored only for members of r_cross_tenant_write (migrate Job; and WS-10's audited
--                           read-only definer role eval_metrics_reader, which holds column-level SELECT only,
--                           via the security_invoker view memory.v_job_outcomes, XD-W2-10-07)
-- memory-service background jobs never use a cross-tenant scope: they iterate tenants and run each
-- tenant (and 'replicant-system' for org rows) as an ordinary 'tenant'-scope transaction (G-04).
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION memory.rls_can_read(p_tenant text) RETURNS boolean
  LANGUAGE sql STABLE PARALLEL SAFE
  AS $$ SELECT p_tenant = ops.current_tenant()
            OR p_tenant = 'replicant-system'
            OR ops.principal_scope() = 'admin:memory' $$;

CREATE OR REPLACE FUNCTION memory.rls_can_write(p_tenant text) RETURNS boolean
  LANGUAGE sql STABLE PARALLEL SAFE
  AS $$ SELECT p_tenant = ops.current_tenant()
            OR ops.principal_scope() = 'admin:memory' $$;

-- -------------------------------------------------------------------------------------
-- Embedding spaces (one per embedding model + dimension). Not tenant data.
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.embedding_spaces (
  space_id          text        PRIMARY KEY CHECK (space_id ~ '^[a-z][a-z0-9-]{2,40}$'),
  gateway_model_id  text        NOT NULL CHECK (length(gateway_model_id) <= 200 AND gateway_model_id !~ 'latest$'),
  dimensions        integer     NOT NULL CHECK (dimensions BETWEEN 64 AND 4000),
  distance          text        NOT NULL DEFAULT 'cosine' CHECK (distance IN ('cosine')),
  input_type        text        NOT NULL DEFAULT 'document' CHECK (input_type IN ('document', 'none')),
  state             text        NOT NULL CHECK (state IN ('building', 'active', 'retiring', 'retired')),
  dedupe_merge_threshold   numeric(4,3) NOT NULL CHECK (dedupe_merge_threshold BETWEEN 0 AND 1),
  dedupe_judge_threshold   numeric(4,3) NOT NULL CHECK (dedupe_judge_threshold BETWEEN 0 AND 1),
  conflict_probe_threshold numeric(4,3) NOT NULL CHECK (conflict_probe_threshold BETWEEN 0 AND 1),
  created_at        timestamptz NOT NULL DEFAULT now(),
  activated_at      timestamptz,
  retired_at        timestamptz,
  CHECK (dedupe_judge_threshold < dedupe_merge_threshold),
  CHECK (conflict_probe_threshold <= dedupe_judge_threshold)
);
-- Exactly one active space at a time (the read path); a 'building' space may coexist
-- while it is backfilled (re-embedding procedure, data-model.md §7).
CREATE UNIQUE INDEX embedding_spaces_one_active ON memory.embedding_spaces ((true)) WHERE state = 'active';

-- -------------------------------------------------------------------------------------
-- Governance policy (admin-configured, D-25). Versioned, append-only. tenant_id
-- 'replicant-system' holds the org default; a tenant row overrides fields for that tenant
-- (tightening only, enforced by memory-service validation).
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.governance_policies (
  tenant_id       text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  policy_version  integer     NOT NULL CHECK (policy_version >= 1),
  document        jsonb       NOT NULL CHECK (jsonb_typeof(document) = 'object'),
  schema_id       text        NOT NULL DEFAULT 'replicant.memory-governance/v1' CHECK (schema_id ~ '^replicant\.memory-governance/v[0-9]+$'),
  digest          text        NOT NULL CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
  is_current      boolean     NOT NULL DEFAULT false,
  created_by      text        NOT NULL CHECK (created_by ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
  created_at      timestamptz NOT NULL DEFAULT now(),
  change_note     text        CHECK (length(change_note) <= 2000),
  PRIMARY KEY (tenant_id, policy_version)
);
CREATE UNIQUE INDEX governance_policies_one_current ON memory.governance_policies (tenant_id) WHERE is_current;

-- -------------------------------------------------------------------------------------
-- Entries: identity, lifecycle state, confidence, counters. Content lives in versions.
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.entries (
  entry_id          text        PRIMARY KEY CHECK (entry_id ~ '^mem_[0-9a-hjkmnp-tv-z]{26}$'),
  tenant_id         text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  memory_type       text        NOT NULL CHECK (memory_type IN ('rule', 'episode', 'procedure', 'negative', 'preference')),
  origin            text        NOT NULL CHECK (origin IN ('pr_feedback', 'agent_proposal', 'episodic_auto', 'playbook_mining',
                                                           'negative_mining', 'curator', 'import')),
  state             text        NOT NULL CHECK (state IN ('candidate', 'active', 'disabled', 'quarantined', 'deprecated',
                                                          'superseded', 'rejected', 'expired', 'purged')),
  state_seq         bigint      NOT NULL DEFAULT 1 CHECK (state_seq >= 1),        -- optimistic concurrency (ETag)
  state_changed_at  timestamptz NOT NULL DEFAULT now(),
  current_version   integer     NOT NULL DEFAULT 1 CHECK (current_version >= 1),
  -- Denormalized from the current version for indexed filtering; maintained in the same
  -- transaction as the version insert (trigger entries_scope_matches_version checks it).
  scope_level       text        NOT NULL CHECK (scope_level IN ('org', 'tenant', 'service_group', 'repo', 'path')),
  service_group     text        CHECK (service_group ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(service_group) <= 63),
  repo              text        CHECK (repo ~ '^[a-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' AND length(repo) <= 455),
  subject_key       text        CHECK (subject_key ~ '^[a-z][a-z0-9_]*(\.[a-z0-9_]+){0,6}$' AND length(subject_key) <= 128),
  enforced          boolean     NOT NULL DEFAULT false,
  -- Beta(alpha, beta) posterior over "this entry is correct and applicable" (write-path.md §6)
  confidence_alpha  numeric(10,4) NOT NULL DEFAULT 1 CHECK (confidence_alpha > 0),
  confidence_beta   numeric(10,4) NOT NULL DEFAULT 1 CHECK (confidence_beta > 0),
  confidence        numeric(6,5) GENERATED ALWAYS AS (round(confidence_alpha / (confidence_alpha + confidence_beta), 5)) STORED,
  evidence_count             integer NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
  independent_evidence_count integer NOT NULL DEFAULT 0 CHECK (independent_evidence_count >= 0),
  distinct_author_count      integer NOT NULL DEFAULT 0 CHECK (distinct_author_count >= 0),
  distinct_repo_count        integer NOT NULL DEFAULT 0 CHECK (distinct_repo_count >= 0),
  last_confirmed_at timestamptz,                          -- last independent evidence or positive outcome
  probation_until   timestamptz,                          -- heightened monitoring after auto-activation
  review_due_at     timestamptz,                          -- staleness review (curation queue)
  candidate_expires_at timestamptz,
  quarantine_id     text        CHECK (quarantine_id ~ '^mqtn_[0-9a-hjkmnp-tv-z]{26}$'),
  -- Activation hold: blocks every automatic activation or re-activation while set. Set by the system
  -- (tightening only) on a correction request, human flags, or a post-merge outcome report; cleared
  -- only by an admin action (D-25). Not a lifecycle state.
  activation_hold   boolean     NOT NULL DEFAULT false,
  hold_reason       text        CHECK (hold_reason IN ('author_correction', 'codeowner_correction', 'human_flags',
                                                       'post_merge_reverted', 'post_merge_wrong_fix', 'agent_flags')),
  injection_count   bigint      NOT NULL DEFAULT 0 CHECK (injection_count >= 0),
  last_injected_at  timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  created_by        text        NOT NULL CHECK (created_by ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
  purged_at         timestamptz,
  CONSTRAINT entries_org_tenant CHECK ((scope_level = 'org') = (tenant_id = 'replicant-system')),
  CONSTRAINT entries_scope_shape CHECK (
       (scope_level IN ('org', 'tenant')  AND service_group IS NULL AND repo IS NULL)
    OR (scope_level = 'service_group'    AND service_group IS NOT NULL AND repo IS NULL)
    OR (scope_level IN ('repo', 'path')  AND repo IS NOT NULL)),
  CONSTRAINT entries_enforced_scope CHECK (NOT enforced OR (scope_level IN ('org', 'tenant') AND memory_type IN ('rule', 'negative'))),
  CONSTRAINT entries_episode_scope CHECK (memory_type <> 'episode' OR scope_level IN ('repo', 'path')),
  CONSTRAINT entries_preference_scope CHECK (memory_type <> 'preference' OR scope_level IN ('service_group', 'repo', 'path')),
  CONSTRAINT entries_purged CHECK ((state = 'purged') = (purged_at IS NOT NULL)),
  CONSTRAINT entries_quarantine CHECK ((state = 'quarantined') <= (quarantine_id IS NOT NULL)),
  CONSTRAINT entries_hold CHECK (activation_hold = (hold_reason IS NOT NULL))
);
CREATE INDEX entries_retrieval    ON memory.entries (tenant_id, state, memory_type, scope_level);
CREATE INDEX entries_repo_active  ON memory.entries (repo, memory_type) WHERE state = 'active';
CREATE INDEX entries_subject      ON memory.entries (tenant_id, subject_key) WHERE subject_key IS NOT NULL AND state IN ('candidate', 'active');
CREATE INDEX entries_candidates   ON memory.entries (tenant_id, created_at) WHERE state = 'candidate';
CREATE INDEX entries_review_due   ON memory.entries (review_due_at) WHERE state = 'active' AND review_due_at IS NOT NULL;
CREATE INDEX entries_quarantine   ON memory.entries (quarantine_id) WHERE quarantine_id IS NOT NULL;

-- -------------------------------------------------------------------------------------
-- Entry versions: immutable content. A version is the full content of an entry,
-- including its scope and applicability facets, so a pinned ref mem_x@v is reproducible.
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.entry_versions (
  entry_id        text        NOT NULL REFERENCES memory.entries (entry_id) ON DELETE RESTRICT,
  version         integer     NOT NULL CHECK (version >= 1),
  tenant_id       text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  scope_level     text        NOT NULL CHECK (scope_level IN ('org', 'tenant', 'service_group', 'repo', 'path')),
  service_group   text        CHECK (service_group ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(service_group) <= 63),
  repo            text        CHECK (repo ~ '^[a-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' AND length(repo) <= 455),
  repo_id         text        CHECK (length(repo_id) <= 64),
  path_globs      text[]      NOT NULL DEFAULT '{}' CHECK (cardinality(path_globs) <= 20),
  languages       text[]      NOT NULL DEFAULT '{}' CHECK (cardinality(languages) <= 10),
  frameworks      text[]      NOT NULL DEFAULT '{}' CHECK (cardinality(frameworks) <= 10),
  workflow_types  text[]      NOT NULL DEFAULT '{}' CHECK (workflow_types <@ ARRAY['bugfix','feature','api_change','docs','hotfix','maintenance','pr_repair','qa_only']::text[]),
  roles           text[]      NOT NULL DEFAULT '{}' CHECK (roles <@ ARRAY['planner','implementer','qa','reviewer','repair']::text[]),
  subject_key     text        CHECK (subject_key ~ '^[a-z][a-z0-9_]*(\.[a-z0-9_]+){0,6}$' AND length(subject_key) <= 128),
  title           text        CHECK (length(title) <= 120),
  statement       text        CHECK (length(statement) BETWEEN 1 AND 4000),
  rationale       text        CHECK (length(rationale) <= 1000),
  structured      jsonb       CHECK (structured IS NULL OR jsonb_typeof(structured) = 'object'),
  structured_schema text      NOT NULL CHECK (structured_schema ~ '^replicant\.memory\.(rule|episode|procedure|negative|preference)/v[0-9]+$'),
  examples        jsonb       CHECK (examples IS NULL OR (jsonb_typeof(examples) = 'array' AND jsonb_array_length(examples) <= 4)),
  token_estimate  integer     CHECK (token_estimate >= 0),
  content_digest  text        NOT NULL CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
  change_kind     text        NOT NULL CHECK (change_kind IN ('create', 'edit', 'merge', 'scope_change', 'redaction')),
  change_note     text        CHECK (length(change_note) <= 2000),
  created_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text        NOT NULL CHECK (created_by ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
  purged_at       timestamptz,
  tsv             tsvector    GENERATED ALWAYS AS (
                    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                    setweight(to_tsvector('english', coalesce(statement, '')), 'B') ||
                    setweight(to_tsvector('english', coalesce(rationale, '')), 'C') ||
                    setweight(to_tsvector('simple', coalesce(replace(subject_key, '.', ' '), '')), 'A')
                  ) STORED,
  PRIMARY KEY (entry_id, version),
  CONSTRAINT entry_versions_scope_shape CHECK (
       (scope_level IN ('org', 'tenant')  AND service_group IS NULL AND repo IS NULL AND cardinality(path_globs) = 0)
    OR (scope_level = 'service_group'    AND service_group IS NOT NULL AND repo IS NULL AND cardinality(path_globs) = 0)
    OR (scope_level = 'repo'             AND repo IS NOT NULL AND cardinality(path_globs) = 0)
    OR (scope_level = 'path'             AND repo IS NOT NULL AND cardinality(path_globs) >= 1)),
  -- Content is present unless purged (tombstone keeps the ref resolvable as "purged").
  CONSTRAINT entry_versions_content CHECK ((purged_at IS NULL) = (statement IS NOT NULL))
);
CREATE INDEX entry_versions_tsv  ON memory.entry_versions USING gin (tsv);
CREATE INDEX entry_versions_trgm ON memory.entry_versions USING gin (statement gin_trgm_ops);
CREATE INDEX entry_versions_repo ON memory.entry_versions (tenant_id, repo, scope_level);

-- -------------------------------------------------------------------------------------
-- Embeddings: one row per (entry, version, space). halfvec without a typmod so several
-- spaces with different dimensions can coexist; each space gets a partial expression
-- HNSW index with an explicit cast (HNSW halfvec supports up to 4,000 dimensions).
--   [verified: https://github.com/pgvector/pgvector/blob/master/README.md, 2026-10-24]
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.entry_embeddings (
  entry_id     text        NOT NULL,
  version      integer     NOT NULL,
  space_id     text        NOT NULL REFERENCES memory.embedding_spaces (space_id),
  tenant_id    text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  memory_type  text        NOT NULL CHECK (memory_type IN ('rule', 'episode', 'procedure', 'negative', 'preference')),
  embedding    halfvec     NOT NULL,
  input_digest text        NOT NULL CHECK (input_digest ~ '^sha256:[0-9a-f]{64}$'),  -- digest of the exact embedded text
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, entry_id, version, space_id),
  FOREIGN KEY (entry_id, version) REFERENCES memory.entry_versions (entry_id, version) ON DELETE CASCADE
) PARTITION BY LIST (tenant_id);
-- One partition per tenant (R2-051): each tenant's HNSW graph holds only its own vectors, so a
-- tenant-filtered ANN query never scans other tenants' points. Partitions are created at tenant
-- onboarding by the migrate Job's maintenance mode through memory.ensure_tenant_partitions().
-- The default partition catches nothing in steady state (alert if non-empty).
CREATE TABLE memory.entry_embeddings_replicant_system PARTITION OF memory.entry_embeddings FOR VALUES IN ('replicant-system');
CREATE TABLE memory.entry_embeddings_default PARTITION OF memory.entry_embeddings DEFAULT;
-- Default space 'vc4-1024' (ADR-04-003). Defined on the parent: PostgreSQL creates the matching
-- partial HNSW index on every partition. Production migrations build new partitions' indexes
-- before attaching (squawk require-concurrent-index-creation).
CREATE INDEX entry_embeddings_hnsw_vc4_1024 ON memory.entry_embeddings
  USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops) WITH (m = 16, ef_construction = 128)
  WHERE space_id = 'vc4-1024';
CREATE INDEX entry_embeddings_tenant ON memory.entry_embeddings (space_id, tenant_id, memory_type);

-- -------------------------------------------------------------------------------------
-- Provenance / evidence: every occurrence that supports an entry (first source + merges).
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.provenance (
  provenance_id        bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entry_id             text        NOT NULL REFERENCES memory.entries (entry_id) ON DELETE RESTRICT,
  tenant_id            text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  supports_version     integer     NOT NULL CHECK (supports_version >= 1),
  source_kind          text        NOT NULL CHECK (source_kind IN ('pr_review_comment', 'pr_review_body', 'pr_issue_comment',
                                     'replicant_command', 'job_outcome', 'qa_failure', 'repair_rejection', 'agent_proposal',
                                     'curator', 'import')),
  source_url           text        CHECK (source_url ~ '^https://[a-z0-9.-]+/' AND length(source_url) <= 2048),
  repo                 text        CHECK (repo ~ '^[a-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  repo_id              text        CHECK (length(repo_id) <= 64),
  pr_number            integer     CHECK (pr_number >= 1),
  comment_id           bigint      CHECK (comment_id >= 1),
  review_id            bigint      CHECK (review_id >= 1),
  thread_node_id       text        CHECK (length(thread_node_id) <= 128),
  comment_digest       text        CHECK (comment_digest ~ '^sha256:[0-9a-f]{64}$'),  -- body as extracted (edit detection)
  source_excerpt       text        CHECK (length(source_excerpt) <= 2000),           -- redacted; admin view only
  author_principal_id  text        CHECK (author_principal_id ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
  author_github_user_id bigint     CHECK (author_github_user_id >= 1),
  author_repo_permission text      CHECK (author_repo_permission IN ('admin', 'maintain', 'write', 'triage', 'read', 'none')),
  author_is_codeowner  boolean,
  author_association   text        CHECK (author_association IN ('OWNER', 'MEMBER', 'COLLABORATOR', 'CONTRIBUTOR', 'FIRST_TIMER',
                                                                 'FIRST_TIME_CONTRIBUTOR', 'MANNEQUIN', 'NONE')),
  job_id               text        CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
  run_id               text        CHECK (run_id ~ '^run_[0-9a-hjkmnp-tv-z]{26}$'),
  cycle                integer     CHECK (cycle >= 0),
  delivery_id          text        CHECK (length(delivery_id) <= 64),
  release_bundle_id    text        CHECK (release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
  extraction_prompt_name    text   CHECK (length(extraction_prompt_name) <= 200),
  extraction_prompt_version integer CHECK (extraction_prompt_version >= 1),
  extraction_model     text        CHECK (length(extraction_model) <= 200),
  extraction_trace_id  text        CHECK (extraction_trace_id ~ '^[0-9a-f]{32}$'),
  generalization       jsonb       CHECK (generalization IS NULL OR jsonb_typeof(generalization) = 'object'),
  independence_key     text        NOT NULL CHECK (independence_key ~ '^sha256:[0-9a-f]{64}$'),
  approving_curator    text        CHECK (approving_curator ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
  observed_at          timestamptz NOT NULL,     -- when the source event happened
  created_at           timestamptz NOT NULL DEFAULT now(),
  withdrawn_at         timestamptz,
  withdrawn_reason     text        CHECK (withdrawn_reason IN ('comment_deleted', 'comment_edited', 'author_quarantined',
                                                               'author_unlinked', 'author_opt_out', 'curator', 'purged')),
  CONSTRAINT provenance_withdrawn CHECK ((withdrawn_at IS NULL) = (withdrawn_reason IS NULL)),
  CONSTRAINT provenance_human_source CHECK (
    source_kind NOT IN ('pr_review_comment', 'pr_review_body', 'pr_issue_comment', 'replicant_command')
    OR (author_principal_id IS NOT NULL AND comment_digest IS NOT NULL AND source_url IS NOT NULL))
);
CREATE INDEX provenance_entry   ON memory.provenance (entry_id);
CREATE INDEX provenance_author  ON memory.provenance (tenant_id, author_principal_id, observed_at);
CREATE INDEX provenance_source  ON memory.provenance (repo, pr_number, comment_id);
CREATE UNIQUE INDEX provenance_one_per_source ON memory.provenance (entry_id, independence_key) WHERE withdrawn_at IS NULL;
CREATE INDEX provenance_job     ON memory.provenance (job_id) WHERE job_id IS NOT NULL;

-- -------------------------------------------------------------------------------------
-- State transitions: append-only lifecycle history (bitemporal "as of" reads, audit).
-- D-25: every transition is by an admin (usr_) or by the system under a pinned
-- governance policy version. Nothing else can write here.
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.state_transitions (
  transition_id      bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entry_id           text        NOT NULL REFERENCES memory.entries (entry_id) ON DELETE RESTRICT,
  tenant_id          text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  from_state         text        CHECK (from_state IN ('candidate', 'active', 'disabled', 'quarantined', 'deprecated',
                                                       'superseded', 'rejected', 'expired', 'purged')),
  to_state           text        NOT NULL CHECK (to_state IN ('candidate', 'active', 'disabled', 'quarantined', 'deprecated',
                                                              'superseded', 'rejected', 'expired', 'purged')),
  entry_version      integer     NOT NULL CHECK (entry_version >= 1),
  at                 timestamptz NOT NULL DEFAULT now(),
  actor_kind         text        NOT NULL CHECK (actor_kind IN ('admin', 'system_policy', 'system_safety', 'leak_purge')),
  actor_principal_id text        NOT NULL CHECK (actor_principal_id ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
  governance_tenant_id text,
  governance_policy_version integer,
  curator_action_id  text        CHECK (curator_action_id ~ '^mact_[0-9a-hjkmnp-tv-z]{26}$'),
  quarantine_id      text        CHECK (quarantine_id ~ '^mqtn_[0-9a-hjkmnp-tv-z]{26}$'),
  reason_code        text        NOT NULL CHECK (reason_code ~ '^[a-z][a-z0-9_]{2,63}$'),
  reason_text        text        CHECK (length(reason_text) <= 2000),
  audit_event_id     text        CHECK (audit_event_id ~ '^aud_[0-9a-hjkmnp-tv-z]{26}$'),
  leak_id            text        CHECK (leak_id ~ '^leak_[0-9a-hjkmnp-tv-z]{26}$'),
  CONSTRAINT st_admin_is_human CHECK (actor_kind <> 'admin' OR (actor_principal_id ~ '^usr_' AND curator_action_id IS NOT NULL)),
  CONSTRAINT st_system_has_policy CHECK (actor_kind NOT IN ('system_policy', 'system_safety') OR (actor_principal_id = 'svc_memory-service'
                                          AND governance_tenant_id IS NOT NULL AND governance_policy_version IS NOT NULL)),
  -- Leak purge (C-ART rule 4a, WS-08 LeakPurge): executed by memory-service for an approved leak_…
  -- (two-person gate kind leak_purge); only ever enters 'purged'.
  CONSTRAINT st_leak_purge CHECK (actor_kind <> 'leak_purge' OR (actor_principal_id = 'svc_memory-service'
                                          AND leak_id IS NOT NULL AND to_state = 'purged')),
  -- system_safety may only remove an entry from service (tightening), never activate.
  CONSTRAINT st_safety_tightens CHECK (actor_kind <> 'system_safety' OR to_state IN ('quarantined', 'disabled', 'rejected', 'expired')),
  CONSTRAINT st_policy_fk FOREIGN KEY (governance_tenant_id, governance_policy_version)
    REFERENCES memory.governance_policies (tenant_id, policy_version)
);
CREATE INDEX state_transitions_entry_at ON memory.state_transitions (entry_id, at);
CREATE INDEX state_transitions_tenant_at ON memory.state_transitions (tenant_id, at);

-- -------------------------------------------------------------------------------------
-- Relations between entries (supersedes, merged_into, refines, derived_from).
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.entry_relations (
  from_entry_id  text        NOT NULL REFERENCES memory.entries (entry_id) ON DELETE RESTRICT,
  to_entry_id    text        NOT NULL REFERENCES memory.entries (entry_id) ON DELETE RESTRICT,
  relation       text        NOT NULL CHECK (relation IN ('supersedes', 'merged_into', 'refines', 'derived_from', 'promoted_from')),
  tenant_id      text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  created_at     timestamptz NOT NULL DEFAULT now(),
  created_by     text        NOT NULL CHECK (created_by ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
  PRIMARY KEY (from_entry_id, to_entry_id, relation),
  CHECK (from_entry_id <> to_entry_id)
);
CREATE INDEX entry_relations_to ON memory.entry_relations (to_entry_id, relation);

-- -------------------------------------------------------------------------------------
-- Conflicts: surfaced, never silently resolved (write-path.md §4).
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.conflicts (
  conflict_id    text        PRIMARY KEY CHECK (conflict_id ~ '^mcfl_[0-9a-hjkmnp-tv-z]{26}$'),
  tenant_id      text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  entry_a        text        NOT NULL REFERENCES memory.entries (entry_id),
  version_a      integer     NOT NULL,
  entry_b        text        NOT NULL REFERENCES memory.entries (entry_id),
  version_b      integer     NOT NULL,
  kind           text        NOT NULL CHECK (kind IN ('contradiction', 'scope_overlap', 'probable_duplicate')),
  detected_by    text        NOT NULL CHECK (detected_by IN ('subject_key', 'semantic_judge', 'runtime_assembly', 'reviewer_signal', 'curator')),
  detector_detail jsonb      NOT NULL DEFAULT '{}'::jsonb,
  state          text        NOT NULL CHECK (state IN ('open', 'resolved', 'dismissed')),
  resolution     text        CHECK (resolution IN ('keep_a', 'keep_b', 'scope_split', 'merged', 'both_valid', 'neither')),
  blocks_activation boolean  NOT NULL DEFAULT true,
  detected_at    timestamptz NOT NULL DEFAULT now(),
  resolved_at    timestamptz,
  resolved_by    text        CHECK (resolved_by ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
  curator_action_id text     CHECK (curator_action_id ~ '^mact_[0-9a-hjkmnp-tv-z]{26}$'),
  CHECK (entry_a < entry_b),                                   -- canonical pair order
  CHECK ((state = 'open') = (resolved_at IS NULL)),
  CHECK (state <> 'resolved' OR (resolution IS NOT NULL AND resolved_by IS NOT NULL)),
  FOREIGN KEY (entry_a, version_a) REFERENCES memory.entry_versions (entry_id, version),
  FOREIGN KEY (entry_b, version_b) REFERENCES memory.entry_versions (entry_id, version)
);
CREATE UNIQUE INDEX conflicts_one_open_per_pair ON memory.conflicts (entry_a, entry_b, kind) WHERE state = 'open';
CREATE INDEX conflicts_queue ON memory.conflicts (tenant_id, detected_at) WHERE state = 'open';

-- -------------------------------------------------------------------------------------
-- Snapshots (content-addressed, immutable; C-IDS memory_snapshot_id):
--   msnap_<b32_130(sha256(JCS({"tenant_id": tenant, "entries": sorted refs, "policy": policy pinned ref})))>  (C-IDS 2.0.0, CCR-04-04)
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.snapshots (
  snapshot_id     text        PRIMARY KEY CHECK (snapshot_id ~ '^msnap_[0-9a-hjkmnp-tv-z]{26}$'),
  tenant_id       text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  policy_ref      text        NOT NULL CHECK (policy_ref ~ '^policy/[a-z][a-z0-9]*(-[a-z0-9]+)*(\.[a-z][a-z0-9-]{1,31})?@[1-9][0-9]*$'),
  policy_digest   text        NOT NULL CHECK (policy_digest ~ '^sha256:[0-9a-f]{64}$'),
  spec_digest     text        NOT NULL CHECK (spec_digest ~ '^sha256:[0-9a-f]{64}$'),
  item_count      integer     NOT NULL CHECK (item_count BETWEEN 0 AND 2000),   -- hard cap = policy schema max; default policy 400
  -- Blob first, row second (C-ART rule 3 / XD-10-12): the export is written with If-None-Match
  -- before this row is inserted, so the key and digest are always present.
  export_key      text        NOT NULL CHECK (export_key ~ '^tenants/[a-z][a-z0-9-]{1,30}[a-z0-9]/snapshots/msnap_[0-9a-hjkmnp-tv-z]{26}\.json$'),
  export_sha256   text        NOT NULL CHECK (export_sha256 ~ '^[0-9a-f]{64}$'),
  renderer_version text       NOT NULL CHECK (renderer_version ~ '^replicant\.memory-block/v[0-9]+@[0-9]+\.[0-9]+\.[0-9]+$'),
  first_job_id    text        CHECK (first_job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX snapshots_tenant_created ON memory.snapshots (tenant_id, created_at);

-- Re-materialized export after a purge (C-ART rule 5, key ….purged-<purge_id>.json). Separate table
-- so memory.snapshots stays strictly append-only (R1-102 item 4).
CREATE TABLE memory.snapshot_export_replacements (
  snapshot_id      text        NOT NULL REFERENCES memory.snapshots (snapshot_id),
  purge_id         text        NOT NULL CHECK (purge_id ~ '^mprg_[0-9a-hjkmnp-tv-z]{26}$'),
  tenant_id        text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  replacement_key  text        NOT NULL CHECK (replacement_key ~ '^tenants/[a-z][a-z0-9-]{1,30}[a-z0-9]/snapshots/msnap_[0-9a-hjkmnp-tv-z]{26}\.purged-mprg_[0-9a-hjkmnp-tv-z]{26}\.json$'),
  replacement_sha256 text      NOT NULL CHECK (replacement_sha256 ~ '^[0-9a-f]{64}$'),
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_id, purge_id)
);

CREATE TABLE memory.snapshot_items (
  snapshot_id      text        NOT NULL REFERENCES memory.snapshots (snapshot_id) ON DELETE RESTRICT,
  entry_id         text        NOT NULL,
  version          integer     NOT NULL,
  tenant_id        text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  section          text        NOT NULL CHECK (section IN ('enforced', 'rule', 'procedure', 'negative', 'preference')),
  precedence_rank  integer     NOT NULL CHECK (precedence_rank >= 1),     -- 1 = highest precedence (retrieval-injection.md §3)
  scope_specificity smallint   NOT NULL CHECK (scope_specificity BETWEEN 0 AND 5),
  base_score       numeric(8,6) NOT NULL CHECK (base_score >= 0),
  overrides        text[]      NOT NULL DEFAULT '{}',   -- refs this item overrides (shown as "overrides …")
  -- Holdout arms are per (job, entry) and live in memory.holdout_assignments, never here: a
  -- content-addressed snapshot is shared by every job with the same eligible set (R1-096).
  created_at       timestamptz NOT NULL,                -- = snapshots.created_at (partition key)
  PRIMARY KEY (snapshot_id, entry_id, created_at),
  FOREIGN KEY (entry_id, version) REFERENCES memory.entry_versions (entry_id, version)
) PARTITION BY RANGE (created_at);
-- Monthly partitions, created 3 months ahead and dropped after the 2-year snapshot lifetime (C-IDS)
-- by the migrate Job's maintenance mode (R2-051 "snapshot compaction": bounded row count).
CREATE TABLE memory.snapshot_items_2026_10 PARTITION OF memory.snapshot_items FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE memory.snapshot_items_2026_11 PARTITION OF memory.snapshot_items FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE memory.snapshot_items_2026_12 PARTITION OF memory.snapshot_items FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');
CREATE INDEX snapshot_items_entry ON memory.snapshot_items (entry_id, version);

-- Job ↔ snapshot binding. A job has an initial snapshot; later context assemblies may
-- derive a revocation-filtered snapshot (entries disabled/quarantined since start are
-- removed; nothing is ever added). pull_as_of fixes the MCP pull universe.
CREATE TABLE memory.job_snapshots (
  job_id            text        NOT NULL CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
  ordinal           integer     NOT NULL CHECK (ordinal >= 1),
  tenant_id         text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  snapshot_id       text        NOT NULL REFERENCES memory.snapshots (snapshot_id),
  base_snapshot_id  text        REFERENCES memory.snapshots (snapshot_id),
  reason            text        NOT NULL CHECK (reason IN ('create', 'revocation_filter', 'policy_refresh', 'bundle_migration', 'eval_pinned')),
  -- Tenant whose memory the snapshot was built from. Equals tenant_id except for evaluation jobs
  -- (tenant 'replicant-eval') that emulate a golden task's source tenant (WS-06 memory_scope_context).
  scope_tenant_id   text        NOT NULL CHECK (scope_tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(scope_tenant_id) BETWEEN 3 AND 32),
  evaluation_mode   text        CHECK (evaluation_mode IN ('eval', 'shadow')),
  -- Exact-memory replay (R1-101, must-answer 4): the source job whose recorded injections and
  -- retrievals the eval job replays (C-JOBREQ options.evaluation.memory_replay_job_id, CCR-04-05).
  replay_of_job_id  text        CHECK (replay_of_job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
  repos             text[]      NOT NULL CHECK (cardinality(repos) BETWEEN 1 AND 20),
  repo              text        NOT NULL CHECK (repo ~ '^[a-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  workflow_type     text        NOT NULL,
  release_bundle_id text        NOT NULL CHECK (release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
  pull_as_of        timestamptz NOT NULL,
  requester_principal_id text   NOT NULL CHECK (requester_principal_id ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
  idempotency_key   text        NOT NULL CHECK (length(idempotency_key) <= 255),
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, ordinal),
  CHECK ((reason IN ('create', 'eval_pinned')) = (base_snapshot_id IS NULL)),
  CHECK (replay_of_job_id IS NULL OR evaluation_mode = 'eval'),
  CHECK (repo = repos[1]),
  CHECK (scope_tenant_id = tenant_id OR (tenant_id = 'replicant-eval' AND evaluation_mode = 'eval'))
);
CREATE UNIQUE INDEX job_snapshots_idem ON memory.job_snapshots (idempotency_key);
CREATE INDEX job_snapshots_snapshot ON memory.job_snapshots (snapshot_id);

-- Per-run injection record: exactly what each run received (authoritative blast radius).
CREATE TABLE memory.injections (
  run_id          text        PRIMARY KEY CHECK (run_id ~ '^run_[0-9a-hjkmnp-tv-z]{26}$'),
  job_id          text        NOT NULL CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
  tenant_id       text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  snapshot_id     text        NOT NULL REFERENCES memory.snapshots (snapshot_id),
  role            text        NOT NULL CHECK (role IN ('planner', 'implementer', 'qa', 'reviewer', 'repair', 'takeover', 'eval')),
  cli             text        NOT NULL CHECK (cli IN ('claude-code', 'claude-agent-sdk', 'codex')),
  node_key        text        NOT NULL CHECK (node_key ~ '^[a-z][a-z0-9_]{0,40}$'),
  cycle           integer     NOT NULL CHECK (cycle >= 0),
  injected_refs   text[]      NOT NULL CHECK (cardinality(injected_refs) <= 500),   -- in rendered order
  cut_refs        text[]      NOT NULL DEFAULT '{}',                                 -- eligible but dropped by budget
  token_count     integer     NOT NULL CHECK (token_count >= 0),
  budget_tokens   integer     NOT NULL CHECK (budget_tokens >= 0),
  tokenizer       text        NOT NULL CHECK (length(tokenizer) <= 100),
  rendered_sha256 text        NOT NULL CHECK (rendered_sha256 ~ '^[0-9a-f]{64}$'),
  renderer_version text       NOT NULL CHECK (renderer_version ~ '^replicant\.memory-block/v[0-9]+@[0-9]+\.[0-9]+\.[0-9]+$'),
  withheld_refs   text[]      NOT NULL DEFAULT '{}',    -- applicable but withheld by this job's holdout arms
  replay_of_run_id text       CHECK (replay_of_run_id ~ '^run_[0-9a-hjkmnp-tv-z]{26}$'),
  artifact_key    text        NOT NULL CHECK (length(artifact_key) <= 1024),          -- memory_injection object (artifacts bucket)
  created_at      timestamptz NOT NULL DEFAULT now(),
  CHECK (token_count <= budget_tokens)
);
CREATE INDEX injections_job  ON memory.injections (job_id);
CREATE INDEX injections_refs ON memory.injections USING gin (injected_refs);
CREATE INDEX injections_tenant_created ON memory.injections (tenant_id, created_at);

-- -------------------------------------------------------------------------------------
-- Pull log (memory MCP). Partitioned monthly; partitions are created 3 months ahead by
-- the migrate Job's scheduled maintenance mode (service roles hold no DDL, C-PG rule 1).
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.retrievals (
  retrieval_id   bigint      GENERATED ALWAYS AS IDENTITY,
  at             timestamptz NOT NULL DEFAULT now(),
  tenant_id      text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  job_id         text        NOT NULL CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
  run_id         text        NOT NULL CHECK (run_id ~ '^run_[0-9a-hjkmnp-tv-z]{26}$'),
  tool           text        NOT NULL CHECK (tool IN ('memory_search', 'memory_get', 'similar_past_work', 'memory_list_injected')),
  query_text     text        CHECK (length(query_text) <= 2000),        -- redacted (WS-08 ruleset) before insert
  query_digest   text        NOT NULL CHECK (query_digest ~ '^sha256:[0-9a-f]{64}$'),
  arguments      jsonb       NOT NULL DEFAULT '{}'::jsonb,
  snapshot_id    text        NOT NULL CHECK (snapshot_id ~ '^msnap_[0-9a-hjkmnp-tv-z]{26}$'),
  pull_as_of     timestamptz NOT NULL,
  result_refs    text[]      NOT NULL DEFAULT '{}',
  results        jsonb       NOT NULL DEFAULT '[]'::jsonb,              -- [{ref, rank, score}] exactly as returned
  latency_ms     integer     NOT NULL CHECK (latency_ms >= 0),
  PRIMARY KEY (retrieval_id, at)
) PARTITION BY RANGE (at);
CREATE TABLE memory.retrievals_2026_10 PARTITION OF memory.retrievals FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE memory.retrievals_2026_11 PARTITION OF memory.retrievals FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE memory.retrievals_2026_12 PARTITION OF memory.retrievals FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');
CREATE INDEX retrievals_job  ON memory.retrievals (job_id, at);
CREATE INDEX retrievals_refs ON memory.retrievals USING gin (result_refs);

-- -------------------------------------------------------------------------------------
-- Usage events (telemetry per entry per job). Partitioned monthly.
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.usage_events (
  event_id     bigint      GENERATED ALWAYS AS IDENTITY,
  at           timestamptz NOT NULL DEFAULT now(),
  tenant_id    text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  entry_id     text        NOT NULL CHECK (entry_id ~ '^mem_[0-9a-hjkmnp-tv-z]{26}$'),
  version      integer     NOT NULL CHECK (version >= 1),
  job_id       text        NOT NULL CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
  run_id       text        CHECK (run_id ~ '^run_[0-9a-hjkmnp-tv-z]{26}$'),
  event        text        NOT NULL CHECK (event IN ('snapshotted', 'injected', 'cut_budget', 'withheld_holdout', 'retrieved',
                                                     'agent_cited', 'agent_flagged', 'reviewer_confirmed', 'reviewer_contradicted',
                                                     'reviewer_repeated', 'revoked_midjob')),
  detail       jsonb       NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (event_id, at)
) PARTITION BY RANGE (at);
CREATE TABLE memory.usage_events_2026_10 PARTITION OF memory.usage_events FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE memory.usage_events_2026_11 PARTITION OF memory.usage_events FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE memory.usage_events_2026_12 PARTITION OF memory.usage_events FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');
CREATE INDEX usage_events_entry ON memory.usage_events (entry_id, at);
CREATE INDEX usage_events_job   ON memory.usage_events (job_id);

-- Holdout assignment for outcome experiments (memory-evals.md §3). Deterministic:
-- arm = holdout iff bucket(sha256('mholdout:' || entry_id || ':' || job_id)) < rate.
CREATE TABLE memory.holdout_assignments (
  job_id     text        NOT NULL CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
  entry_id   text        NOT NULL REFERENCES memory.entries (entry_id),
  tenant_id  text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  arm        text        NOT NULL CHECK (arm IN ('treatment', 'holdout')),
  experiment text        NOT NULL CHECK (experiment IN ('probation', 'periodic_recheck', 'candidate_shadow')),
  assigned_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, entry_id)
);
CREATE INDEX holdout_assignments_entry ON memory.holdout_assignments (entry_id, arm);

-- Job outcomes (one row per terminal job; recomputed if late PR events arrive).
CREATE TABLE memory.job_outcomes (
  job_id                 text        PRIMARY KEY CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
  tenant_id              text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  repo                   text        NOT NULL,
  workflow_type          text        NOT NULL,
  release_bundle_id      text        NOT NULL CHECK (release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
  terminal_status        text        NOT NULL CHECK (terminal_status IN ('succeeded', 'failed', 'cancelled', 'rejected', 'expired', 'taken_over')),
  pr_merged              boolean,
  review_rounds          integer     CHECK (review_rounds >= 0),        -- repair cycles triggered by authorized humans
  changes_requested_reviews integer  CHECK (changes_requested_reviews >= 0),
  human_comment_count    integer     CHECK (human_comment_count >= 0),
  repeated_comment_count integer     CHECK (repeated_comment_count >= 0), -- comments matching an entry in the snapshot
  learned_repeat_count   integer     CHECK (learned_repeat_count >= 0),   -- comments matching an active entry NOT in the snapshot
  first_pass_qa          boolean,
  first_review_approved  boolean,
  seconds_to_merge       bigint      CHECK (seconds_to_merge >= 0),
  merged_at              timestamptz,                              -- XD-W2-06-01
  memory_snapshot_ids    text[]      NOT NULL DEFAULT '{}',
  post_merge_reverted    boolean     NOT NULL DEFAULT false,   -- revert detected within 30 days (R2-017)
  outcome_report_count   integer     NOT NULL DEFAULT 0 CHECK (outcome_report_count >= 0),
  recorded_at            timestamptz NOT NULL DEFAULT now(),
  revision               integer     NOT NULL DEFAULT 1 CHECK (revision >= 1)
);
CREATE INDEX job_outcomes_repo ON memory.job_outcomes (tenant_id, repo, recorded_at);

-- Per-entry outcome statistics (nightly; memory-evals.md §4 drives auto-deprecation).
CREATE TABLE memory.entry_outcome_stats (
  entry_id                text        PRIMARY KEY REFERENCES memory.entries (entry_id),
  tenant_id               text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  exposed_jobs            integer     NOT NULL DEFAULT 0,
  holdout_jobs            integer     NOT NULL DEFAULT 0,
  exposed_review_rounds_mean numeric(8,4),
  holdout_review_rounds_mean numeric(8,4),
  exposed_repeat_rate     numeric(6,5),
  holdout_repeat_rate     numeric(6,5),
  contradicted_count      integer     NOT NULL DEFAULT 0,
  confirmed_count         integer     NOT NULL DEFAULT 0,
  p_harm                  numeric(6,5) CHECK (p_harm BETWEEN 0 AND 1),
  p_benefit               numeric(6,5) CHECK (p_benefit BETWEEN 0 AND 1),
  verdict                 text        NOT NULL DEFAULT 'insufficient_data'
                                       CHECK (verdict IN ('insufficient_data', 'helps', 'neutral', 'harms', 'ineffective')),
  computed_at             timestamptz NOT NULL DEFAULT now(),
  method_version          text        NOT NULL DEFAULT 'outcome-v1'
);

-- -------------------------------------------------------------------------------------
-- Feedback ingest ledger: one row per (source item, extraction prompt version).
-- Makes memory.write_candidates idempotent and records every non-candidate decision.
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.feedback_items (
  tenant_id        text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  source_key       text        NOT NULL CHECK (source_key ~ '^(gh-review-comment|gh-review|gh-issue-comment|agent|qa|outcome):[A-Za-z0-9_:./-]{1,200}$'),
  extractor        text        NOT NULL CHECK (extractor ~ '^[a-z0-9][a-z0-9._/-]{0,199}@[1-9][0-9]*$'),   -- prompt name@version
  job_id           text        NOT NULL CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
  source_digest    text        NOT NULL CHECK (source_digest ~ '^sha256:[0-9a-f]{64}$'),
  decision         text        NOT NULL CHECK (decision IN ('not_generalizable', 'candidate_created', 'evidence_merged',
                                    'blocked_safety', 'blocked_author', 'blocked_identity', 'opted_out', 'conflict_opened',
                                    'rate_limited', 'superseded_edit')),
  entry_id         text        REFERENCES memory.entries (entry_id),
  detail           jsonb       NOT NULL DEFAULT '{}'::jsonb,
  decided_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, source_key, extractor)
);
CREATE INDEX feedback_items_job ON memory.feedback_items (job_id);
CREATE INDEX feedback_items_decision ON memory.feedback_items (tenant_id, decision, decided_at);

-- -------------------------------------------------------------------------------------
-- Human and agent flags, author/CODEOWNER correction requests (R2-018, R2-028).
-- A flag never changes content or state by itself; governance policy may set an activation hold
-- or disable a probation entry (tightening only, system_policy) and every flag lands in the admin
-- queue with an SLA (D-25).
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.entry_flags (
  flag_id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entry_id           text        NOT NULL REFERENCES memory.entries (entry_id),
  entry_version      integer     NOT NULL CHECK (entry_version >= 1),
  tenant_id          text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  kind               text        NOT NULL CHECK (kind IN ('viewer_flag', 'author_correction', 'codeowner_correction', 'agent_flag')),
  source             text        NOT NULL CHECK (source IN ('ui_job_view', 'ui_public_view', 'pr_command', 'handoff')),
  reporter_principal_id text     NOT NULL CHECK (reporter_principal_id ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
  job_id             text        CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
  reason_code        text        NOT NULL CHECK (reason_code IN ('wrong', 'misread_comment', 'too_broad', 'outdated', 'harmful', 'unsafe', 'other')),
  reason_text        text        CHECK (length(reason_text) <= 500),     -- redacted (WS-08 ruleset) before insert
  github_comment_id  bigint      CHECK (github_comment_id >= 1),          -- for /replicant memory-wrong replies
  status             text        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'actioned', 'dismissed')),
  effect             text        NOT NULL DEFAULT 'queued' CHECK (effect IN ('queued', 'hold_set', 'probation_disabled')),
  created_at         timestamptz NOT NULL DEFAULT now(),
  due_at             timestamptz NOT NULL,                                -- admin SLA (governance flags.sla_hours)
  handled_by         text        CHECK (handled_by ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
  handled_at         timestamptz,
  curator_action_id  text        CHECK (curator_action_id ~ '^mact_[0-9a-hjkmnp-tv-z]{26}$'),
  CHECK ((status = 'open') = (handled_at IS NULL)),
  CHECK (kind <> 'agent_flag' OR reporter_principal_id ~ '^svc_'),
  CHECK (kind = 'agent_flag' OR reporter_principal_id ~ '^usr_')
);
CREATE UNIQUE INDEX entry_flags_one_open_per_reporter ON memory.entry_flags (entry_id, reporter_principal_id) WHERE status = 'open' AND kind <> 'agent_flag';
CREATE INDEX entry_flags_queue ON memory.entry_flags (tenant_id, due_at) WHERE status = 'open';

-- Post-merge outcome reports (R2-017): "this was wrong", regression, or a detected revert. Job-level.
CREATE TABLE memory.job_outcome_reports (
  report_id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id          text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  job_id             text        NOT NULL CHECK (job_id ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'),
  kind               text        NOT NULL CHECK (kind IN ('wrong_fix', 'regression', 'reverted')),
  source             text        NOT NULL CHECK (source IN ('ui', 'pr_command', 'revert_detector', 'linked_bug')),
  reporter_principal_id text     NOT NULL CHECK (reporter_principal_id ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
  evidence_url       text        CHECK (evidence_url ~ '^https://' AND length(evidence_url) <= 2048),
  note               text        CHECK (length(note) <= 1000),
  dedupe_key         text        NOT NULL CHECK (length(dedupe_key) <= 256),  -- e.g. revert:<repo_id>:<revert_pr>
  effects            jsonb       NOT NULL DEFAULT '{}'::jsonb,               -- entries held/disabled, episode disabled
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, dedupe_key),
  CHECK (source <> 'revert_detector' OR (kind = 'reverted' AND reporter_principal_id ~ '^svc_'))
);
CREATE INDEX job_outcome_reports_job ON memory.job_outcome_reports (job_id);

-- -------------------------------------------------------------------------------------
-- Quarantine and bulk disable (safety.md §5). One action, one transaction.
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.quarantines (
  quarantine_id    text        PRIMARY KEY CHECK (quarantine_id ~ '^mqtn_[0-9a-hjkmnp-tv-z]{26}$'),
  tenant_id        text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  kind             text        NOT NULL CHECK (kind IN ('quarantine', 'bulk_disable')),
  selector         jsonb       NOT NULL CHECK (jsonb_typeof(selector) = 'object'),
  selector_digest  text        NOT NULL CHECK (selector_digest ~ '^sha256:[0-9a-f]{64}$'),
  reason           text        NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000),
  entries_affected integer     NOT NULL CHECK (entries_affected >= 0),
  created_by       text        NOT NULL CHECK (created_by ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_memory-service)$'),
  created_at       timestamptz NOT NULL DEFAULT now(),
  curator_action_id text       CHECK (curator_action_id ~ '^mact_[0-9a-hjkmnp-tv-z]{26}$'),
  released_at      timestamptz,
  released_by      text        CHECK (released_by ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
  CHECK ((released_at IS NULL) = (released_by IS NULL)),
  CHECK (created_by ~ '^usr_' OR kind = 'quarantine')
);
CREATE TABLE memory.quarantine_members (
  quarantine_id  text NOT NULL REFERENCES memory.quarantines (quarantine_id),
  entry_id       text NOT NULL REFERENCES memory.entries (entry_id),
  tenant_id      text NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  prior_state    text NOT NULL CHECK (prior_state IN ('candidate', 'active', 'disabled', 'deprecated')),
  PRIMARY KEY (quarantine_id, entry_id)
);

-- Author restrictions (write-path gate G1b): set by admins or by anomaly policy.
CREATE TABLE memory.author_restrictions (
  tenant_id           text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  author_principal_id text        NOT NULL CHECK (author_principal_id ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
  restriction         text        NOT NULL CHECK (restriction IN ('no_extraction', 'review_all_candidates')),
  until               timestamptz,
  reason              text        NOT NULL CHECK (length(reason) <= 2000),
  created_by          text        NOT NULL CHECK (created_by ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_memory-service)$'),
  created_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, author_principal_id)
);

CREATE TABLE memory.anomaly_alerts (
  alert_id     bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  kind         text        NOT NULL CHECK (kind IN ('author_rate', 'tenant_rate', 'cross_repo_burst', 'new_identity',
                                                    'edit_after_extract', 'safety_block_burst', 'outcome_regression')),
  subject      text        NOT NULL CHECK (length(subject) <= 200),
  window_start timestamptz NOT NULL,
  window_end   timestamptz NOT NULL,
  metrics      jsonb       NOT NULL,
  action_taken text        NOT NULL CHECK (action_taken IN ('none', 'author_restricted', 'quarantined', 'paused_auto_activation')),
  state        text        NOT NULL CHECK (state IN ('open', 'acknowledged', 'closed')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  CHECK (window_end > window_start)
);
CREATE INDEX anomaly_alerts_open ON memory.anomaly_alerts (tenant_id, created_at) WHERE state = 'open';

-- Curator actions (D-25): every admin mutation, idempotent on Idempotency-Key.
CREATE TABLE memory.curator_actions (
  action_id         text        PRIMARY KEY CHECK (action_id ~ '^mact_[0-9a-hjkmnp-tv-z]{26}$'),
  tenant_id         text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  admin_principal_id text       NOT NULL CHECK (admin_principal_id ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
  action            text        NOT NULL CHECK (action IN ('approve', 'reject', 'edit', 'create', 'disable', 'enable', 'deprecate',
                                     'restore', 'promote_scope', 'resolve_conflict', 'dismiss_conflict', 'quarantine',
                                     'release_quarantine', 'bulk_disable', 'purge', 'export', 'restrict_author',
                                     'unrestrict_author', 'update_governance', 'second_approval')),
  target            jsonb       NOT NULL,
  request_digest    text        NOT NULL CHECK (request_digest ~ '^sha256:[0-9a-f]{64}$'),
  idempotency_key   text        NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 128),
  okta_groups_checked text[]    NOT NULL,          -- admin group(s) confirmed live for this request
  audit_event_id    text        NOT NULL CHECK (audit_event_id ~ '^aud_[0-9a-hjkmnp-tv-z]{26}$'),
  at                timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX curator_actions_idem ON memory.curator_actions (admin_principal_id, idempotency_key);
CREATE INDEX curator_actions_tenant_at ON memory.curator_actions (tenant_id, at);

-- Two-person rule for tenant- and org-scope activation and edits (scoping.md §5).
CREATE TABLE memory.pending_approvals (
  entry_id          text        PRIMARY KEY REFERENCES memory.entries (entry_id),
  tenant_id         text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  target_scope      text        NOT NULL CHECK (target_scope IN ('tenant', 'org')),
  first_version     integer     NOT NULL CHECK (first_version >= 1),
  first_action_id   text        NOT NULL REFERENCES memory.curator_actions (action_id),
  first_admin       text        NOT NULL CHECK (first_admin ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
  requested_at      timestamptz NOT NULL DEFAULT now(),
  expires_at        timestamptz NOT NULL,
  CHECK ((target_scope = 'org') = (tenant_id = 'replicant-system'))
);

-- -------------------------------------------------------------------------------------
-- Hard delete (purge) and export bookkeeping.
-- -------------------------------------------------------------------------------------
CREATE TABLE memory.purges (
  purge_id       text        PRIMARY KEY CHECK (purge_id ~ '^mprg_[0-9a-hjkmnp-tv-z]{26}$'),
  tenant_id      text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  path           text        NOT NULL CHECK (path IN ('admin', 'leak', 'horizon')),
  requested_by   text        NOT NULL CHECK (requested_by ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
  approved_by    text[]      NOT NULL DEFAULT '{}',          -- leak path: the leak_purge gate's approvers (usr_)
  leak_id        text        CHECK (leak_id ~ '^leak_[0-9a-hjkmnp-tv-z]{26}$'),
  reason_code    text        NOT NULL CHECK (reason_code IN ('poisoned', 'secret_leak', 'pii', 'legal_request', 'tenant_offboarding',
                                                             'curator', 'episode_horizon')),
  reason_text    text        NOT NULL CHECK (length(reason_text) <= 2000),
  entry_ids      text[]      NOT NULL CHECK (cardinality(entry_ids) BETWEEN 1 AND 10000),
  -- Deletion-ledger entries (C-STATE "Deletion ledger", C-IDS del_), written to the audit bucket
  -- BEFORE any delete so a restore can never resurrect purged memory (R1-102).
  deletion_ledger_ids text[] NOT NULL CHECK (cardinality(deletion_ledger_ids) >= 1),
  status         text        NOT NULL CHECK (status IN ('db_purged', 'propagating', 'complete', 'failed_retrying')),
  propagation    jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- per target: snapshot_exports, memory_injection, memory_export, langfuse, session_and_transcripts
  created_at     timestamptz NOT NULL DEFAULT now(),
  completed_at   timestamptz,
  curator_action_id text     CHECK (curator_action_id ~ '^mact_[0-9a-hjkmnp-tv-z]{26}$'),
  governance_tenant_id text,
  governance_policy_version integer,
  CHECK (path <> 'admin'   OR (requested_by ~ '^usr_' AND curator_action_id IS NOT NULL)),
  CHECK (path <> 'leak'    OR (leak_id IS NOT NULL AND cardinality(approved_by) >= 2 AND reason_code = 'secret_leak')),
  CHECK (path <> 'horizon' OR (requested_by = 'svc_memory-service' AND reason_code = 'episode_horizon'
                               AND governance_policy_version IS NOT NULL)),
  CHECK (array_to_string(deletion_ledger_ids, ',') ~ '^del_[0-9a-hjkmnp-tv-z]{26}(,del_[0-9a-hjkmnp-tv-z]{26})*$'),
  FOREIGN KEY (governance_tenant_id, governance_policy_version) REFERENCES memory.governance_policies (tenant_id, policy_version)
);

CREATE TABLE memory.exports (
  export_id      text        PRIMARY KEY CHECK (export_id ~ '^mexp_[0-9a-hjkmnp-tv-z]{26}$'),
  tenant_id      text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  requested_by   text        NOT NULL CHECK (requested_by ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
  filter         jsonb       NOT NULL,
  status         text        NOT NULL CHECK (status IN ('pending', 'running', 'complete', 'failed')),
  bucket_class   text        NOT NULL DEFAULT 'memory' CHECK (bucket_class = 'memory'),
  object_key     text        CHECK (object_key ~ '^(tenants/[a-z][a-z0-9-]{1,30}[a-z0-9]|org)/exports/mexp_[0-9a-hjkmnp-tv-z]{26}/'),
  sha256         text        CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  entry_count    integer     CHECK (entry_count >= 0),
  created_at     timestamptz NOT NULL DEFAULT now(),
  completed_at   timestamptz,
  curator_action_id text     NOT NULL CHECK (curator_action_id ~ '^mact_[0-9a-hjkmnp-tv-z]{26}$')
);

-- =====================================================================================
-- Integrity triggers
-- =====================================================================================

-- entry_versions are immutable; the only permitted UPDATE is a purge (content -> NULL)
-- performed inside memory.purge_entry(), which sets replicant.memory_purge_id.
CREATE OR REPLACE FUNCTION memory.tg_entry_versions_immutable() RETURNS trigger
  LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'memory.entry_versions rows are never deleted (purge nulls content instead)'
      USING ERRCODE = 'restrict_violation';
  END IF;
  IF NULLIF(current_setting('replicant.memory_purge_id', true), '') IS NULL
     OR NEW.purged_at IS NULL OR NEW.statement IS NOT NULL
     OR NEW.entry_id <> OLD.entry_id OR NEW.version <> OLD.version THEN
    RAISE EXCEPTION 'memory.entry_versions is immutable (entry %, version %)', OLD.entry_id, OLD.version
      USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER entry_versions_immutable BEFORE UPDATE OR DELETE ON memory.entry_versions
  FOR EACH ROW EXECUTE FUNCTION memory.tg_entry_versions_immutable();

-- Append-only tables.
CREATE OR REPLACE FUNCTION memory.tg_append_only() RETURNS trigger
  LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END $$;
CREATE TRIGGER state_transitions_append_only BEFORE UPDATE OR DELETE ON memory.state_transitions
  FOR EACH ROW EXECUTE FUNCTION memory.tg_append_only();
CREATE TRIGGER snapshots_append_only BEFORE UPDATE OR DELETE ON memory.snapshots
  FOR EACH ROW EXECUTE FUNCTION memory.tg_append_only();
CREATE TRIGGER snapshot_items_append_only BEFORE UPDATE ON memory.snapshot_items
  FOR EACH ROW EXECUTE FUNCTION memory.tg_append_only();   -- rows leave only by partition drop (2-year lifetime)
CREATE TRIGGER job_snapshots_append_only BEFORE UPDATE OR DELETE ON memory.job_snapshots
  FOR EACH ROW EXECUTE FUNCTION memory.tg_append_only();
CREATE TRIGGER injections_append_only BEFORE UPDATE OR DELETE ON memory.injections
  FOR EACH ROW EXECUTE FUNCTION memory.tg_append_only();
CREATE TRIGGER curator_actions_append_only BEFORE UPDATE OR DELETE ON memory.curator_actions
  FOR EACH ROW EXECUTE FUNCTION memory.tg_append_only();

-- snapshots are strictly append-only (blob first, row second; replacements live in
-- memory.snapshot_export_replacements).
CREATE TRIGGER snapshot_export_replacements_append_only BEFORE UPDATE OR DELETE ON memory.snapshot_export_replacements
  FOR EACH ROW EXECUTE FUNCTION memory.tg_append_only();

-- Every state change of an entry must be accompanied by a state_transitions row written
-- in the same transaction (deferred check), and state_seq must increase by exactly 1.
CREATE OR REPLACE FUNCTION memory.tg_entries_state_guard() RETURNS trigger
  LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state IS DISTINCT FROM OLD.state THEN
    IF NEW.state_seq <> OLD.state_seq + 1 THEN
      RAISE EXCEPTION 'state change on % must increment state_seq', OLD.entry_id USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM memory.state_transitions t
                    WHERE t.entry_id = NEW.entry_id AND t.to_state = NEW.state
                      AND t.from_state IS NOT DISTINCT FROM OLD.state
                      AND t.at = transaction_timestamp()) THEN
      RAISE EXCEPTION 'state change on % (% -> %) has no state_transitions row', OLD.entry_id, OLD.state, NEW.state
        USING ERRCODE = 'check_violation';
    END IF;
    NEW.state_changed_at := transaction_timestamp();
  END IF;
  IF NEW.entry_id <> OLD.entry_id OR NEW.tenant_id <> OLD.tenant_id OR NEW.memory_type <> OLD.memory_type THEN
    RAISE EXCEPTION 'entry identity columns are immutable' USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER entries_state_guard BEFORE UPDATE ON memory.entries
  FOR EACH ROW EXECUTE FUNCTION memory.tg_entries_state_guard();

CREATE OR REPLACE FUNCTION memory.tg_entries_no_delete() RETURNS trigger
  LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'memory.entries rows are tombstoned (state purged), never deleted' USING ERRCODE = 'restrict_violation';
END $$;
CREATE TRIGGER entries_no_delete BEFORE DELETE ON memory.entries
  FOR EACH ROW EXECUTE FUNCTION memory.tg_entries_no_delete();

-- entries' denormalized scope must match its current version.
CREATE OR REPLACE FUNCTION memory.tg_entries_scope_matches_version() RETURNS trigger
  LANGUAGE plpgsql AS $$
DECLARE v memory.entry_versions%ROWTYPE;
BEGIN
  SELECT * INTO v FROM memory.entry_versions WHERE entry_id = NEW.entry_id AND version = NEW.current_version;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'entry % current_version % does not exist', NEW.entry_id, NEW.current_version USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF (v.scope_level, v.service_group, v.repo, v.subject_key, v.tenant_id)
     IS DISTINCT FROM (NEW.scope_level, NEW.service_group, NEW.repo, NEW.subject_key, NEW.tenant_id) THEN
    RAISE EXCEPTION 'entry % denormalized scope does not match version %', NEW.entry_id, NEW.current_version USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER entries_scope_matches_version AFTER INSERT OR UPDATE ON memory.entries
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION memory.tg_entries_scope_matches_version();

-- =====================================================================================
-- Purge (hard delete with tombstone). Called by memory-service in one transaction per
-- purge batch after the admin action is recorded. Propagation to S3/Langfuse is tracked
-- in memory.purges.propagation (data-model.md §8).
-- =====================================================================================
CREATE OR REPLACE FUNCTION memory.purge_entry(p_entry_id text, p_purge_id text)
  RETURNS void LANGUAGE plpgsql AS $$
-- Actor, reason, and approvals come from the memory.purges row (admin, leak, or horizon path), so the
-- three paths share one implementation and one state_transitions shape (R1-103, R1-106).
DECLARE e memory.entries%ROWTYPE; pr memory.purges%ROWTYPE;
BEGIN
  SELECT * INTO pr FROM memory.purges WHERE purge_id = p_purge_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown purge %', p_purge_id; END IF;
  IF NOT (p_entry_id = ANY (pr.entry_ids)) THEN RAISE EXCEPTION 'entry % not in purge %', p_entry_id, p_purge_id; END IF;
  SELECT * INTO e FROM memory.entries WHERE entry_id = p_entry_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown entry %', p_entry_id; END IF;
  IF e.state = 'purged' THEN RETURN; END IF;                       -- idempotent
  PERFORM set_config('replicant.memory_purge_id', p_purge_id, true);
  UPDATE memory.entry_versions
     SET statement = NULL, title = NULL, rationale = NULL, structured = NULL, examples = NULL, purged_at = now()
   WHERE entry_id = p_entry_id AND purged_at IS NULL;
  DELETE FROM memory.entry_embeddings WHERE entry_id = p_entry_id;
  UPDATE memory.provenance
     SET source_excerpt = NULL, author_principal_id = NULL, author_github_user_id = NULL,
         withdrawn_at = COALESCE(withdrawn_at, now()), withdrawn_reason = COALESCE(withdrawn_reason, 'purged')
   WHERE entry_id = p_entry_id;
  UPDATE memory.entry_flags SET reason_text = NULL WHERE entry_id = p_entry_id;
  INSERT INTO memory.state_transitions (entry_id, tenant_id, from_state, to_state, entry_version, actor_kind,
                                        actor_principal_id, curator_action_id, leak_id,
                                        governance_tenant_id, governance_policy_version, reason_code)
  VALUES (p_entry_id, e.tenant_id, e.state, 'purged', e.current_version,
          CASE pr.path WHEN 'admin' THEN 'admin' WHEN 'leak' THEN 'leak_purge' ELSE 'system_policy' END,
          CASE pr.path WHEN 'admin' THEN pr.requested_by ELSE 'svc_memory-service' END,
          CASE pr.path WHEN 'admin' THEN pr.curator_action_id END,
          pr.leak_id, pr.governance_tenant_id, pr.governance_policy_version,
          CASE pr.path WHEN 'horizon' THEN 'episode_horizon' WHEN 'leak' THEN 'leak_purge' ELSE 'purge' END);
  UPDATE memory.entries SET state = 'purged', state_seq = e.state_seq + 1, purged_at = now(), quarantine_id = NULL,
                            activation_hold = false, hold_reason = NULL
   WHERE entry_id = p_entry_id;
  PERFORM set_config('replicant.memory_purge_id', '', true);
END $$;

-- The provenance_human_source CHECK must tolerate purged rows (author scrubbed).
ALTER TABLE memory.provenance DROP CONSTRAINT provenance_human_source;
ALTER TABLE memory.provenance ADD CONSTRAINT provenance_human_source CHECK (
  withdrawn_reason = 'purged'
  OR source_kind NOT IN ('pr_review_comment', 'pr_review_body', 'pr_issue_comment', 'replicant_command')
  OR (author_principal_id IS NOT NULL AND comment_digest IS NOT NULL AND source_url IS NOT NULL));

-- =====================================================================================
-- Tenant partitions for entry_embeddings (R2-051). Called by the migrate Job's maintenance mode at
-- tenant onboarding (DDL stays with replicant_migrator, C-PG rule 1). Idempotent.
-- =====================================================================================
CREATE OR REPLACE FUNCTION memory.ensure_tenant_partitions(p_tenant text) RETURNS void
  LANGUAGE plpgsql AS $$
DECLARE part text := 'entry_embeddings_t_' || replace(p_tenant, '-', '_');
BEGIN
  IF p_tenant !~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' OR length(p_tenant) NOT BETWEEN 3 AND 32 THEN
    RAISE EXCEPTION 'invalid tenant_id %', p_tenant;
  END IF;
  IF to_regclass('memory.' || part) IS NULL THEN
    EXECUTE format('CREATE TABLE memory.%I PARTITION OF memory.entry_embeddings FOR VALUES IN (%L)', part, p_tenant);
    -- XD-ORCH-04: RLS ENABLE + FORCE, canonical tenant policy, REVOKE ALL from every non-owner role.
    -- The application only ever queries the parent (whose policies and grants apply).
    PERFORM ops.secure_partition(format('memory.%I', part)::regclass);
  END IF;
END $$;
REVOKE ALL ON FUNCTION memory.ensure_tenant_partitions(text) FROM PUBLIC;

-- =====================================================================================
-- "As of" read: the state of an entry at a past instant (reproduction, pull universe).
-- =====================================================================================
CREATE OR REPLACE FUNCTION memory.state_as_of(p_entry_id text, p_at timestamptz)
  RETURNS TABLE (state text, entry_version integer)
  LANGUAGE sql STABLE AS $$
  SELECT t.to_state, t.entry_version
    FROM memory.state_transitions t
   WHERE t.entry_id = p_entry_id AND t.at <= p_at
   ORDER BY t.at DESC, t.transition_id DESC
   LIMIT 1
$$;

-- =====================================================================================
-- Row Level Security
-- =====================================================================================
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['governance_policies', 'entries', 'entry_versions', 'entry_embeddings', 'provenance',
                           'state_transitions', 'entry_relations', 'conflicts', 'snapshots', 'snapshot_items',
                           'job_snapshots', 'injections', 'retrievals', 'usage_events', 'holdout_assignments',
                           'job_outcomes', 'entry_outcome_stats', 'feedback_items', 'quarantines', 'quarantine_members',
                           'author_restrictions', 'anomaly_alerts', 'curator_actions', 'pending_approvals',
                           'purges', 'exports', 'snapshot_export_replacements', 'entry_flags', 'job_outcome_reports']
  LOOP
    EXECUTE format('ALTER TABLE memory.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE memory.%I FORCE ROW LEVEL SECURITY', t);          -- G-19
    EXECUTE format('CREATE POLICY %I ON memory.%I FOR SELECT USING (memory.rls_can_read(tenant_id))', t || '_read', t);
    EXECUTE format('CREATE POLICY %I ON memory.%I FOR INSERT WITH CHECK (memory.rls_can_write(tenant_id))', t || '_insert', t);
    EXECUTE format('CREATE POLICY %I ON memory.%I FOR UPDATE USING (memory.rls_can_write(tenant_id)) WITH CHECK (memory.rls_can_write(tenant_id))', t || '_update', t);
    EXECUTE format('CREATE POLICY %I ON memory.%I FOR DELETE USING (memory.rls_can_write(tenant_id))', t || '_delete', t);
    -- WS-10 standard cross-tenant policies (role-targeted, so ordinary roles never see an OR branch)
    EXECUTE format('CREATE POLICY %I ON memory.%I FOR SELECT TO r_cross_tenant_read USING (ops.principal_scope() = ''operator:all-tenants'')', t || '_xt_read', t);
    EXECUTE format('CREATE POLICY %I ON memory.%I FOR ALL TO r_cross_tenant_write USING (ops.principal_scope() = ''system:all-tenants'') WITH CHECK (ops.principal_scope() = ''system:all-tenants'')', t || '_xt_write', t);
  END LOOP;
END $$;
-- Governance (except the write path's own read), curator actions, quarantines, purges, exports,
-- anomaly alerts, author restrictions and pending approvals are admin data: tenant-scope sessions
-- cannot read them at all (D-25), even for their own tenant.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['curator_actions', 'quarantines', 'quarantine_members', 'purges',
                           'exports', 'anomaly_alerts', 'author_restrictions', 'pending_approvals']
  LOOP
    EXECUTE format('DROP POLICY %I ON memory.%I', t || '_read', t);
    EXECUTE format('CREATE POLICY %I ON memory.%I FOR SELECT USING (ops.principal_scope() = ''admin:memory'')', t || '_read', t);
  END LOOP;
END $$;
-- Background jobs running in tenant scope must still see their tenant's restrictions and alerts
-- (anomaly detection, author gate). Narrow read policies for that:
CREATE POLICY author_restrictions_read_own ON memory.author_restrictions FOR SELECT USING (tenant_id = ops.current_tenant());
CREATE POLICY anomaly_alerts_read_own ON memory.anomaly_alerts FOR SELECT USING (tenant_id = ops.current_tenant());
-- Governance and curator actions are written only in admin scope (D-25).
DO $$
DECLARE t text; op text;
BEGIN
  FOREACH t IN ARRAY ARRAY['governance_policies', 'curator_actions', 'pending_approvals', 'purges', 'exports', 'author_restrictions']
  LOOP
    FOREACH op IN ARRAY ARRAY['insert', 'update', 'delete'] LOOP
      EXECUTE format('DROP POLICY %I ON memory.%I', t || '_' || op, t);
    END LOOP;
    EXECUTE format('CREATE POLICY %I ON memory.%I FOR INSERT WITH CHECK (ops.principal_scope() = ''admin:memory'')', t || '_insert', t);
    EXECUTE format('CREATE POLICY %I ON memory.%I FOR UPDATE USING (ops.principal_scope() = ''admin:memory'') WITH CHECK (ops.principal_scope() = ''admin:memory'')', t || '_update', t);
    EXECUTE format('CREATE POLICY %I ON memory.%I FOR DELETE USING (ops.principal_scope() = ''admin:memory'')', t || '_delete', t);
  END LOOP;
END $$;
-- Exception: service-executed purge paths (leak, horizon) run in the tenant's scope (R1-103, R1-106).
CREATE POLICY purges_insert_service ON memory.purges FOR INSERT
  WITH CHECK (tenant_id = ops.current_tenant() AND path IN ('leak', 'horizon'));
CREATE POLICY purges_update_service ON memory.purges FOR UPDATE
  USING (tenant_id = ops.current_tenant() AND path IN ('leak', 'horizon'))
  WITH CHECK (tenant_id = ops.current_tenant() AND path IN ('leak', 'horizon'));
CREATE POLICY purges_read_service ON memory.purges FOR SELECT
  USING (tenant_id = ops.current_tenant() AND path IN ('leak', 'horizon'));
-- Exception: the anomaly policy may restrict an author automatically (system_safety, tenant scope).
CREATE POLICY author_restrictions_insert_safety ON memory.author_restrictions FOR INSERT
  WITH CHECK (tenant_id = ops.current_tenant() AND created_by = 'svc_memory-service' AND restriction = 'review_all_candidates');

-- =====================================================================================
-- Analytics views for grafana_ro (D-25: aggregates only, no content, no identities).
-- Views run with the owner's rights (security_invoker = false). Because tables FORCE RLS,
-- the views are owned by memory_analytics_owner (NOLOGIN, member of r_cross_tenant_read,
-- SELECT on the base tables); grafana_ro's role default replicant.principal_scope =
-- 'operator:all-tenants' (WS-10) makes the role-targeted _xt_read policy pass for the owner,
-- so the views aggregate across tenants. They expose no statement, author, or source columns.
-- =====================================================================================
CREATE VIEW memory.v_analytics_entry_counts AS
  SELECT tenant_id, memory_type, scope_level, state, count(*) AS entries
    FROM memory.entries GROUP BY tenant_id, memory_type, scope_level, state;

CREATE VIEW memory.v_analytics_candidate_flow AS
  SELECT tenant_id, date_trunc('day', decided_at) AS day, decision, count(*) AS items
    FROM memory.feedback_items GROUP BY tenant_id, date_trunc('day', decided_at), decision;

CREATE VIEW memory.v_analytics_transitions AS
  SELECT tenant_id, date_trunc('day', at) AS day, from_state, to_state, actor_kind, reason_code, count(*) AS transitions
    FROM memory.state_transitions GROUP BY 1, 2, 3, 4, 5, 6;

CREATE VIEW memory.v_analytics_injection AS
  SELECT tenant_id, date_trunc('day', created_at) AS day, role, count(*) AS runs,
         avg(cardinality(injected_refs))::numeric(8,2) AS avg_entries, avg(token_count)::numeric(10,1) AS avg_tokens,
         sum(cardinality(cut_refs)) AS cut_entries
    FROM memory.injections GROUP BY 1, 2, 3;

CREATE VIEW memory.v_analytics_outcomes AS
  SELECT tenant_id, workflow_type, date_trunc('week', recorded_at) AS week, count(*) AS jobs,
         avg(review_rounds)::numeric(8,3) AS avg_review_rounds,
         avg(CASE WHEN human_comment_count > 0 THEN repeated_comment_count::numeric / human_comment_count END)::numeric(6,4) AS repeated_comment_rate,
         avg(first_pass_qa::int)::numeric(6,4) AS first_pass_qa_rate,
         avg(first_review_approved::int)::numeric(6,4) AS first_review_approval_rate,
         (avg(post_merge_reverted::int) FILTER (WHERE pr_merged))::numeric(6,4) AS post_merge_revert_rate
    FROM memory.job_outcomes GROUP BY 1, 2, 3;

CREATE VIEW memory.v_analytics_curation_queue AS
  SELECT tenant_id, memory_type, scope_level, count(*) AS open_candidates,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM now() - created_at) / 86400.0)::numeric(8,2) AS age_days_p50,
         percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM now() - created_at) / 86400.0)::numeric(8,2) AS age_days_p90,
         count(*) FILTER (WHERE candidate_expires_at < now() + interval '7 days') AS expiring_within_7d
    FROM memory.entries WHERE state = 'candidate' GROUP BY 1, 2, 3;

CREATE VIEW memory.v_analytics_flags AS
  SELECT tenant_id, kind, status, count(*) AS flags, count(*) FILTER (WHERE status = 'open' AND due_at < now()) AS overdue
    FROM memory.entry_flags GROUP BY 1, 2, 3;

-- Per-job outcomes for WS-10's audited reader ops.eval_job_outcomes (XD-W2-06-01 d): IDs, status,
-- outcome codes and counts only; no free text (report notes and evidence URLs are not exposed).
-- report_kind: 'reverted' if any revert was reported, else the earliest report's kind.
-- security_invoker (XD-W2-10-07): base-table privileges and RLS are checked against the caller, so
-- ops.eval_job_outcomes (owner eval_metrics_reader, member of r_cross_tenant_write) reads across tenants
-- under the canonical 'system:all-tenants' scope through the *_xt_write policies, and a tenant-scope
-- caller sees only its own rows. It references only columns granted to eval_metrics_reader below.
CREATE VIEW memory.v_job_outcomes WITH (security_invoker = true) AS
  SELECT o.job_id, o.tenant_id, o.release_bundle_id, o.workflow_type, o.terminal_status, o.pr_merged, o.merged_at,
         o.post_merge_reverted, o.outcome_report_count, o.review_rounds, o.first_pass_qa, o.first_review_approved,
         r.report_kind, r.reported_at
    FROM memory.job_outcomes o
    LEFT JOIN LATERAL (
      SELECT (array_agg(x.kind ORDER BY (x.kind = 'reverted') DESC, x.created_at))[1] AS report_kind,
             min(x.created_at) AS reported_at
        FROM memory.job_outcome_reports x
       WHERE x.job_id = o.job_id AND x.tenant_id = o.tenant_id) r ON true;

CREATE VIEW memory.v_analytics_entry_verdicts AS
  SELECT tenant_id, verdict, count(*) AS entries FROM memory.entry_outcome_stats GROUP BY 1, 2;

-- =====================================================================================
-- Grants (roles are created by WS-10's baseline migration).
-- svc_memory_service: the only writer (C-PG). No other service role gets table access;
-- replicant-worker uses the memory-service API (C-PG open question, resolved by WS-04).
-- =====================================================================================
GRANT USAGE ON SCHEMA memory TO svc_memory_service, grafana_ro;
-- XD-ORCH-04: grants name the partitioned PARENTS and the non-partitioned tables explicitly. Never
-- `GRANT … ON ALL TABLES IN SCHEMA` (that would reach partitions); partitions keep no grants
-- (ops.secure_partition below and in WS-10's hourly sweep).
GRANT SELECT, INSERT, UPDATE ON
  memory.embedding_spaces,
  memory.governance_policies,
  memory.entries,
  memory.entry_versions,
  memory.entry_embeddings,
  memory.provenance,
  memory.state_transitions,
  memory.entry_relations,
  memory.conflicts,
  memory.snapshots,
  memory.snapshot_export_replacements,
  memory.snapshot_items,
  memory.job_snapshots,
  memory.injections,
  memory.retrievals,
  memory.usage_events,
  memory.holdout_assignments,
  memory.job_outcomes,
  memory.entry_outcome_stats,
  memory.feedback_items,
  memory.entry_flags,
  memory.job_outcome_reports,
  memory.quarantines,
  memory.quarantine_members,
  memory.author_restrictions,
  memory.anomaly_alerts,
  memory.curator_actions,
  memory.pending_approvals,
  memory.purges,
  memory.exports
  TO svc_memory_service;
GRANT DELETE ON memory.entry_embeddings, memory.holdout_assignments, memory.pending_approvals TO svc_memory_service;
REVOKE UPDATE ON memory.state_transitions, memory.snapshot_items, memory.job_snapshots, memory.injections,
                 memory.curator_actions, memory.snapshots, memory.snapshot_export_replacements FROM svc_memory_service;
-- Embedding pruning (R2-051): memory-service deletes embeddings of non-current, superseded, rejected,
-- expired and purged versions (maintenance job, per tenant).
GRANT USAGE ON ALL SEQUENCES IN SCHEMA memory TO svc_memory_service;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA memory TO svc_memory_service;
REVOKE EXECUTE ON FUNCTION memory.ensure_tenant_partitions(text) FROM svc_memory_service;   -- migrate Job only
REVOKE ALL ON ALL TABLES IN SCHEMA memory FROM grafana_ro;

-- XD-ORCH-04: secure every partition this DDL creates (later ones: ensure_tenant_partitions and
-- WS-10 maintenance).
SELECT ops.secure_partition(c.oid::regclass)
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'memory' AND c.relispartition AND c.relkind = 'r';   -- leaf tables only (index partitions are relispartition too)

-- Order matters (XD-10-18): grant grafana_ro on the views while replicant_migrator still owns them,
-- then hand ownership to memory_analytics_owner. Grants survive the ownership change.
GRANT SELECT ON memory.v_analytics_entry_counts, memory.v_analytics_candidate_flow, memory.v_analytics_transitions,
                memory.v_analytics_injection, memory.v_analytics_outcomes, memory.v_analytics_entry_verdicts,
                memory.v_analytics_curation_queue, memory.v_analytics_flags TO grafana_ro;
GRANT USAGE, CREATE ON SCHEMA memory TO memory_analytics_owner;   -- CREATE needed only for ALTER ... OWNER; revoked below
GRANT SELECT ON memory.entries, memory.feedback_items, memory.state_transitions, memory.injections,
                memory.job_outcomes, memory.entry_outcome_stats, memory.entry_flags TO memory_analytics_owner;
ALTER VIEW memory.v_analytics_entry_counts OWNER TO memory_analytics_owner;
ALTER VIEW memory.v_analytics_candidate_flow OWNER TO memory_analytics_owner;
ALTER VIEW memory.v_analytics_transitions OWNER TO memory_analytics_owner;
ALTER VIEW memory.v_analytics_injection OWNER TO memory_analytics_owner;
ALTER VIEW memory.v_analytics_outcomes OWNER TO memory_analytics_owner;
ALTER VIEW memory.v_analytics_entry_verdicts OWNER TO memory_analytics_owner;
ALTER VIEW memory.v_analytics_curation_queue OWNER TO memory_analytics_owner;
ALTER VIEW memory.v_analytics_flags OWNER TO memory_analytics_owner;
ALTER VIEW memory.v_job_outcomes OWNER TO memory_analytics_owner;   -- invoker view: the owner's privileges are not used
-- Column-level SELECT for the audited cross-tenant reader (CCR-06-15 pattern); nothing else. Because the
-- view is security_invoker, the caller also needs the base columns it reads: codes, flags, counts, IDs and
-- timestamps only. Never repo, note, evidence_url, reporter_principal_id, dedupe_key or effects.
GRANT USAGE ON SCHEMA memory TO eval_metrics_reader;
GRANT SELECT (job_id, tenant_id, release_bundle_id, workflow_type, terminal_status, pr_merged, merged_at,
              post_merge_reverted, outcome_report_count, review_rounds, first_pass_qa, first_review_approved,
              report_kind, reported_at) ON memory.v_job_outcomes TO eval_metrics_reader;
GRANT SELECT (job_id, tenant_id, release_bundle_id, workflow_type, terminal_status, pr_merged, merged_at,
              post_merge_reverted, outcome_report_count, review_rounds, first_pass_qa, first_review_approved)
  ON memory.job_outcomes TO eval_metrics_reader;
GRANT SELECT (job_id, tenant_id, kind, created_at) ON memory.job_outcome_reports TO eval_metrics_reader;
REVOKE CREATE ON SCHEMA memory FROM memory_analytics_owner;
