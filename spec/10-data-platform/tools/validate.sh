#!/usr/bin/env bash
# WS-10 validation suite (schema-management.md §12). Requires podman, a Python venv with
# sqlalchemy[asyncio] alembic psycopg[binary] pgvector pyyaml jsonschema botocore squawk-cli parliament,
# and the image pgvector/pgvector:0.8.2-pg17-bookworm (the version Aurora runs; WS-11 builds its local image FROM it). Usage: tools/validate.sh <venv-python>
set -euo pipefail
PY=${1:?venv python}; BIN=$(dirname "$PY"); HERE=$(cd "$(dirname "$0")/.." && pwd)
# Unique container, port and temp dir per run: several workstreams run this suite concurrently.
C=${VALIDATE_CONTAINER:-ws10-validate-$$}; PORT=${VALIDATE_PORT:-$(python3 -c "import socket;s=socket.socket();s.bind(('',0));print(s.getsockname()[1])")}
T=$(mktemp -d); export TMPDIR_WS10=$T
URL="postgresql://replicant_migrator:mig@localhost:$PORT/replicant"
cleanup() { podman rm -f $C >/dev/null 2>&1 || true; rm -rf "$T"; }
# KEEP=1 leaves the container running for inspection (port $PORT, postgres/pw).
if [ "${KEEP:-0}" = 1 ]; then echo "container $C on port $PORT (kept)"; else trap cleanup EXIT; fi
podman rm -f $C >/dev/null 2>&1 || true; podman run -d --name $C -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=replicant -p $PORT:5432 docker.io/pgvector/pgvector:0.8.2-pg17-bookworm >/dev/null   # prod pgvector version (XD-FIX-R1-260)
until podman exec $C pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done; sleep 2
psqlf() { podman cp "$2" $C:/tmp/v.sql >/dev/null; podman exec $C psql -U "$1" -d "${3:-replicant}" -q -v ON_ERROR_STOP=1 -f /tmp/v.sql >/dev/null; }
echo "== bootstrap";   psqlf postgres "$HERE/config/compose/postgres-init/00-fake-rds-roles.sql"; psqlf postgres "$HERE/ddl/00-bootstrap.sql"
podman exec $C psql -U postgres -q -c "ALTER ROLE replicant_migrator PASSWORD 'mig'"
export REPLICANT_DB_DSN=$URL REPLICANT_IMAGE_DIGEST="sha256:$(printf 'a%.0s' {1..64})" REPLICANT_GIT_SHA="$(printf 'b%.0s' {1..40})" REPLICANT_ENV=ci
echo "== migrate (fresh), rerun (noop)"
(cd "$HERE/code" && PYTHONPATH=. "$PY" -m replicant_db.cli migrate --to head --advisory-lock --verify-drift --skip-contract-gate)
(cd "$HERE/code" && PYTHONPATH=. "$PY" -m replicant_db.cli migrate --to head --advisory-lock --skip-contract-gate)
echo "== downgrade base / upgrade / check"
(cd "$HERE/code" && PYTHONPATH=. "$PY" - <<'P'
from alembic.config import Config; from alembic import command
c = Config(); c.set_main_option("script_location", "replicant_db/migrations")
command.downgrade(c, "base"); command.upgrade(c, "head"); command.check(c); print("round trip + check OK")
P
)
echo "== offline render + squawk"
(cd "$HERE/code" && PYTHONPATH=. "$PY" -c "
from alembic.config import Config; from alembic import command
c=Config(); c.set_main_option('script_location','replicant_db/migrations'); command.upgrade(c,'head',sql=True)" > $T/ws10-render.sql 2>/dev/null)
(cd "$HERE/code" && "$BIN/squawk" -c .squawk.toml --reporter gcc $T/ws10-render.sql) && echo "squawk OK"
echo "== compat self-test";  (cd "$HERE/code" && PYTHONPATH=. "$PY" -m replicant_db.revision)
echo "== migration lint";    "$PY" "$HERE/tools/lint_migrations.py" "$HERE/code/replicant_db/migrations/versions"
echo "== spec DDL equivalence (migrations vs ddl/*.sql)"
podman exec $C psql -U postgres -q -c "CREATE DATABASE replicant_spec"
sed 's/ALTER DATABASE replicant /ALTER DATABASE replicant_spec /; s/ON DATABASE replicant /ON DATABASE replicant_spec /g' "$HERE/ddl/00-bootstrap.sql" > $T/ws10-b.sql
sed 's/ON DATABASE replicant /ON DATABASE replicant_spec /g' "$HERE/ddl/10-roles-and-grants.sql" > $T/ws10-r.sql
psqlf postgres $T/ws10-b.sql replicant_spec; psqlf replicant_migrator $T/ws10-r.sql replicant_spec; psqlf replicant_migrator "$HERE/ddl/20-ops.sql" replicant_spec
"$PY" "$HERE/tools/spec_ddl_equivalence.py" "postgresql://postgres:pw@localhost:$PORT/replicant" "postgresql://postgres:pw@localhost:$PORT/replicant_spec"
echo "== owners' DDL on the WS-10 framework"; "$HERE/tools/apply_owner_ddl.sh" $C replicant
echo "== partition isolation (XD-ORCH-04)"
SU="postgresql://postgres:pw@localhost:$PORT/replicant"
# Seed two tenants' rows into a partitioned tenant table, then reproduce WS-05's finding: a schema-wide grant
# (what WS-10's old default privileges did) reaches every partition, whose RLS the parent does not cover.
podman exec $C psql -U postgres -d replicant -q -v ON_ERROR_STOP=1 -c "
  INSERT INTO qa.test_stats_daily (day_start,tenant_id,test_key,repo_id,test_id,runs,flaky_runs) VALUES
   ('2026-10-24 00:00+00','payments',decode(repeat('ab',32),'hex'),'R_x','go:pkg.TestA',214,11),
   ('2026-10-24 00:00+00','commerce',decode(repeat('cd',32),'hex'),'R_y','pw:e2e/a.spec.ts::x',50,1);
  GRANT SELECT ON ALL TABLES IN SCHEMA qa TO r_qa_read;
  DO \$\$ DECLARE p regclass; BEGIN
    FOR p IN SELECT c.oid FROM pg_class c WHERE c.relispartition AND c.relkind = 'r' AND c.relnamespace = 'qa'::regnamespace LOOP
      EXECUTE format('ALTER TABLE %s NO FORCE ROW LEVEL SECURITY', p);
      EXECUTE format('ALTER TABLE %s DISABLE ROW LEVEL SECURITY', p);
    END LOOP; END \$\$;"
if REPLICANT_DB_DSN=$SU "$PY" "$HERE/tools/check_partitions.py" > $T/ws10-part-neg.txt; then
  echo "negative control FAILED: the check did not detect direct partition reads"; exit 1
else
  echo "negative control: $(grep -c '^LEAK' $T/ws10-part-neg.txt) leaks detected, as expected"
fi
(cd "$HERE/code" && PYTHONPATH=. "$PY" -m replicant_db.maintenance 2026-10-24 | grep -E '^secured' | cut -c1-200)
REPLICANT_DB_DSN=$SU "$PY" "$HERE/tools/check_partitions.py"
echo "== eval-worker audited cross-tenant reads (CCR-06-15)"
psqlf replicant_migrator "$HERE/ddl/40-eval-metrics-reader.sql"; psqlf replicant_migrator "$HERE/ddl/40-eval-metrics-reader.sql"   # re-runnable
"$HERE/tools/test_eval_reader.sh" $C replicant
echo "== audit-exporter approval-record reads (XD-W2-08-03)"
"$HERE/tools/test_audit_exporter_reads.sh" $C replicant
echo "== grants / RLS checks (fatal with STRICT=1)"
# STRICT=1 (the integration gate) makes grant and RLS findings fatal.
GATE=0
REPLICANT_DB_DSN="postgresql://postgres:pw@localhost:$PORT/replicant" "$PY" "$HERE/tools/check_grants.py" || GATE=1
REPLICANT_DB_DSN="postgresql://postgres:pw@localhost:$PORT/replicant" "$PY" "$HERE/tools/check_rls.py" || GATE=1
if [ "${STRICT:-0}" = 1 ] && [ "$GATE" = 1 ]; then echo "integration gate FAILED (grants or RLS)"; exit 1; fi
echo "== S3 configs";        "$PY" "$HERE/tools/validate_s3_config.py" "$HERE/config/s3/prod"; "$PY" "$HERE/tools/validate_s3_config.py" "$HERE/config/s3/local"
echo "done"
