-- XD-W2-10-07: memory.v_job_outcomes is security_invoker. Run as postgres; every assertion runs under
-- SET ROLE, so RLS (FORCE) and column privileges apply to the role named.
\set QUIET on
SET ROLE svc_memory_service;
SELECT set_config('replicant.tenant_id', 'payments', false);
INSERT INTO memory.job_outcomes (job_id, tenant_id, repo, workflow_type, release_bundle_id, terminal_status, pr_merged, merged_at)
  VALUES ('job_01m5p7x7r0bt69j0h0fbxepwa0', 'payments', 'payments-api', 'bugfix', 'rb_01m5p7x7r0bt69j0h0fbxepwa0', 'succeeded', true, now());
INSERT INTO memory.job_outcome_reports (tenant_id, job_id, kind, source, reporter_principal_id, note, dedupe_key)
  VALUES ('payments', 'job_01m5p7x7r0bt69j0h0fbxepwa0', 'reverted', 'revert_detector', 'svc_revert-detector', 'free text', 'revert:t:1');
SELECT set_config('replicant.tenant_id', 'checkout', false);
INSERT INTO memory.job_outcomes (job_id, tenant_id, repo, workflow_type, release_bundle_id, terminal_status, pr_merged, merged_at)
  VALUES ('job_01m5p7x7r0bt69j0h0fbxepwa1', 'checkout', 'checkout-web', 'feature', 'rb_01m5p7x7r0bt69j0h0fbxepwa0', 'succeeded', true, now());
RESET ROLE;
SELECT set_config('replicant.tenant_id', '', false);

SET ROLE eval_metrics_reader;
DO $$
DECLARE n int; k text;
BEGIN
  PERFORM set_config('replicant.principal_scope', 'system:all-tenants', true);
  SELECT count(DISTINCT tenant_id) INTO n FROM memory.v_job_outcomes
   WHERE job_id IN ('job_01m5p7x7r0bt69j0h0fbxepwa0', 'job_01m5p7x7r0bt69j0h0fbxepwa1');
  IF n <> 2 THEN RAISE EXCEPTION 'system:all-tenants read saw % tenants, want 2', n; END IF;
  SELECT report_kind INTO k FROM memory.v_job_outcomes WHERE job_id = 'job_01m5p7x7r0bt69j0h0fbxepwa0';
  IF k IS DISTINCT FROM 'reverted' THEN RAISE EXCEPTION 'report_kind %', k; END IF;
  RAISE NOTICE 'ok eval_metrics_reader reads both tenants under system:all-tenants';
  PERFORM set_config('replicant.principal_scope', 'operator:all-tenants', true);
  SELECT count(*) INTO n FROM memory.v_job_outcomes;
  IF n <> 0 THEN RAISE EXCEPTION 'operator scope must not admit eval_metrics_reader (saw % rows)', n; END IF;
  PERFORM set_config('replicant.principal_scope', 'tenant', true);
  SELECT count(*) INTO n FROM memory.v_job_outcomes;
  IF n <> 0 THEN RAISE EXCEPTION 'tenant scope without tenant saw % rows', n; END IF;
  RAISE NOTICE 'ok no other scope widens the read';
END $$;
DO $$ BEGIN
  PERFORM set_config('replicant.principal_scope', 'system:all-tenants', true);
  BEGIN PERFORM note FROM memory.job_outcome_reports;
  EXCEPTION WHEN insufficient_privilege THEN
    BEGIN PERFORM repo FROM memory.job_outcomes;
    EXCEPTION WHEN insufficient_privilege THEN
      BEGIN PERFORM * FROM memory.v_analytics_outcomes;
      EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok free-text columns and other views denied'; RETURN; END;
    END;
  END;
  RAISE EXCEPTION 'eval_metrics_reader read a column outside its allowlist';
END $$;
DO $$ BEGIN
  PERFORM set_config('replicant.principal_scope', 'system:all-tenants', true);
  INSERT INTO memory.job_outcome_reports (tenant_id, job_id, kind, source, reporter_principal_id, dedupe_key)
    VALUES ('payments', 'job_01m5p7x7r0bt69j0h0fbxepwa0', 'regression', 'ui', 'svc_x-y', 'x');
  RAISE EXCEPTION 'eval_metrics_reader could write';
EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok eval_metrics_reader is read-only';
END $$;
RESET ROLE;
SET ROLE grafana_ro;
DO $$ BEGIN
  PERFORM count(*) FROM memory.v_job_outcomes;
  RAISE EXCEPTION 'grafana_ro could read memory.v_job_outcomes';
EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok grafana_ro has no per-job view';
END $$;
RESET ROLE;
\echo 'test_eval_invoker.sql: all assertions passed'
