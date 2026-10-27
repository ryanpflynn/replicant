-- WS-06 — DDL for the release-bundle, promotion and assignment tables in schema `registry`.
-- Writer: control-plane-api through its registry module role svc_cpa_registry (WS-10 role matrix), per C-PG §2. Definition-version tables
-- in the same schema belong to WS-03 and are not repeated here. Readers (SELECT): replicant-worker,
-- publisher-worker, eval-worker, memory-service, grafana_ro.
-- Target: PostgreSQL 17+. All objects owned by replicant_migrator (C-PG rule 1). Shipped as Alembic
-- revisions generated from replicant_db.models.registry (D-26); this file is the reviewed target DDL.
-- Global (non-tenant) rows use the reserved tenant_id 'replicant-system' so every table satisfies
-- C-PG rule 4 (tenant_id NOT NULL + RLS).

CREATE SCHEMA IF NOT EXISTS registry;

-- ---------------------------------------------------------------------------------------------
-- Enumerations (as CHECK-constrained text, so adding a value is an expand-only migration).
-- ---------------------------------------------------------------------------------------------
-- promotion_state: see release-bundle.md §6 and promotion.md §2 (state machine).

CREATE TABLE registry.release_bundles (
    release_bundle_id        text        PRIMARY KEY
                                         CHECK (release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
    digest                   text        NOT NULL UNIQUE
                                         CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
    tenant_id                text        NOT NULL DEFAULT 'replicant-system'
                                         CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'),
    display_version          text        NOT NULL
                                         CHECK (display_version ~ '^[0-9]{4}\.[0-9]{2}\.[0-9]{2}-[0-9]{1,3}(\.[a-z][a-z0-9-]{1,31})?$'),
    spec                     jsonb       NOT NULL,           -- exact bundle spec (content-addressed)
    metadata                 jsonb       NOT NULL,           -- excluded from the digest
    blob_key                 text        NOT NULL            -- registry bucket: blobs/sha256/<hex>.json
                                         CHECK (blob_key ~ '^blobs/sha256/[0-9a-f]{64}\.json$'),
    worker_build_id          text        NOT NULL CHECK (worker_build_id ~ '^b[0-9]{8}-[0-9a-f]{12}$'),
    base_release_bundle_id   text        NULL
                                         CHECK (base_release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
    parent_release_bundle_id text        NULL
                                         CHECK (parent_release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
    request_name             text        NULL,
    request_digest           text        NULL CHECK (request_digest ~ '^sha256:[0-9a-f]{64}$'),
    source_commit            text        NULL CHECK (source_commit ~ '^[0-9a-f]{40}$'),
    change_classes           text[]      NOT NULL DEFAULT '{}',  -- release-bundle.md §5.3
    required_tier            text        NOT NULL CHECK (required_tier IN ('T0', 'T1', 'T2', 'T3')),
    require_shadow           boolean     NOT NULL DEFAULT false,
    promotion_state          text        NOT NULL DEFAULT 'built'
                                         CHECK (promotion_state IN ('built', 'evaluating', 'eval_passed',
                                             'eval_failed', 'shadowing', 'canary', 'promoted',
                                             'rolled_back', 'withdrawn', 'deprecated', 'revoked')),
    state_version            integer     NOT NULL DEFAULT 0,  -- optimistic concurrency for transitions
    state_changed_at         timestamptz NOT NULL DEFAULT now(),
    eval_only                boolean     NOT NULL DEFAULT false, -- R1-151: set at build when the closure uses an eval-only capability (codex app_server, claude-agent-sdk); assignment to non-eval tenants refuses it
    assignment_window_end    timestamptz NOT NULL DEFAULT (now() + interval '60 days'), -- XD-ORCH-01: release-bundle.md §3.4
    created_at               timestamptz NOT NULL DEFAULT now(),
    created_by               text        NOT NULL
                                         CHECK (created_by ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
    CONSTRAINT derived_has_base CHECK ((tenant_id = 'replicant-system') = (base_release_bundle_id IS NULL)),
    CONSTRAINT spec_is_object   CHECK (jsonb_typeof(spec) = 'object')
);
CREATE INDEX release_bundles_state_idx     ON registry.release_bundles (promotion_state, state_changed_at DESC);
CREATE INDEX release_bundles_base_idx      ON registry.release_bundles (base_release_bundle_id) WHERE base_release_bundle_id IS NOT NULL;
CREATE INDEX release_bundles_worker_idx    ON registry.release_bundles (worker_build_id);
CREATE INDEX release_bundles_tenant_idx    ON registry.release_bundles (tenant_id, created_at DESC);

-- Inverted reference index: which bundles contain a given definition version or prompt version.
-- Used for revocation fan-out (C-DEF §4.1 rule 4), blast-radius queries and the bundle diff.
CREATE TABLE registry.bundle_refs (
    release_bundle_id text NOT NULL REFERENCES registry.release_bundles (release_bundle_id),
    tenant_id         text NOT NULL DEFAULT 'replicant-system',
    ref_kind          text NOT NULL CHECK (ref_kind IN ('agent', 'environment', 'graph', 'policy',
                                                        'nodecatalog', 'prompt', 'cli', 'model', 'worker')),
    ref_name          text NOT NULL,   -- e.g. 'implementer-claude', 'reviewer-system', 'claude-code'
    ref_version       text NOT NULL,   -- integer versions as text; CLI semver; model id; build id
    ref_digest        text NULL CHECK (ref_digest ~ '^sha256:[0-9a-f]{64}$'),
    role_path         text NOT NULL,   -- JSON pointer inside spec, e.g. '/graphs/bugfix', '/agents/3'
    PRIMARY KEY (release_bundle_id, ref_kind, ref_name, role_path)
);
CREATE INDEX bundle_refs_lookup_idx ON registry.bundle_refs (ref_kind, ref_name, ref_version);

-- Append-only promotion history. Each row is one state transition with its evidence.
CREATE TABLE registry.bundle_promotion_events (
    release_bundle_id text        NOT NULL REFERENCES registry.release_bundles (release_bundle_id),
    seq               integer     NOT NULL CHECK (seq >= 1),
    tenant_id         text        NOT NULL DEFAULT 'replicant-system',
    from_state        text        NOT NULL,
    to_state          text        NOT NULL,
    tier              text        NULL CHECK (tier IN ('T0', 'T1', 'T2', 'T3')),
    actor             text        NOT NULL
                                  CHECK (actor ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
    pipeline_id       text        NULL CHECK (pipeline_id ~ '^rpl_[0-9a-hjkmnp-tv-z]{26}$'),
    comparison_id     text        NULL CHECK (comparison_id ~ '^cmp_[0-9a-hjkmnp-tv-z]{26}$'),
    reason            text        NOT NULL CHECK (length(reason) BETWEEN 3 AND 4000),
    evidence          jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- gate results, sign-off decision ids, tightening proof
    audit_event_id    text        NOT NULL CHECK (audit_event_id ~ '^aud_[0-9a-hjkmnp-tv-z]{26}$'),
    at                timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (release_bundle_id, seq)
);

-- ---------------------------------------------------------------------------------------------
-- Bundle assignment (C-DEF §4.5). One current row per environment plus child rows; every change
-- bumps `version` and writes a full snapshot to bundle_assignment_history in the same transaction.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE registry.bundle_assignments (
    env                         text        PRIMARY KEY CHECK (env IN ('local', 'dev', 'staging', 'prod')),
    tenant_id                   text        NOT NULL DEFAULT 'replicant-system',
    default_release_bundle_id   text        NOT NULL CHECK (default_release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
    rollback_release_bundle_id  text        NULL CHECK (rollback_release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
    version                     bigint      NOT NULL DEFAULT 1,
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    updated_by                  text        NOT NULL
);

CREATE TABLE registry.bundle_assignment_tenant_overrides (
    env               text        NOT NULL REFERENCES registry.bundle_assignments (env),
    tenant_id         text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'),
    release_bundle_id text        NOT NULL CHECK (release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
    kind              text        NOT NULL CHECK (kind IN ('holdback_tenant', 'pinned_by_operator', 'layer_pending')),
    reason            text        NOT NULL,
    expires_at        timestamptz NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    created_by        text        NOT NULL,
    PRIMARY KEY (env, tenant_id)
);

-- Ramps (C-DEF 3.0.0 §4.5, CCR-06-05(b)): at most 3 per environment (slots 0-2), each with a bucket
-- range [bucket_lo, bucket_lo + percent*100) that must not overlap another ramp's range in the same
-- environment (enforced by registry.check_ramp_ranges below). Intake evaluates ramps in slot order.
CREATE TABLE registry.bundle_assignment_ramps (
    env                    text         NOT NULL REFERENCES registry.bundle_assignments (env),
    slot                   smallint     NOT NULL CHECK (slot BETWEEN 0 AND 2),
    tenant_id              text         NOT NULL DEFAULT 'replicant-system',
    candidate_bundle_id    text         NOT NULL CHECK (candidate_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
    purpose                text         NOT NULL CHECK (purpose IN ('canary', 'holdback', 'experiment')),
    percent                numeric(5,2) NOT NULL CHECK (percent >= 0 AND percent <= 100),
    bucket_lo              integer      NOT NULL DEFAULT 0 CHECK (bucket_lo >= 0 AND bucket_lo <= 10000),
    -- purpose 'canary' and 'experiment' need a candidate in state canary; 'holdback' needs promoted or
    -- deprecated (C-DEF §4.4 slot table). Checked by control-plane-api in the assignment transaction.
    tenants_included       text[]       NOT NULL DEFAULT '{}',
    tenants_excluded       text[]       NOT NULL DEFAULT '{}',
    pipeline_id            text         NULL CHECK (pipeline_id ~ '^rpl_[0-9a-hjkmnp-tv-z]{26}$'),
    started_at             timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (env, slot),
    CONSTRAINT ramp_range_within_buckets CHECK (bucket_lo + (percent * 100)::integer <= 10000)
);

CREATE FUNCTION registry.check_ramp_ranges() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM registry.bundle_assignment_ramps r
        WHERE r.env = NEW.env AND r.slot <> NEW.slot
          AND int4range(r.bucket_lo, r.bucket_lo + (r.percent * 100)::integer)
              && int4range(NEW.bucket_lo, NEW.bucket_lo + (NEW.percent * 100)::integer)
    ) THEN
        RAISE EXCEPTION 'ramp bucket range overlaps another ramp in env %', NEW.env
            USING ERRCODE = 'exclusion_violation';
    END IF;
    IF EXISTS (
        SELECT 1 FROM registry.bundle_assignment_ramps r
        WHERE r.env = NEW.env AND r.slot <> NEW.slot AND r.candidate_bundle_id = NEW.candidate_bundle_id
    ) THEN
        RAISE EXCEPTION 'bundle % already holds a ramp in env %', NEW.candidate_bundle_id, NEW.env
            USING ERRCODE = 'unique_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER bundle_assignment_ramps_ranges
    AFTER INSERT OR UPDATE ON registry.bundle_assignment_ramps
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION registry.check_ramp_ranges();

CREATE TABLE registry.bundle_assignment_history (
    env          text        NOT NULL,
    version      bigint      NOT NULL,
    tenant_id    text        NOT NULL DEFAULT 'replicant-system',
    change_kind  text        NOT NULL CHECK (change_kind IN ('bootstrap_default', 'set_default', 'rollback', 'ramp_start',
                              'ramp_set', 'ramp_clear', 'override_set', 'override_clear', 'auto_rollback',
                              'tenant_exclusion_sync')),
    snapshot     jsonb       NOT NULL,   -- default, overrides, ramps exactly as committed
    reason       text        NOT NULL,
    changed_by   text        NOT NULL,
    audit_event_id text      NOT NULL CHECK (audit_event_id ~ '^aud_[0-9a-hjkmnp-tv-z]{26}$'),
    changed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (env, version)
);

-- ---------------------------------------------------------------------------------------------
-- Tenant layers and derived bundles (ADR-013, WS-06 ADR-001).
-- ---------------------------------------------------------------------------------------------
CREATE TABLE registry.tenant_layers (
    tenant_id      text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'),
    layer_version  integer     NOT NULL CHECK (layer_version >= 1),
    policies       jsonb       NOT NULL,  -- sorted pinned refs of tenant-scoped Policy versions
    digest         text        NOT NULL CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),  -- sha256(JCS(policies))
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text        NOT NULL,
    PRIMARY KEY (tenant_id, layer_version),
    UNIQUE (tenant_id, digest)
);

CREATE TABLE registry.derived_bundles (
    base_release_bundle_id text        NOT NULL CHECK (base_release_bundle_id ~ '^rb_[0-9a-hjkmnp-tv-z]{26}$'),
    tenant_id              text        NOT NULL,
    layer_version          integer     NOT NULL,
    release_bundle_id      text        NOT NULL REFERENCES registry.release_bundles (release_bundle_id),
    classification         text        NOT NULL CHECK (classification IN ('tightening', 'neutral', 'behavioral')),
    tightening_report      jsonb       NOT NULL,  -- per policyType verdicts and the failing paths, if any
    acknowledged_by        text        NULL,      -- tenant admin acknowledgement (budget sufficiency, ADR-001 §3)
    created_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (base_release_bundle_id, tenant_id, layer_version),
    FOREIGN KEY (tenant_id, layer_version) REFERENCES registry.tenant_layers (tenant_id, layer_version)
);
CREATE INDEX derived_bundles_bundle_idx ON registry.derived_bundles (release_bundle_id);

-- ---------------------------------------------------------------------------------------------
-- Worker deployment versions (XD-FIX-R1-013, WS-01 versioning.md §2). One row per replicant-worker build
-- known to the release system. `version_ready` = ready_at IS NOT NULL AND retired_at IS NULL; the C-DEF §4.5
-- precondition "candidate build registered on all task queues" reads ready_at (set by the release controller
-- after DescribeWorkerDeploymentVersion shows pollers on every task queue of the build).
-- ---------------------------------------------------------------------------------------------
CREATE TABLE registry.worker_deployment_versions (
    deployment_name     text        NOT NULL DEFAULT 'replicant-worker',
    build_id            text        NOT NULL CHECK (build_id ~ '^b[0-9]{8}-[0-9a-f]{12}$'),
    tenant_id           text        NOT NULL DEFAULT 'replicant-system',
    image_digest        text        NOT NULL CHECK (image_digest ~ '^sha256:[0-9a-f]{64}$'),
    node_catalog_digest text        NOT NULL CHECK (node_catalog_digest ~ '^sha256:[0-9a-f]{64}$'),
    registered_at       timestamptz NOT NULL DEFAULT now(),
    ready_at            timestamptz NULL,
    retire_after        timestamptz NULL,
    retired_at          timestamptz NULL,
    patch_of_build_id   text        NULL CHECK (patch_of_build_id ~ '^b[0-9]{8}-[0-9a-f]{12}$'),
    incident_id         text        NULL,
    PRIMARY KEY (deployment_name, build_id),
    CONSTRAINT retired_after_ready CHECK (retired_at IS NULL OR ready_at IS NULL OR retired_at >= ready_at)
);

-- Operator-approved model successor mapping (XD-FIX-R2-044, upgrades.md §6.5).
CREATE TABLE registry.model_successors (
    retiring_model   text        PRIMARY KEY CHECK (length(retiring_model) BETWEEN 3 AND 200),
    tenant_id        text        NOT NULL DEFAULT 'replicant-system',
    successor_model  text        NOT NULL CHECK (length(successor_model) BETWEEN 3 AND 200),
    approved_by      text[]      NOT NULL CHECK (cardinality(approved_by) >= 2),   -- two-person (G-09 model class)
    approved_at      timestamptz NOT NULL,
    evidence_comparison_id text  NOT NULL CHECK (evidence_comparison_id ~ '^cmp_[0-9a-hjkmnp-tv-z]{26}$'),
    CONSTRAINT successor_differs CHECK (successor_model <> retiring_model)
);

-- ---------------------------------------------------------------------------------------------
-- Model retirement calendar (XD-ORCH-01, release-bundle.md §3.4, upgrades.md §6.5). Maintained by
-- svc_release-controller from the gateway model catalog and provider deprecation pages; operators can
-- add or correct rows through PUT /v1/releases/model-retirements/{gateway_model_id} (release:operate).
-- ---------------------------------------------------------------------------------------------
CREATE TABLE registry.model_retirements (
    gateway_model_id text        PRIMARY KEY CHECK (length(gateway_model_id) BETWEEN 3 AND 200),
    tenant_id        text        NOT NULL DEFAULT 'replicant-system',
    retires_at       timestamptz NOT NULL,
    announced_at     timestamptz NOT NULL,
    source_url       text        NOT NULL,
    successor_model_id text      NULL,
    recorded_by      text        NOT NULL,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------
-- Prompt escrow (CCR-06-02): content-addressed copy of every prompt version a bundle pins, so a
-- deleted Langfuse version cannot strand a pinned job. Body lives in the registry bucket at
-- blobs/sha256/<hex>.json where <hex> is the bundle's prompt digest.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE registry.prompt_escrow (
    prompt_name   text        NOT NULL CHECK (prompt_name ~ '^[a-z0-9][a-z0-9._/-]{0,199}$'),
    prompt_version integer    NOT NULL CHECK (prompt_version >= 1),
    tenant_id     text        NOT NULL DEFAULT 'replicant-system',
    digest        text        NOT NULL CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
    blob_key      text        NOT NULL CHECK (blob_key ~ '^blobs/sha256/[0-9a-f]{64}\.json$'),
    langfuse_prompt_type text NOT NULL CHECK (langfuse_prompt_type IN ('text', 'chat')),
    escrowed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (prompt_name, prompt_version),
    UNIQUE (digest)
);

-- Versioned promotion policy documents (operational config, promotion.md §1).
CREATE TABLE registry.promotion_policies (
    name        text        NOT NULL,
    version     integer     NOT NULL CHECK (version >= 1),
    tenant_id   text        NOT NULL DEFAULT 'replicant-system',
    digest      text        NOT NULL CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
    document    jsonb       NOT NULL,
    source_commit text      NOT NULL CHECK (source_commit ~ '^[0-9a-f]{40}$'),
    active      boolean     NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (name, version)
);
CREATE UNIQUE INDEX promotion_policies_one_active ON registry.promotion_policies (name) WHERE active;

-- ---------------------------------------------------------------------------------------------
-- Row-level security (C-PG rule 4; canonical scope vocabulary of WS-10 schema-management.md §4.2:
-- tenant | operator:all-tenants | system:all-tenants | admin:memory | eval:platform). Every table with
-- tenant_id gets ENABLE + FORCE RLS. Platform rows (tenant 'replicant-system') are readable by every
-- reader; tenant-derived rows only in their tenant or under a cross-tenant scope.
-- ---------------------------------------------------------------------------------------------
DO $rls$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['release_bundles', 'bundle_refs', 'bundle_promotion_events', 'bundle_assignments',
        'bundle_assignment_tenant_overrides', 'bundle_assignment_ramps', 'bundle_assignment_history',
        'tenant_layers', 'derived_bundles', 'model_retirements', 'model_successors', 'worker_deployment_versions',
        'prompt_escrow', 'promotion_policies']
    LOOP
        EXECUTE format('ALTER TABLE registry.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE registry.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY %I ON registry.%I FOR SELECT USING (
            tenant_id = 'replicant-system'
            OR tenant_id = NULLIF(current_setting('replicant.tenant_id', true), '')
            OR current_setting('replicant.principal_scope', true) IN ('operator:all-tenants', 'system:all-tenants', 'eval:platform'))$p$,
            t || '_read', t);
        EXECUTE format($p$CREATE POLICY %I ON registry.%I FOR ALL TO svc_cpa_registry USING (true) WITH CHECK (true)$p$,
            t || '_writer', t);
    END LOOP;
END
$rls$;

-- Promotion events and assignment history are append-only for the writer as well.
REVOKE UPDATE, DELETE ON registry.bundle_promotion_events, registry.bundle_assignment_history FROM svc_cpa_registry;

-- Grants (C-PG §2 readers). Created by the same migration.
GRANT USAGE ON SCHEMA registry TO svc_replicant_worker, svc_publisher_worker, svc_eval_worker, svc_memory_service, grafana_ro;
-- XD-FIX-ORCH-04: grants name tables explicitly (never ON ALL TABLES IN SCHEMA); registry has no partitions.
GRANT SELECT ON registry.release_bundles, registry.bundle_refs, registry.bundle_promotion_events,
    registry.bundle_assignments, registry.bundle_assignment_tenant_overrides, registry.bundle_assignment_ramps,
    registry.bundle_assignment_history, registry.tenant_layers, registry.derived_bundles,
    registry.model_retirements, registry.model_successors, registry.worker_deployment_versions,
    registry.prompt_escrow, registry.promotion_policies
    TO svc_replicant_worker, svc_publisher_worker, svc_eval_worker, svc_memory_service, grafana_ro;
GRANT SELECT, INSERT, UPDATE ON registry.release_bundles, registry.bundle_assignments,
    registry.bundle_assignment_tenant_overrides, registry.bundle_assignment_ramps,
    registry.tenant_layers, registry.derived_bundles, registry.prompt_escrow,
    registry.promotion_policies, registry.bundle_refs, registry.model_retirements, registry.model_successors,
    registry.worker_deployment_versions TO svc_cpa_registry;
GRANT DELETE ON registry.bundle_assignment_tenant_overrides, registry.bundle_assignment_ramps TO svc_cpa_registry;
GRANT SELECT, INSERT ON registry.bundle_promotion_events, registry.bundle_assignment_history TO svc_cpa_registry;

-- Promotion-state transition guard: only legal edges (promotion.md §2) may be written. The
-- control-plane-api release module also enforces this; the trigger is the backstop.
CREATE FUNCTION registry.check_promotion_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    legal constant text[] := ARRAY[
        'built>evaluating', 'built>withdrawn', 'built>revoked',
        'evaluating>eval_passed', 'evaluating>eval_failed', 'evaluating>built', 'evaluating>withdrawn', 'evaluating>revoked',
        'eval_failed>evaluating', 'eval_failed>withdrawn', 'eval_failed>revoked',
        'eval_passed>shadowing', 'eval_passed>canary', 'eval_passed>promoted', 'eval_passed>withdrawn', 'eval_passed>revoked',
        'shadowing>canary', 'shadowing>eval_failed', 'shadowing>withdrawn', 'shadowing>revoked',
        'canary>promoted', 'canary>rolled_back', 'canary>revoked',
        'rolled_back>evaluating', 'rolled_back>withdrawn', 'rolled_back>revoked',
        'promoted>deprecated', 'promoted>rolled_back', 'promoted>revoked',
        'deprecated>promoted', 'deprecated>revoked'
    ];
BEGIN
    IF NEW.promotion_state IS DISTINCT FROM OLD.promotion_state THEN
        IF NOT (OLD.promotion_state || '>' || NEW.promotion_state) = ANY (legal) THEN
            RAISE EXCEPTION 'illegal promotion transition % -> % for %',
                OLD.promotion_state, NEW.promotion_state, OLD.release_bundle_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.state_version <> OLD.state_version + 1 THEN
            RAISE EXCEPTION 'state_version must increment by 1' USING ERRCODE = 'serialization_failure';
        END IF;
        NEW.state_changed_at := now();
    END IF;
    IF NEW.spec IS DISTINCT FROM OLD.spec OR NEW.digest IS DISTINCT FROM OLD.digest THEN
        RAISE EXCEPTION 'release bundles are immutable' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER release_bundles_transition_guard
    BEFORE UPDATE ON registry.release_bundles
    FOR EACH ROW EXECUTE FUNCTION registry.check_promotion_transition();
