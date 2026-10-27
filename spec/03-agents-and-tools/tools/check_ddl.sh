#!/usr/bin/env bash
# Apply WS-03 DDL on top of WS-10's framework (bootstrap, roles and grants, ops helpers) in a disposable PostgreSQL 17
# with pgvector, exactly as WS-10's apply_owner_ddl.sh does (as replicant_migrator), then exercise behaviour:
#   - CHECK constraints (positive and negative rows);
#   - tenant isolation under FORCE RLS for the writer and reader roles (no session tenant => no rows);
#   - global registry definitions visible to every tenant, writable only by the registry module;
#   - a maintenance-style partition secured with ops.secure_partition is not directly readable.
# The authoritative grant / RLS / partition checks are WS-10's tools/validate.sh (check_grants.py, check_rls.py,
# check_partitions.py); this script is the fast WS-03-only loop.
# Usage (repo root): spec/03-agents-and-tools/tools/check_ddl.sh   (needs podman or docker)
set -euo pipefail
RT=$(command -v podman || command -v docker)
DIR=$(cd "$(dirname "$0")/.." && pwd)
W10="$DIR/../10-data-platform"
NAME=ws03-ddl-check-$$
"$RT" run -d --rm --name "$NAME" -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=replicant \
  docker.io/pgvector/pgvector:0.8.2-pg17-bookworm >/dev/null
trap '"$RT" stop "$NAME" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 30); do "$RT" exec "$NAME" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
sleep 2
run() { local u=$1; shift; "$RT" exec -i "$NAME" psql -U "$u" -d replicant -v ON_ERROR_STOP=1 -q -At "$@"; }
runf() { "$RT" cp "$2" "$NAME:/tmp/f.sql" >/dev/null; run "$1" -f /tmp/f.sql >/dev/null 2>&1 || { run "$1" -f /tmp/f.sql; exit 1; }; }
runf postgres "$W10/config/compose/postgres-init/00-fake-rds-roles.sql"
runf postgres "$W10/ddl/00-bootstrap.sql"
runf replicant_migrator "$W10/ddl/10-roles-and-grants.sql"
runf replicant_migrator "$W10/ddl/20-ops.sql"
runf replicant_migrator "$DIR/ddl/registry-definitions.sql"
runf replicant_migrator "$DIR/ddl/cost.sql"
fail() { echo "FAIL: $*"; exit 1; }
expect() {  # expect <role> <sql> <expected output>
  local got; got=$(run postgres -c "SET ROLE $1;" -c "$2" 2>&1 | tail -1) || true
  [ "$got" = "$3" ] || fail "$1: $2 => '$got' (expected '$3')"
}
expect_error() {  # expect_error <role> <sql>
  if run postgres -c "SET ROLE $1;" -c "$2" >/dev/null 2>&1; then fail "$1 accepted: $2"; fi
}
T="SELECT set_config('replicant.tenant_id', 'payments', false);"
U="SELECT set_config('replicant.tenant_id', 'commerce', false);"
EV="INSERT INTO cost.usage_events (event_id, occurred_at, tenant_id, job_id, run_id, role, caller_kind, token_jti,
  gateway_model_id, protocol, status, http_status, cost_source, latency_ms) VALUES"
# Partition for October 2026, created and secured the way WS-10 maintenance does it (XD-FIX-ORCH-04).
run replicant_migrator -c "CREATE TABLE cost.usage_events_2026_10 PARTITION OF cost.usage_events
  FOR VALUES FROM ('2026-10-01 00:00:00+00') TO ('2026-11-01 00:00:00+00');
  SELECT ops.secure_partition('cost.usage_events_2026_10'::regclass);" >/dev/null
# 1. writer inserts under its tenant; a row for another tenant is refused by WITH CHECK
expect svc_usage_ingestor "$T $EV ('gwu_01m5pa3c4d5e6f7g8h9j0k1m2n','2026-10-24T08:10:42Z','payments','job_01m5p7x7r0bt69j0h0fbxepw90',
  'run_gj3h3ep3phjfs9e23v768zrca5','implementer','run','rtk_01m5p7z0a1b2c3d4e5f6g7h8j9','anthropic/claude-sonnet-5-5',
  'anthropic_messages','ok',200,'price_book',14210) RETURNING 'ok';" "ok"
expect_error svc_usage_ingestor "$T $EV ('gwu_01m5pa3c4d5e6f7g8h9j0k1m2p','2026-10-24T08:10:42Z','commerce',NULL,
  'run_gj3h3ep3phjfs9e23v768zrca6','implementer','run','rtk_01m5p7z0a1b2c3d4e5f6g7h8j8','m','anthropic_messages','ok',200,'price_book',1);"
expect svc_usage_ingestor "$U $EV ('gwu_01m5pa3c4d5e6f7g8h9j0k1m2q','2026-10-24T09:00:00Z','commerce',NULL,
  'run_gj3h3ep3phjfs9e23v768zrca7','eval-judge','run','rtk_01m5p7z0a1b2c3d4e5f6g7h8j7','m','anthropic_messages','ok',200,'price_book',1) RETURNING 'ok';" "ok"
