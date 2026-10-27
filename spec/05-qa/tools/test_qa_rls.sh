#!/usr/bin/env bash
# Applies spec/05-qa/sql/qa-schema.sql to a disposable PostgreSQL 17 and checks tenant isolation (R1-113),
# security_invoker behavior, grafana aggregate access, and idempotent indexing.
# Prelude: the roles and RLS helpers WS-10 creates (spec/10-data-platform/ddl/10-roles-and-grants.sql, 20-ops.sql);
# the helper functions are copied verbatim from 20-ops.sql so the test exercises the real policy text.
# Usage: bash spec/05-qa/tools/test_qa_rls.sh   (needs podman or docker)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
ENG="$(command -v podman || command -v docker)"
C=ws05-qa-rls
"$ENG" rm -f "$C" >/dev/null 2>&1 || true
"$ENG" run -d --name "$C" -e POSTGRES_PASSWORD=x docker.io/library/postgres:17 >/dev/null
for _ in $(seq 1 40); do "$ENG" exec "$C" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
sleep 2
psql() { "$ENG" exec -i "$C" psql -U postgres -v ON_ERROR_STOP=1 -q -X "$@"; }
{
cat <<'SQL'
CREATE ROLE replicant_migrator LOGIN CREATEROLE NOINHERIT;
CREATE ROLE r_cross_tenant_read NOLOGIN; CREATE ROLE r_cross_tenant_write NOLOGIN;
CREATE ROLE r_qa_read NOLOGIN; CREATE ROLE r_qa_write NOLOGIN; CREATE ROLE qa_analytics_owner NOLOGIN;
CREATE ROLE grafana_ro LOGIN; CREATE ROLE svc_replicant_projector LOGIN; CREATE ROLE svc_replicant_worker LOGIN; CREATE ROLE svc_control_plane_api LOGIN;
GRANT r_qa_write TO svc_replicant_projector; GRANT r_qa_read TO svc_replicant_worker, svc_control_plane_api;
GRANT r_cross_tenant_read TO svc_control_plane_api, qa_analytics_owner;
GRANT r_cross_tenant_read, r_cross_tenant_write, qa_analytics_owner TO replicant_migrator WITH INHERIT TRUE;
ALTER ROLE grafana_ro SET replicant.principal_scope = 'operator:all-tenants';
GRANT CREATE ON DATABASE postgres TO replicant_migrator;
CREATE SCHEMA ops AUTHORIZATION replicant_migrator;
GRANT USAGE ON SCHEMA ops TO PUBLIC;
SET ROLE replicant_migrator;
CREATE TABLE ops.partitioned_tables (table_name text PRIMARY KEY, key_kind text, key_column text, ulid_prefix text,
  interval_months smallint, premake smallint, retain_months smallint, owner_role text, detach_before_drop boolean, export_before_drop boolean);
SQL
sed -n '/^CREATE OR REPLACE FUNCTION ops.current_tenant/,/^REVOKE ALL ON FUNCTION ops.secure_partition/p' "$ROOT/spec/10-data-platform/ddl/20-ops.sql"
cat "$ROOT/spec/05-qa/sql/qa-schema.sql"
cat <<'SQL'
GRANT SELECT ON ALL TABLES IN SCHEMA qa TO r_qa_read;  -- what WS-10 default privileges give new partitions
RESET ROLE;
SQL
} | psql
psql <<'SQL'
-- projector indexes two tenants' runs (tenant set per transaction, as replicant-db does)
SET ROLE svc_replicant_projector;
BEGIN; SET LOCAL replicant.tenant_id = 'payments';
INSERT INTO qa.indexed_runs VALUES ('2026-10-24 08:41:02+00','run_chxtsxqzcpds1fc1c4dvw9e4em','payments','job_01m5p7x7r0bt69j0h0fbxepw90','k',repeat('a',64),2);
INSERT INTO qa.test_stats_daily (day_start,tenant_id,test_key,repo_id,test_id,runs,flaky_runs) VALUES ('2026-10-24 00:00+00','payments',decode(repeat('ab',32),'hex'),'R_x','go:pkg.TestA',214,11);
COMMIT;
BEGIN; SET LOCAL replicant.tenant_id = 'commerce';
INSERT INTO qa.test_stats_daily (day_start,tenant_id,test_key,repo_id,test_id,runs,flaky_runs) VALUES ('2026-10-24 00:00+00','commerce',decode(repeat('cd',32),'hex'),'R_y','pw:e2e/a.spec.ts::x',50,1);
COMMIT;
-- idempotent re-index: conflict does nothing
BEGIN; SET LOCAL replicant.tenant_id = 'payments';
INSERT INTO qa.indexed_runs VALUES ('2026-10-24 08:41:02+00','run_chxtsxqzcpds1fc1c4dvw9e4em','payments','job_01m5p7x7r0bt69j0h0fbxepw90','k',repeat('a',64),2) ON CONFLICT DO NOTHING;
COMMIT;
RESET ROLE;
SQL
check() { local who="$1" sql="$2" want="$3"; got=$(psql -At -c "SET ROLE $who; $sql" | tail -1); [ "$got" = "$want" ] && echo "ok    $who: $sql -> $got" || { echo "FAIL  $who: $sql -> $got (want $want)"; FAILED=1; }; }
FAILED=0
check svc_replicant_worker "SET replicant.tenant_id='payments'; SELECT count(*) FROM qa.test_stats_30d" 1
check svc_replicant_worker "SET replicant.tenant_id='commerce'; SELECT count(*) FROM qa.test_stats_30d" 1
check svc_replicant_worker "SELECT count(*) FROM qa.test_stats_30d" 0
check svc_replicant_worker "SET replicant.principal_scope='operator:all-tenants'; SELECT count(*) FROM qa.test_stats_30d" 0
check svc_control_plane_api "SET replicant.principal_scope='operator:all-tenants'; SELECT count(*) FROM qa.test_stats_30d" 2
check svc_control_plane_api "SET replicant.principal_scope='operator'; SELECT count(*) FROM qa.test_stats_30d" 0
check replicant_migrator "SELECT count(*) FROM qa.test_stats_daily" 0
check grafana_ro "SET replicant.principal_scope='operator:all-tenants'; SELECT count(*) FROM qa.flake_summary_daily" 2   # role-default scope applies at login; simulated here
check grafana_ro "SELECT count(*) FROM qa.flake_summary_daily" 0
check grafana_ro "SELECT has_table_privilege('qa.test_stats_daily','SELECT')::text" false
check svc_replicant_projector "SET replicant.tenant_id='payments'; SELECT has_table_privilege('qa.test_results','DELETE')::text" false
check svc_replicant_projector "SET replicant.tenant_id='payments'; SELECT count(*) FROM qa.indexed_runs" 1
check svc_replicant_worker "SELECT count(*) FROM qa.test_stats_daily_2026_10" 0   # direct partition read without tenant
check svc_control_plane_api "SET replicant.tenant_id='payments'; SELECT count(*) FROM qa.test_stats_daily_2026_10" 1
check postgres "SELECT count(*) FROM pg_class WHERE relnamespace='qa'::regnamespace AND relkind='r' AND NOT relforcerowsecurity" 0
check postgres "SELECT string_agg(relname||':'||relforcerowsecurity::text, ',' ORDER BY relname) FROM pg_class WHERE relnamespace='qa'::regnamespace AND relkind='p'" "indexed_runs:true,test_results:true,test_stats_daily:true"
check postgres "SELECT array_to_string(reloptions, ',') FROM pg_class WHERE oid='qa.test_stats_30d'::regclass" "security_invoker=true,security_barrier=true"
"$ENG" rm -f "$C" >/dev/null
[ "$FAILED" = 0 ] && echo "all qa RLS checks passed" || { echo "qa RLS checks FAILED"; exit 1; }
