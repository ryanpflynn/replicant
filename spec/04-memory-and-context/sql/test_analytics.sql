-- Run as grafana_ro (role default replicant.principal_scope = 'operator:all-tenants', WS-10) after
-- test_memory.sql. Views aggregate across tenants through memory_analytics_owner; base tables are denied.
\set QUIET on
DO $$ BEGIN
  IF (SELECT count(*) FROM memory.v_analytics_entry_counts) < 1 THEN RAISE EXCEPTION 'analytics view empty for grafana_ro'; END IF;
  BEGIN
    PERFORM count(*) FROM memory.entries;
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok grafana_ro aggregates only; base table denied'; RETURN; END;
  RAISE EXCEPTION 'grafana_ro could read memory.entries';
END $$;
\echo 'test_analytics.sql: all assertions passed'