# 2. CHECK constraints: run caller without run_id; eval mode without account; unknown role
expect_error svc_usage_ingestor "$T $EV ('gwu_01m5pa3c4d5e6f7g8h9j0k1m2r','2026-10-24T08:10:42Z','payments',NULL,NULL,'implementer','run',NULL,'m','anthropic_messages','ok',200,'price_book',1);"
expect_error svc_usage_ingestor "$T INSERT INTO cost.usage_events (event_id, occurred_at, tenant_id, caller_kind, service_principal,
  gateway_model_id, protocol, status, http_status, cost_source, latency_ms, evaluation_mode) VALUES ('gwu_01m5pa3c4d5e6f7g8h9j0k1m2s',
  '2026-10-24T08:10:42Z','payments','service','svc_eval-worker','m','anthropic_messages','ok',200,'price_book',1,'eval');"
expect svc_usage_ingestor "$T INSERT INTO cost.usage_events (event_id, occurred_at, tenant_id, role, caller_kind, service_principal,
  gateway_model_id, protocol, purpose, status, http_status, cost_source, latency_ms, evaluation_mode, budget_account) VALUES
  ('gwu_01m5pa3c4d5e6f7g8h9j0k1m2t','2026-10-24T08:10:42Z','payments','eval-judge','service','svc_eval-worker','m',
  'anthropic_messages','eval_judge','ok',200,'price_book',1,'eval','fgw:eval:evr_01m5p7x7r0bt69j0h0fbxepw90') RETURNING 'ok';" "ok"
expect_error svc_usage_ingestor "$T $EV ('gwu_01m5pa3c4d5e6f7g8h9j0k1m2u','2026-10-24T08:10:42Z','payments',NULL,'run_gj3h3ep3phjfs9e23v768zrca8','judge','run','rtk_01m5p7z0a1b2c3d4e5f6g7h8j6','m','anthropic_messages','ok',200,'price_book',1);"
# 3. readers see only their tenant; no tenant set => nothing
expect svc_replicant_worker "$T SELECT count(*) FROM cost.usage_events;" "2"
expect svc_replicant_worker "$U SELECT count(*) FROM cost.usage_events;" "1"
expect svc_replicant_worker "SELECT count(*) FROM cost.usage_events;" "0"
expect svc_cpa_read "$T SELECT count(*) FROM cost.usage_events;" "2"
# 4. grafana_ro reads across tenants (scope operator:all-tenants is its role default; set here for SET ROLE)
expect grafana_ro "SELECT set_config('replicant.principal_scope','operator:all-tenants',false); SELECT count(*) FROM cost.usage_events;" "3"
# 5. the partition is not directly readable by any service role (ops.secure_partition revoked everything)
expect_error grafana_ro "SELECT count(*) FROM cost.usage_events_2026_10;"
expect_error svc_usage_ingestor "SELECT count(*) FROM cost.usage_events_2026_10;"
# 6. views: v_daily_spend counts only evaluation_mode = none, per tenant
run postgres -c "SET ROLE svc_usage_ingestor;" -c "$T INSERT INTO cost.tenant_daily_costs (tenant_id, day, gateway_model_id, role, cost_micros, evaluation_mode)
  VALUES ('payments','2026-10-24','m','implementer',1500000,'none'), ('payments','2026-10-24','m','eval-judge',900000,'eval');" >/dev/null
expect svc_cpa_read "$T SELECT spend_usd FROM cost.v_daily_spend;" "1.5000000000000000"
expect svc_cpa_read "$U SELECT count(*) FROM cost.v_daily_spend;" "0"
# 7. registry: a global definition is visible to every tenant, tenant definitions only to their tenant
DOC="'{}'::jsonb, 'registry/blobs/sha256/x.json', '0123456789012345678901234567890123456789', 'agents/x.yaml', 'svc_control-plane-api'"
DV="INSERT INTO registry.definition_versions (kind, scope_level, tenant_id, name, version, digest, api_version, kind_schema_id,
  document, blob_key, source_commit, source_path, published_by) VALUES"
H64=$(printf 'a%.0s' $(seq 1 64))
expect svc_cpa_registry "$DV ('agent','global','','implementer',1,'sha256:$H64','replicant.example.com/v1','k',$DOC) RETURNING 'ok';" "ok"
expect svc_cpa_registry "$T $DV ('agent','tenant','payments','implementer',1,'sha256:$H64','replicant.example.com/v1','k',$DOC) RETURNING 'ok';" "ok"
expect_error svc_cpa_registry "$T $DV ('agent','tenant','commerce','implementer',1,'sha256:$H64','replicant.example.com/v1','k',$DOC);"
expect svc_replicant_worker "$U SELECT count(*) FROM registry.definition_versions;" "1"
expect svc_replicant_worker "$T SELECT count(*) FROM registry.definition_versions;" "2"
expect svc_replicant_worker "SELECT count(*) FROM registry.definition_versions;" "1"
# 8. readers cannot write; the control-plane-api login itself holds no DML (grant model v2)
expect_error svc_replicant_worker "$DV ('agent','global','','other',1,'sha256:$H64','replicant.example.com/v1','k',$DOC);"
expect_error svc_control_plane_api "$DV ('agent','global','','other',1,'sha256:$H64','replicant.example.com/v1','k',$DOC);"
expect_error svc_cpa_registry "DELETE FROM registry.definition_state_events;"
echo "DDL OK (constraints, tenant isolation under FORCE RLS, global definitions, partition isolation, views)"
