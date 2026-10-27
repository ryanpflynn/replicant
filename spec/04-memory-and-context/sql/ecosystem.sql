-- =====================================================================================
-- Schema `ecosystem` — reviewed target DDL (WS-04). Contract C-PG 2.0.0: single writer
-- `ecosystem-indexer` (role svc_ecosystem_indexer). Readers: replicant-worker (briefings),
-- memory-service, context-mcp. Graph storage decision: plain Postgres tables with
-- recursive CTEs (ADR-04-004).
--
-- Visibility model (ecosystem-context.md §6):
--   * Catalog metadata (services, owners, edges, contract element names) is org-visible
--     by design: blast radius crosses tenants. tenant_id is the OWNING tenant.
--   * Contract documents and call sites contain repository content and are filtered by the
--     requesting job's repo visibility set, passed per transaction in
--     replicant.visible_repo_ids (comma-separated GitHub repo node IDs), as defense in depth
--     behind context-mcp's own check (D-18: no confused deputy).
-- Validated: applied to PostgreSQL 18 + pgvector 0.8.1 and exercised by test_ecosystem.sql
-- on 2026-10-24.
-- =====================================================================================
CREATE SCHEMA IF NOT EXISTS ecosystem AUTHORIZATION replicant_migrator;
SET search_path = ecosystem, public;

-- Visibility helper. Writes are restricted by role-targeted policies to the single writer
-- (svc_ecosystem_indexer), so no cross-tenant session scope is needed (WS-09 G-04).
CREATE OR REPLACE FUNCTION ecosystem.repo_visible(p_repo_id text) RETURNS boolean
  LANGUAGE sql STABLE PARALLEL SAFE
  AS $$ SELECT p_repo_id = ANY (string_to_array(NULLIF(current_setting('replicant.visible_repo_ids', true), ''), ',')) $$;

