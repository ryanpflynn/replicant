-- Schema `registry`, definition tables (C-PG: writer control-plane-api; DDL owner WS-03 for definitions,
-- WS-06 for bundles, promotion, and assignment). Target DDL for replicant-db models/migrations (D-26).

CREATE SCHEMA IF NOT EXISTS registry;

-- Immutable definition versions (C-DEF section 4.1). Only `state` columns change, via definition_state_events.
CREATE TABLE registry.definition_versions (
    kind            text        NOT NULL CHECK (kind IN ('agent','environment','graph','policy','nodecatalog')),
    scope_level     text        NOT NULL CHECK (scope_level IN ('global','tenant')),
    tenant_id       text        NOT NULL DEFAULT '' CHECK (tenant_id = '' OR tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'),
    name            text        NOT NULL CHECK (name ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND char_length(name) <= 63),
    version         integer     NOT NULL CHECK (version >= 1),
    digest          text        NOT NULL CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
    api_version     text        NOT NULL,
    policy_type     text,       -- set for kind = policy
    kind_schema_id  text        NOT NULL,    -- e.g. https://schemas.replicant.example.com/kinds/agent-definition/v1.json
    document        jsonb       NOT NULL,    -- full authored document (envelope + spec), canonical JSON
    blob_key        text        NOT NULL,    -- registry bucket key blobs/sha256/<hex>.json (written first, C-DEF 4.1 rule 5)
    refs            jsonb       NOT NULL DEFAULT '[]'::jsonb,  -- outgoing selectors/refs found via x-replicant-ref (closure input)
    source_commit   text        NOT NULL CHECK (source_commit ~ '^[0-9a-f]{40}$'),
    source_path     text        NOT NULL,
    published_at    timestamptz NOT NULL DEFAULT now(),
    published_by    text        NOT NULL CHECK (published_by ~ '^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$'),
    state           text        NOT NULL DEFAULT 'active' CHECK (state IN ('active','deprecated','revoked')),
    state_changed_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((scope_level = 'global' AND tenant_id = '') OR (scope_level = 'tenant' AND tenant_id <> '')),
    CHECK ((kind = 'policy') = (policy_type IS NOT NULL)),
    PRIMARY KEY (kind, scope_level, tenant_id, name, version)
);
CREATE UNIQUE INDEX definition_versions_digest_uq
    ON registry.definition_versions (kind, scope_level, tenant_id, name, digest);
CREATE INDEX definition_versions_refs_gin ON registry.definition_versions USING gin (refs jsonb_path_ops);

-- Version index objects (C-DEF 2.3.0 rule 5): blob → index/<kind>/<scope>/<name>/<version>.json → row. The index is
-- in S3 (registry bucket); this table records nothing extra, and rebuild after a restore reads the index objects.

-- Head pointer: the version whose content equals the document at the most recent publish from the config repo's
-- default branch. Unpinned selectors (agent/x) resolve to the head (CCR-03-02), which makes a Git revert to older
-- content (identical digest => existing older version) become the head again.
CREATE TABLE registry.definition_heads (
    kind            text        NOT NULL,
    scope_level     text        NOT NULL,
    tenant_id       text        NOT NULL DEFAULT '',
    name            text        NOT NULL,
    version         integer     NOT NULL,
    digest          text        NOT NULL,
    source_commit   text        NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz,             -- file removed from the repo: no head; selectors fail, pinned refs still resolve
    PRIMARY KEY (kind, scope_level, tenant_id, name),
    FOREIGN KEY (kind, scope_level, tenant_id, name, version)
        REFERENCES registry.definition_versions (kind, scope_level, tenant_id, name, version)
);

CREATE TABLE registry.definition_state_events (
    event_id        bigserial   PRIMARY KEY,
    kind            text        NOT NULL,
    scope_level     text        NOT NULL,
    tenant_id       text        NOT NULL DEFAULT '',
    name            text        NOT NULL,
    version         integer     NOT NULL,
    from_state      text        NOT NULL,
    to_state        text        NOT NULL CHECK (to_state IN ('active','deprecated','revoked')),
    reason          text        NOT NULL,
    actor           text        NOT NULL,
    audit_event_id  text        NOT NULL CHECK (audit_event_id ~ '^aud_[0-9a-hjkmnp-tv-z]{26}$'),
    created_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (kind, scope_level, tenant_id, name, version)
        REFERENCES registry.definition_versions (kind, scope_level, tenant_id, name, version)
);

-- Publish requests (POST /v1/registry/publish), one per CI call; idempotent on (commit_sha, request_digest).
CREATE TABLE registry.publish_requests (
    publish_id      text        PRIMARY KEY CHECK (publish_id ~ '^pub_[0-9a-hjkmnp-tv-z]{26}$'),
    commit_sha      text        NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
    request_digest  text        NOT NULL CHECK (request_digest ~ '^sha256:[0-9a-f]{64}$'),
    source          text        NOT NULL CHECK (source IN ('config_repo','worker_ci')),
    dry_run         boolean     NOT NULL DEFAULT false,
    caller          text        NOT NULL,
    outcome         text        NOT NULL CHECK (outcome IN ('published','no_change','rejected')),
    results         jsonb       NOT NULL,   -- [{path, ref, digest, created, head}] or violations
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (commit_sha, request_digest, dry_run, source)
);

-- Image attestation cache for publish-time checks (environment images, CLI images, service images).
CREATE TABLE registry.image_verifications (
    image_digest    text        PRIMARY KEY CHECK (image_digest ~ '^sha256:[0-9a-f]{64}$'),
    repository      text        NOT NULL,
    signature_ok    boolean     NOT NULL,
    provenance_ok   boolean     NOT NULL,
    sbom_digest     text,
    build_spec_digest text,
    labels          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    verified_at     timestamptz NOT NULL DEFAULT now()
);

-- Row level security (C-PG rule 4; XD-FIX-R1-160, XD-10-20). The three tables with `tenant_id` get WS-10's canonical
-- tenant policies (ENABLE + FORCE; `tenant_id = ops.current_tenant()`, cross-tenant read for r_cross_tenant_read
-- under scope `operator:all-tenants`, cross-tenant write for r_cross_tenant_write under `system:all-tenants`).
-- Global definitions (`scope_level = 'global'`, `tenant_id = ''`) are platform rows every tenant resolves against,
-- so two extra permissive policies make them visible to every reader and writable only by the registry writer
-- (control-plane-api's registry module, svc_cpa_registry via r_registry_write). publish_requests and
-- image_verifications hold no tenant data (no tenant_id column).
SELECT ops.apply_tenant_rls('registry.definition_versions');
SELECT ops.apply_tenant_rls('registry.definition_heads');
SELECT ops.apply_tenant_rls('registry.definition_state_events');
CREATE POLICY rls_definition_versions_global_read ON registry.definition_versions
    AS PERMISSIVE FOR SELECT TO PUBLIC USING (tenant_id = '');
CREATE POLICY rls_definition_versions_global_write ON registry.definition_versions
    AS PERMISSIVE FOR ALL TO r_registry_write USING (tenant_id = '') WITH CHECK (tenant_id = '');
CREATE POLICY rls_definition_heads_global_read ON registry.definition_heads
    AS PERMISSIVE FOR SELECT TO PUBLIC USING (tenant_id = '');
CREATE POLICY rls_definition_heads_global_write ON registry.definition_heads
    AS PERMISSIVE FOR ALL TO r_registry_write USING (tenant_id = '') WITH CHECK (tenant_id = '');
CREATE POLICY rls_definition_state_events_global_read ON registry.definition_state_events
    AS PERMISSIVE FOR SELECT TO PUBLIC USING (tenant_id = '');
CREATE POLICY rls_definition_state_events_global_write ON registry.definition_state_events
    AS PERMISSIVE FOR ALL TO r_registry_write USING (tenant_id = '') WITH CHECK (tenant_id = '');

-- Grants (C-PG rule 1, grant model v2, XD-FIX-R1-160). WS-10 grants schema USAGE and role memberships; owners grant
-- table privileges on named tables only (never ON ALL TABLES IN SCHEMA, never to a login that has a capability role).
-- Reads: r_registry_read (inherited by svc_cpa_read, replicant-worker, publisher-worker, eval-worker, memory-service)
-- and grafana_ro (C-PG rule 6 / §2). Writes: r_registry_write (inherited by svc_cpa_registry only), narrowed per
-- table: definition_state_events is append-only; definition_versions changes only `state` (trigger below).
GRANT SELECT ON registry.definition_versions, registry.definition_heads, registry.definition_state_events,
                registry.publish_requests, registry.image_verifications
    TO r_registry_read, grafana_ro;
GRANT SELECT, INSERT, UPDATE ON registry.definition_versions, registry.definition_heads, registry.image_verifications
    TO r_registry_write;
GRANT SELECT, INSERT ON registry.definition_state_events, registry.publish_requests TO r_registry_write;
GRANT USAGE ON SEQUENCE registry.definition_state_events_event_id_seq TO r_registry_write;
-- Immutability guard: no role may UPDATE document/digest columns; enforced by a trigger.
CREATE FUNCTION registry.forbid_definition_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.document IS DISTINCT FROM OLD.document OR NEW.digest IS DISTINCT FROM OLD.digest
     OR NEW.version IS DISTINCT FROM OLD.version OR NEW.name IS DISTINCT FROM OLD.name THEN
    RAISE EXCEPTION 'registry.definition_versions is immutable except state';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER definition_versions_immutable BEFORE UPDATE ON registry.definition_versions
  FOR EACH ROW EXECUTE FUNCTION registry.forbid_definition_mutation();
CREATE RULE definition_versions_no_delete AS ON DELETE TO registry.definition_versions DO INSTEAD NOTHING;
