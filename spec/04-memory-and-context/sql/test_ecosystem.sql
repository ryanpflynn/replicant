-- Behavioral test for ecosystem.sql. Writes run as svc_ecosystem_indexer with scope system;
-- the visibility check runs as the same login with scope tenant (simulating context-mcp).
\set QUIET on
SET search_path = ecosystem, public;

BEGIN;
INSERT INTO graph_versions (graph_seq, source_run_ids) VALUES (1, '{}');
INSERT INTO repos (repo_id, tenant_id, repo, default_branch, visibility, primary_languages) VALUES
  ('R_kgDOK2a9Xw', 'payments', 'github.com/acme-payments/ledger-api', 'main', 'private', '{go}'),
  ('R_kgDOK2b1Aa', 'payments', 'github.com/acme-payments/billing-worker', 'main', 'internal', '{go}'),
  ('R_kgDOK3c7Zz', 'checkout', 'github.com/acme-checkout/checkout-web', 'main', 'private', '{typescript}');
INSERT INTO services (service_ref, tenant_id, name, kind, service_group, owner_group, lifecycle, tier, sources, graph_seq) VALUES
  ('component:default/ledger-api', 'payments', 'ledger-api', 'service', 'ledger', 'payments-ledger', 'production', 1, '{backstage}', 1),
  ('component:default/billing-worker', 'payments', 'billing-worker', 'worker', 'ledger', 'payments-billing', 'production', 2, '{backstage}', 1),
  ('component:default/checkout-web', 'checkout', 'checkout-web', 'website', 'storefront', 'checkout-web', 'production', 1, '{backstage}', 1);
INSERT INTO service_repos VALUES
  ('component:default/ledger-api', 'R_kgDOK2a9Xw', '', 'payments', 'backstage'),
  ('component:default/billing-worker', 'R_kgDOK2b1Aa', '', 'payments', 'backstage'),
  ('component:default/checkout-web', 'R_kgDOK3c7Zz', '', 'checkout', 'backstage');
INSERT INTO contracts (contract_ref, kind, repo_id, path, service_ref, tenant_id, current_digest) VALUES
  ('protobuf:R_kgDOK2a9Xw:proto/ledger/v1/ledger.proto', 'protobuf', 'R_kgDOK2a9Xw', 'proto/ledger/v1/ledger.proto',
   'component:default/ledger-api', 'payments', 'sha256:' || encode(sha256('ledger-v1'), 'hex'));
INSERT INTO contract_versions (contract_ref, digest, repo_id, tenant_id, commit_sha, source, extractor, normalized) VALUES
  ('protobuf:R_kgDOK2a9Xw:proto/ledger/v1/ledger.proto', 'sha256:' || encode(sha256('ledger-v1'), 'hex'), 'R_kgDOK2a9Xw',
   'payments', '8c1e4f2a9b7d3c6e5f0a1b2c3d4e5f6a7b8c9d0e', 'repo_scan', 'buf-image@1.75.0',
   '{"package":"ledger.v1","services":[{"name":"LedgerService","methods":["PostEntry","GetBalance"]}]}');
INSERT INTO contract_elements VALUES
  ('protobuf:R_kgDOK2a9Xw:proto/ledger/v1/ledger.proto', 'sha256:' || encode(sha256('ledger-v1'), 'hex'),
   'ledger.v1.LedgerService/PostEntry', 'rpc_method', 'sha256:' || encode(sha256('m1'), 'hex'), 'proto/ledger/v1/ledger.proto', 'payments');
INSERT INTO edges (from_service, to_service, edge_kind, via, source, confidence, calls_per_min, tenant_id, graph_seq) VALUES
  ('component:default/billing-worker', 'component:default/ledger-api', 'calls_runtime', 'ledger.v1.LedgerService/PostEntry',
   'tempo_service_graph', 0.950, 812.4, 'payments', 1),
  ('component:default/checkout-web', 'component:default/billing-worker', 'consumes_api', '', 'backstage', 0.800, NULL, 'checkout', 1);
INSERT INTO call_sites VALUES
  ('ledger.v1.LedgerService/PostEntry', 'protobuf:R_kgDOK2a9Xw:proto/ledger/v1/ledger.proto', 'R_kgDOK2b1Aa',
   'internal/post/poster.go', 88, 'PostEntry', 'sym:PostEntry lang:go', '1111111111111111111111111111111111111111', 'payments', now());
COMMIT;

-- Blast radius across tenants (catalog metadata is org-visible).
SELECT service_ref, depth, path FROM ecosystem.consumers_of('component:default/ledger-api', 3) ORDER BY depth;

-- Reads below run as the reader role (context-mcp); the writer's policy would see everything.
\connect replicant svc_context_mcp localhost
SET search_path = ecosystem, public;
-- Visibility: a job whose requester can read only ledger-api cannot see billing-worker call sites.
BEGIN;
SET LOCAL replicant.principal_scope = 'tenant';
SET LOCAL replicant.visible_repo_ids = 'R_kgDOK2a9Xw';
DO $$ BEGIN
  IF (SELECT count(*) FROM ecosystem.call_sites) <> 0 THEN RAISE EXCEPTION 'call site leak'; END IF;
  IF (SELECT count(*) FROM ecosystem.contract_versions) <> 1 THEN RAISE EXCEPTION 'own contract must be visible'; END IF;
  IF (SELECT count(*) FROM ecosystem.consumers_of('component:default/ledger-api', 3)) <> 2 THEN RAISE EXCEPTION 'blast radius wrong'; END IF;
  RAISE NOTICE 'ok visibility filtering and cross-tenant blast radius';
END $$;
COMMIT;
BEGIN;
SET LOCAL replicant.principal_scope = 'tenant';
DO $$ BEGIN
  BEGIN
    INSERT INTO ecosystem.graph_versions (graph_seq, source_run_ids) VALUES (2, '{}');
    INSERT INTO ecosystem.services (service_ref, tenant_id, name, kind, sources, graph_seq)
    VALUES ('component:default/evil', 'payments', 'evil', 'service', '{fixture}', 2);
  EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'ok (expected failure) non-system write: %', SQLERRM; RETURN; END;
  RAISE EXCEPTION 'non-system write succeeded';
END $$;
COMMIT;
\echo 'test_ecosystem.sql: all assertions passed'