-- Monotonic graph version: every indexer commit increments it; briefings record it.
CREATE TABLE ecosystem.graph_versions (
  graph_seq     bigint      PRIMARY KEY CHECK (graph_seq >= 1),
  committed_at  timestamptz NOT NULL DEFAULT now(),
  source_run_ids bigint[]   NOT NULL,
  summary       jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE ecosystem.sources (
  source_id      text        PRIMARY KEY CHECK (source_id ~ '^[a-z][a-z0-9-]{2,62}$'),
  kind           text        NOT NULL CHECK (kind IN ('backstage', 'codeowners', 'repo_scan', 'ci_artifact', 'tempo_service_graph',
                                                      'build_graph', 'deploy_manifest', 'incidents', 'fixture')),
  config         jsonb       NOT NULL DEFAULT '{}'::jsonb,     -- no secrets: secretRef names only
  schedule_cron  text        NOT NULL CHECK (length(schedule_cron) <= 64),
  max_staleness  interval    NOT NULL,                         -- freshness SLO for this source
  enabled        boolean     NOT NULL DEFAULT true,
  last_success_at timestamptz,
  last_run_id    bigint
);

CREATE TABLE ecosystem.source_runs (
  run_id        bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id     text        NOT NULL REFERENCES ecosystem.sources (source_id),
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,
  status        text        NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
  counts        jsonb       NOT NULL DEFAULT '{}'::jsonb,
  error         text        CHECK (length(error) <= 4000),
  graph_seq     bigint      REFERENCES ecosystem.graph_versions (graph_seq)
);
CREATE INDEX source_runs_source ON ecosystem.source_runs (source_id, started_at DESC);

CREATE TABLE ecosystem.repos (
  repo_id         text        PRIMARY KEY CHECK (length(repo_id) BETWEEN 1 AND 64),     -- GitHub node ID
  tenant_id       text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  repo            text        NOT NULL UNIQUE CHECK (repo ~ '^[a-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  default_branch  text        NOT NULL CHECK (length(default_branch) <= 255),
  visibility      text        NOT NULL CHECK (visibility IN ('public', 'internal', 'private')),
  archived        boolean     NOT NULL DEFAULT false,
  primary_languages text[]    NOT NULL DEFAULT '{}',
  frameworks      text[]      NOT NULL DEFAULT '{}',
  indexed_sha     text        CHECK (indexed_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  indexed_at      timestamptz,
  zoekt_indexed_sha text      CHECK (zoekt_indexed_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  zoekt_indexed_at timestamptz,
  first_seen_at   timestamptz NOT NULL DEFAULT now(),
  last_seen_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX repos_tenant ON ecosystem.repos (tenant_id);

CREATE TABLE ecosystem.services (
  service_ref     text        PRIMARY KEY CHECK (service_ref ~ '^(component|resource|system):[a-z0-9][a-z0-9_.-]{0,62}/[a-z0-9][a-z0-9_.-]{0,62}$'),
  tenant_id       text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  name            text        NOT NULL CHECK (length(name) <= 128),
  kind            text        NOT NULL CHECK (kind IN ('service', 'library', 'website', 'worker', 'database', 'queue', 'external', 'other')),
  system_ref      text        CHECK (system_ref ~ '^system:'),
  service_group   text        CHECK (service_group ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'),          -- memory scope "service_group"
  owner_group     text        CHECK (length(owner_group) <= 200),
  lifecycle       text        CHECK (lifecycle IN ('experimental', 'production', 'deprecated')),
  tier            smallint    CHECK (tier BETWEEN 0 AND 4),
  description     text        CHECK (length(description) <= 2000),          -- untrusted (from catalog files)
  sources         text[]      NOT NULL,
  first_seen_at   timestamptz NOT NULL DEFAULT now(),
  last_seen_at    timestamptz NOT NULL DEFAULT now(),
  graph_seq       bigint      NOT NULL REFERENCES ecosystem.graph_versions (graph_seq),
  removed_at      timestamptz
);
CREATE INDEX services_tenant ON ecosystem.services (tenant_id) WHERE removed_at IS NULL;
CREATE INDEX services_name_trgm ON ecosystem.services USING gin (name gin_trgm_ops);

CREATE TABLE ecosystem.service_repos (
  service_ref   text        NOT NULL REFERENCES ecosystem.services (service_ref),
  repo_id       text        NOT NULL REFERENCES ecosystem.repos (repo_id),
  path_prefix   text        NOT NULL DEFAULT '' CHECK (length(path_prefix) <= 1024),     -- monorepo subtree
  tenant_id     text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  source        text        NOT NULL CHECK (source IN ('backstage', 'repo_scan', 'deploy_manifest', 'fixture')),
  PRIMARY KEY (service_ref, repo_id, path_prefix)
);
CREATE INDEX service_repos_repo ON ecosystem.service_repos (repo_id);

-- CODEOWNERS rules as parsed at a commit (first found of .github/, root, docs/; last match wins).
CREATE TABLE ecosystem.owner_rules (
  repo_id       text        NOT NULL REFERENCES ecosystem.repos (repo_id),
  commit_sha    text        NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  line_no       integer     NOT NULL CHECK (line_no >= 1),
  pattern       text        NOT NULL CHECK (length(pattern) <= 1024),
  owners        text[]      NOT NULL,
  tenant_id     text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  is_current    boolean     NOT NULL DEFAULT true,
  PRIMARY KEY (repo_id, commit_sha, line_no)
);
CREATE INDEX owner_rules_current ON ecosystem.owner_rules (repo_id, line_no) WHERE is_current;

CREATE TABLE ecosystem.contracts (
  contract_ref  text        PRIMARY KEY CHECK (contract_ref ~ '^(openapi|protobuf|graphql|asyncapi|jsonschema|avro):[A-Za-z0-9_-]{1,64}:[A-Za-z0-9._/-]+$'
                                     AND length(contract_ref) <= 1100),
  -- <kind>:<repo_id>:<path or logical name>
  kind          text        NOT NULL CHECK (kind IN ('openapi', 'protobuf', 'graphql', 'asyncapi', 'jsonschema', 'avro')),
  repo_id       text        NOT NULL REFERENCES ecosystem.repos (repo_id),
  path          text        NOT NULL CHECK (length(path) <= 1024),
  service_ref   text        REFERENCES ecosystem.services (service_ref),
  tenant_id     text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  current_digest text       CHECK (current_digest ~ '^sha256:[0-9a-f]{64}$'),
  removed_at    timestamptz
);
CREATE INDEX contracts_repo ON ecosystem.contracts (repo_id);
CREATE INDEX contracts_service ON ecosystem.contracts (service_ref);

-- Normalized contract documents (derived reference data; compressed, capped at 2 MiB).
CREATE TABLE ecosystem.contract_versions (
  contract_ref  text        NOT NULL REFERENCES ecosystem.contracts (contract_ref),
  digest        text        NOT NULL CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),       -- sha256(JCS(normalized))
  repo_id       text        NOT NULL,
  tenant_id     text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  commit_sha    text        NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  git_blob_sha  text        CHECK (git_blob_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  source        text        NOT NULL CHECK (source IN ('repo_scan', 'ci_artifact', 'backstage', 'fixture')),
  extractor     text        NOT NULL CHECK (length(extractor) <= 100),                -- e.g. 'buf-image@1.75.0'
  normalized    jsonb       NOT NULL CHECK (pg_column_size(normalized) <= 2097152),
  summary       jsonb       NOT NULL DEFAULT '{}'::jsonb,                            -- counts, version string
  extracted_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (contract_ref, digest)
);
ALTER TABLE ecosystem.contract_versions ALTER COLUMN normalized SET COMPRESSION lz4;
CREATE INDEX contract_versions_commit ON ecosystem.contract_versions (repo_id, commit_sha);

-- Addressable elements of a contract (operations, RPCs, types, channels).
CREATE TABLE ecosystem.contract_elements (
  contract_ref  text        NOT NULL,
  digest        text        NOT NULL,
  element_key   text        NOT NULL CHECK (length(element_key) BETWEEN 1 AND 512),
    -- 'GET /v1/invoices/{id}', 'ledger.v1.LedgerService/PostEntry', 'Query.invoice', 'invoices.created'
  element_kind  text        NOT NULL CHECK (element_kind IN ('http_operation', 'rpc_method', 'message_type', 'graphql_field',
                                                             'graphql_type', 'channel', 'schema')),
  schema_digest text        NOT NULL CHECK (schema_digest ~ '^sha256:[0-9a-f]{64}$'),
  source_path   text        CHECK (length(source_path) <= 1024),
  tenant_id     text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  PRIMARY KEY (contract_ref, digest, element_key),
  FOREIGN KEY (contract_ref, digest) REFERENCES ecosystem.contract_versions (contract_ref, digest)
);
CREATE INDEX contract_elements_key ON ecosystem.contract_elements (element_key);

-- Dependency and call graph. One row per (edge, evidence source).
CREATE TABLE ecosystem.edges (
  from_service  text        NOT NULL REFERENCES ecosystem.services (service_ref),
  to_service    text        NOT NULL REFERENCES ecosystem.services (service_ref),
  edge_kind     text        NOT NULL CHECK (edge_kind IN ('consumes_api', 'depends_on', 'calls_runtime', 'publishes_to',
                                                          'subscribes_to', 'build_dependency', 'client_stub_usage')),
  via           text        NOT NULL DEFAULT '' CHECK (length(via) <= 512),       -- contract_ref, topic, or module path
  source        text        NOT NULL CHECK (source IN ('backstage', 'tempo_service_graph', 'build_graph', 'code_search',
                                                       'deploy_manifest', 'fixture')),
  confidence    numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  calls_per_min numeric(12,3) CHECK (calls_per_min >= 0),                      -- runtime edges only (7-day average)
  error_ratio   numeric(6,5) CHECK (error_ratio BETWEEN 0 AND 1),
  evidence      jsonb       NOT NULL DEFAULT '{}'::jsonb,
  tenant_id     text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz NOT NULL DEFAULT now(),
  graph_seq     bigint      NOT NULL REFERENCES ecosystem.graph_versions (graph_seq),
  PRIMARY KEY (from_service, to_service, edge_kind, via, source),
  CHECK (from_service <> to_service)
);
CREATE INDEX edges_to ON ecosystem.edges (to_service, edge_kind);
CREATE INDEX edges_via ON ecosystem.edges (via) WHERE via <> '';

-- Deployed revisions per service and environment (R1-104: contract revision 'deployed_prod').
-- Source: the GitOps repo (image reference → OCI label org.opencontainers.image.revision) read by the
-- deploy-manifest extractor. When the deployed commit changes, the indexer extracts the service's
-- contracts at that commit (contract_versions rows with that commit_sha).
CREATE TABLE ecosystem.deployments (
  service_ref   text        NOT NULL REFERENCES ecosystem.services (service_ref),
  environment   text        NOT NULL CHECK (environment IN ('prod', 'staging', 'dev')),
  repo_id       text        NOT NULL REFERENCES ecosystem.repos (repo_id),
  commit_sha    text        NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  image_digest  text        CHECK (image_digest ~ '^sha256:[0-9a-f]{64}$'),
  tenant_id     text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  observed_at   timestamptz NOT NULL DEFAULT now(),
  source        text        NOT NULL CHECK (source IN ('deploy_manifest', 'fixture')),
  graph_seq     bigint      NOT NULL REFERENCES ecosystem.graph_versions (graph_seq),
  PRIMARY KEY (service_ref, environment)
);

-- Consumer call sites of contract elements (precomputed weekly via Zoekt; live queries
-- go to Zoekt through context-mcp). Contains code locations: repo-visibility filtered.
CREATE TABLE ecosystem.call_sites (
  element_key   text        NOT NULL CHECK (length(element_key) <= 512),
  contract_ref  text        NOT NULL REFERENCES ecosystem.contracts (contract_ref),
  repo_id       text        NOT NULL REFERENCES ecosystem.repos (repo_id),
  path          text        NOT NULL CHECK (length(path) <= 1024),
  line          integer     NOT NULL CHECK (line >= 1),
  symbol        text        CHECK (length(symbol) <= 256),
  matched_query text        NOT NULL CHECK (length(matched_query) <= 1024),     -- the Zoekt query that found it
  indexed_sha   text        NOT NULL CHECK (indexed_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  tenant_id     text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  found_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (element_key, repo_id, path, line)
);
CREATE INDEX call_sites_contract ON ecosystem.call_sites (contract_ref);

-- Recent incidents, only if an incident source is configured (otherwise empty).
CREATE TABLE ecosystem.incidents (
  incident_ref  text        PRIMARY KEY CHECK (length(incident_ref) <= 128),
  service_ref   text        NOT NULL REFERENCES ecosystem.services (service_ref),
  tenant_id     text        NOT NULL CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(tenant_id) BETWEEN 3 AND 32),
  severity      text        NOT NULL CHECK (severity IN ('sev1', 'sev2', 'sev3', 'sev4')),
  title         text        NOT NULL CHECK (length(title) <= 300),           -- untrusted
  started_at    timestamptz NOT NULL,
  resolved_at   timestamptz,
  url           text        CHECK (url ~ '^https://' AND length(url) <= 2048)
);
CREATE INDEX incidents_service ON ecosystem.incidents (service_ref, started_at DESC);

-- =====================================================================================
-- Blast radius: transitive consumers of a service within N hops (used by briefings).
-- =====================================================================================
CREATE OR REPLACE FUNCTION ecosystem.consumers_of(p_service text, p_max_depth integer DEFAULT 3)
  RETURNS TABLE (service_ref text, depth integer, path text[], min_confidence numeric, calls_per_min numeric)
  LANGUAGE sql STABLE AS $$
  WITH RECURSIVE walk AS (
    SELECT e.from_service AS service_ref, 1 AS depth, ARRAY[p_service, e.from_service] AS path,
           e.confidence AS min_confidence, e.calls_per_min
      FROM ecosystem.edges e
     WHERE e.to_service = p_service
       AND e.edge_kind IN ('consumes_api', 'calls_runtime', 'subscribes_to', 'client_stub_usage', 'depends_on')
    UNION ALL
    SELECT e.from_service, w.depth + 1, w.path || e.from_service, least(w.min_confidence, e.confidence), e.calls_per_min
      FROM walk w JOIN ecosystem.edges e ON e.to_service = w.service_ref
     WHERE w.depth < p_max_depth AND NOT e.from_service = ANY (w.path)
       AND e.edge_kind IN ('consumes_api', 'calls_runtime', 'subscribes_to', 'client_stub_usage', 'depends_on')
  )
  SELECT DISTINCT ON (service_ref) service_ref, depth, path, min_confidence, calls_per_min
    FROM walk ORDER BY service_ref, depth, min_confidence DESC
$$;

-- =====================================================================================
-- RLS
-- =====================================================================================
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['repos', 'services', 'service_repos', 'owner_rules', 'contracts', 'contract_elements', 'edges', 'incidents', 'deployments']
  LOOP
    EXECUTE format('ALTER TABLE ecosystem.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE ecosystem.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY %I ON ecosystem.%I FOR SELECT USING (true)', t || '_read_org', t);
    EXECUTE format('CREATE POLICY %I ON ecosystem.%I FOR ALL TO svc_ecosystem_indexer USING (true) WITH CHECK (true)', t || '_writer', t);
    EXECUTE format('CREATE POLICY %I ON ecosystem.%I FOR ALL TO r_cross_tenant_write USING (ops.principal_scope() = ''system:all-tenants'') WITH CHECK (ops.principal_scope() = ''system:all-tenants'')', t || '_xt_write', t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['contract_versions', 'call_sites']
  LOOP
    EXECUTE format('ALTER TABLE ecosystem.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE ecosystem.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY %I ON ecosystem.%I FOR SELECT USING (ecosystem.repo_visible(repo_id))', t || '_read_visible', t);
    EXECUTE format('CREATE POLICY %I ON ecosystem.%I FOR ALL TO svc_ecosystem_indexer USING (true) WITH CHECK (true)', t || '_writer', t);
    EXECUTE format('CREATE POLICY %I ON ecosystem.%I FOR ALL TO r_cross_tenant_write USING (ops.principal_scope() = ''system:all-tenants'') WITH CHECK (ops.principal_scope() = ''system:all-tenants'')', t || '_xt_write', t);
  END LOOP;
END $$;

-- =====================================================================================
-- Grants
-- =====================================================================================
GRANT USAGE ON SCHEMA ecosystem TO svc_ecosystem_indexer, svc_context_mcp, svc_replicant_worker, svc_memory_service, grafana_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ecosystem TO svc_ecosystem_indexer;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA ecosystem TO svc_ecosystem_indexer;
GRANT SELECT ON ALL TABLES IN SCHEMA ecosystem TO svc_context_mcp, svc_replicant_worker, svc_memory_service;
-- grafana_ro reads only aggregate views (R1-107, C-PG rule 6 "approved views"; CCR-04-07 adds the
-- reader row). No repository content, no service-level rows.
CREATE VIEW ecosystem.v_source_freshness AS
  SELECT s.source_id, s.kind, s.enabled, s.max_staleness, s.last_success_at,
         (s.last_success_at IS NULL OR now() - s.last_success_at > s.max_staleness) AS stale,
         (SELECT count(*) FROM ecosystem.source_runs r WHERE r.source_id = s.source_id AND r.status = 'failed'
             AND r.started_at > now() - interval '24 hours') AS failed_runs_24h
    FROM ecosystem.sources s;
CREATE VIEW ecosystem.v_graph_stats AS
  SELECT (SELECT max(graph_seq) FROM ecosystem.graph_versions) AS graph_seq,
         (SELECT count(*) FROM ecosystem.repos WHERE NOT archived) AS repos,
         (SELECT count(*) FROM ecosystem.services WHERE removed_at IS NULL) AS services,
         (SELECT count(*) FROM ecosystem.edges) AS edges,
         (SELECT count(*) FROM ecosystem.contracts WHERE removed_at IS NULL) AS contracts,
         (SELECT count(*) FROM ecosystem.repos WHERE zoekt_indexed_sha IS DISTINCT FROM indexed_sha) AS zoekt_lagging_repos;
GRANT SELECT ON ecosystem.v_source_freshness, ecosystem.v_graph_stats TO grafana_ro;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA ecosystem TO svc_ecosystem_indexer, svc_context_mcp, svc_replicant_worker, svc_memory_service;
