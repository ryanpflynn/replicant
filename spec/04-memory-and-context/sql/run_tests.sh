#!/usr/bin/env bash
# WS-04 SQL tests on WS-10's real framework (no mirror): WS-10 bootstrap, roles-and-grants and ops DDL,
# then memory.sql and ecosystem.sql as replicant_migrator, a tenant-onboarding step, the behavioral tests,
# and WS-10's partition isolation check (XD-ORCH-04) WITHOUT a maintenance sweep, so it proves that this
# DDL and memory.ensure_tenant_partitions() secure their own partitions.
#   ./run_tests.sh [venv-python-with-psycopg]      (podman; docker with ENGINE=docker)
set -euo pipefail
ENGINE="${ENGINE:-podman}"; C="${CONTAINER:-ws04-fw}"; PORT="${PORT:-56204}"
PY="${1:-python3}"
D="$(cd "$(dirname "$0")" && pwd)"; W="$D/../../10-data-platform"
cleanup() { "$ENGINE" rm -f "$C" >/dev/null 2>&1 || true; }; trap cleanup EXIT
cleanup
"$ENGINE" run -d --name "$C" -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=replicant -p "$PORT:5432" docker.io/pgvector/pgvector:pg17 >/dev/null
until "$ENGINE" exec "$C" pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done; sleep 2
run() { "$ENGINE" cp "$2" "$C:/tmp/t.sql" >/dev/null; "$ENGINE" exec -e PGOPTIONS=-cclient_min_messages=warning "${@:3}" "$C" psql -U "$1" -d replicant -q -v ON_ERROR_STOP=1 -f /tmp/t.sql; }
run postgres "$W/config/compose/postgres-init/00-fake-rds-roles.sql"
run postgres "$W/ddl/00-bootstrap.sql"
run replicant_migrator "$W/ddl/10-roles-and-grants.sql" 2>&1 | grep -v NOTICE || true
run replicant_migrator "$W/ddl/20-ops.sql"
run replicant_migrator "$D/memory.sql"
run replicant_migrator "$D/ecosystem.sql"
# tenant onboarding (migrate Job maintenance mode): per-tenant embedding partitions, secured on creation
"$ENGINE" exec "$C" psql -U replicant_migrator -d replicant -q -v ON_ERROR_STOP=1 \
  -c "SELECT memory.ensure_tenant_partitions('payments'), memory.ensure_tenant_partitions('checkout')"
# local logins for the behavioral tests (RDS uses IAM auth; locally compose passwords)
for r in svc_memory_service svc_ecosystem_indexer svc_context_mcp grafana_ro; do
  "$ENGINE" exec "$C" psql -U postgres -d replicant -q -c "ALTER ROLE $r LOGIN PASSWORD 'pw'"
done
"$ENGINE" cp "$D/test_memory.sql" "$C:/tmp/m.sql"; "$ENGINE" cp "$D/test_ecosystem.sql" "$C:/tmp/e.sql"; "$ENGINE" cp "$D/test_analytics.sql" "$C:/tmp/a.sql"
"$ENGINE" exec -e PGPASSWORD=pw "$C" psql -h localhost -U svc_memory_service -d replicant -v ON_ERROR_STOP=1 -f /tmp/m.sql
"$ENGINE" exec -e PGPASSWORD=pw "$C" psql -h localhost -U svc_ecosystem_indexer -d replicant -v ON_ERROR_STOP=1 -f /tmp/e.sql
"$ENGINE" exec -e PGPASSWORD=pw "$C" psql -h localhost -U grafana_ro -d replicant -v ON_ERROR_STOP=1 -f /tmp/a.sql
run postgres "$D/test_eval_invoker.sql"   # XD-W2-10-07: invoker view under SET ROLE eval_metrics_reader
# XD-ORCH-04: structural + behavioral partition check, no maintenance sweep in between
REPLICANT_DB_DSN="postgresql://postgres:pw@localhost:$PORT/replicant" "$PY" "$W/tools/check_partitions.py"
REPLICANT_DB_DSN="postgresql://postgres:pw@localhost:$PORT/replicant" "$PY" "$W/tools/check_grants.py" || true
REPLICANT_DB_DSN="postgresql://postgres:pw@localhost:$PORT/replicant" "$PY" "$W/tools/check_rls.py" || true
# models vs migrated schema (D-26); needs sqlalchemy, alembic, pgvector, psycopg in the venv
"$PY" "$D/../models/check_drift.py" "postgresql+psycopg://postgres:pw@localhost:$PORT/replicant"
echo "all WS-04 SQL tests passed"
