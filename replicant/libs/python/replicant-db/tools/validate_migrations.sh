#!/usr/bin/env bash
# Proves the replicant-db Alembic history applies all 17 owner DDL files and equals the reviewed spec DDL.
# Same approach as spec/10-data-platform/tools/validate.sh (WS-10), run against THIS package's migrations:
#   1. bootstrap (fake RDS roles + 00-bootstrap.sql) as the superuser, exactly as postgres-replicant's init does
#   2. `replicant-db migrate --verify-drift --ensure-roles` fresh, then again (no-op)
#   3. downgrade base -> upgrade head -> alembic check (round trip)
#   4. migration lint, revision compat self-test
#   5. reference DB built from the spec files with psql (spec's apply_owner_ddl.sh) -> catalog diff over all 17 schemas
#   6. grants, RLS, partition isolation, eval-reader and audit-exporter probes (fatal with STRICT=1)
# Usage: tools/validate_migrations.sh   (needs podman and uv; the spec checkout at $SPEC, default ../../../../spec)
set -euo pipefail
ENGINE="${REPLICANT_CONTAINER_CLI:-podman}"
HERE=$(cd "$(dirname "$0")/.." && pwd); ROOT=$(cd "$HERE/../../.." && pwd)
SPEC=${SPEC:-$(cd "$ROOT/../spec" && pwd)}
IMG=${VALIDATE_IMAGE:-docker.io/pgvector/pgvector:0.8.2-pg17-bookworm}
C=${VALIDATE_CONTAINER:-plat-validate-$$}
PORT=${VALIDATE_PORT:-$(python3 -c "import socket;s=socket.socket();s.bind(('',0));print(s.getsockname()[1])")}
T=$(mktemp -d)
PY=(uv run --project "$ROOT" --package replicant-db python)
cleanup() { "$ENGINE" rm -f -v "$C" >/dev/null 2>&1 || true; rm -rf "$T"; }
if [ "${KEEP:-0}" = 1 ]; then echo "container $C on port $PORT (kept)"; else trap cleanup EXIT; fi
MIGPW=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
"$ENGINE" rm -f -v "$C" >/dev/null 2>&1 || true
"$ENGINE" run -d --name "$C" -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=replicant -p "127.0.0.1:$PORT:5432" "$IMG" >/dev/null
until "$ENGINE" exec "$C" pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done; sleep 2
psqlf() { "$ENGINE" cp "$2" "$C:/tmp/v.sql" >/dev/null; "$ENGINE" exec -e PGOPTIONS="-c client_min_messages=warning" "$C" psql -U "$1" -d "${3:-replicant}" -q -v ON_ERROR_STOP=1 -f /tmp/v.sql >/dev/null; }
echo "== bootstrap (package bootstrap/ files, as postgres-replicant init runs them)"
psqlf postgres "$HERE/bootstrap/00-fake-rds-roles.sql"; psqlf postgres "$HERE/bootstrap/00-bootstrap.sql"
"$ENGINE" exec "$C" psql -U postgres -q -c "ALTER ROLE replicant_migrator PASSWORD '$MIGPW'"
# Role password files for --ensure-roles (local/ci only)
mkdir -p "$T/pw"; for r in svc_control_plane_api svc_replicant_worker grafana_ro; do python3 -c "import secrets;print(secrets.token_urlsafe(24))" > "$T/pw/db_$r"; done
printf '%s' "$MIGPW" > "$T/pw/db_replicant_migrator"
export REPLICANT_ENV=ci REPLICANT_DB_HOST=127.0.0.1 REPLICANT_DB_PORT=$PORT REPLICANT_DB_NAME=replicant REPLICANT_DB_USER=replicant_migrator \
       REPLICANT_DB_PASSWORD_FILE="$T/pw/db_replicant_migrator" REPLICANT_DB_ROLE_PASSWORDS_DIR="$T/pw"
echo "== migrate (fresh), rerun (noop)"
"${PY[@]}" -m replicant_db.cli migrate --to head --advisory-lock --verify-drift --ensure-roles --skip-contract-gate
"${PY[@]}" -m replicant_db.cli migrate --to head --advisory-lock --skip-contract-gate
REV=$("$ENGINE" exec "$C" psql -U postgres -d replicant -qAtX -c "SELECT version_num FROM ops.alembic_version")
NLOG=$("$ENGINE" exec "$C" psql -U postgres -d replicant -qAtX -c "SELECT count(*) FROM ops.migration_log WHERE downgraded_at IS NULL")
echo "head=$REV revisions_applied=$NLOG"
"$ENGINE" exec "$C" psql -U postgres -d replicant -qAtX -c "SELECT revision||' '||schema_name||' '||compat FROM ops.migration_log ORDER BY revision"
echo "== login with an --ensure-roles password"
"$ENGINE" exec -e PGPASSWORD="$(cat "$T/pw/db_svc_replicant_worker")" "$C" psql -h 127.0.0.1 -U svc_replicant_worker -d replicant -qAtX -c "SELECT 'svc_replicant_worker login ok'"
echo "== downgrade base / upgrade / check"
(cd "$HERE" && "${PY[@]}" - <<'P'
from alembic.config import Config; from alembic import command
c = Config(); c.set_main_option("script_location", "replicant_db/migrations")
command.downgrade(c, "base"); command.upgrade(c, "head"); command.check(c); print("round trip + check OK")
P
)
echo "== migration lint + revision self-test"
"${PY[@]}" "$HERE/tools/lint_migrations.py" "$HERE/replicant_db/migrations/versions"
"${PY[@]}" -m replicant_db.revision
echo "== reference DB from the spec files (psql), then catalog diff over all 17 schemas"
"$ENGINE" exec "$C" psql -U postgres -q -c "CREATE DATABASE replicant_spec"
sed 's/ALTER DATABASE replicant /ALTER DATABASE replicant_spec /; s/ON DATABASE replicant /ON DATABASE replicant_spec /g' "$SPEC/10-data-platform/ddl/00-bootstrap.sql" > "$T/b.sql"
sed 's/ON DATABASE replicant /ON DATABASE replicant_spec /g' "$SPEC/10-data-platform/ddl/10-roles-and-grants.sql" > "$T/r.sql"
psqlf postgres "$T/b.sql" replicant_spec; psqlf replicant_migrator "$T/r.sql" replicant_spec; psqlf replicant_migrator "$SPEC/10-data-platform/ddl/20-ops.sql" replicant_spec
"$SPEC/10-data-platform/tools/apply_owner_ddl.sh" "$C" replicant_spec | tee "$T/owner.txt"
psqlf replicant_migrator "$SPEC/10-data-platform/ddl/40-eval-metrics-reader.sql" replicant_spec
[ "$(grep -c ' OK$' "$T/owner.txt")" = 17 ] || { echo "spec reference build failed"; exit 1; }
"${PY[@]}" "$HERE/tools/spec_ddl_equivalence.py" "postgresql://postgres:pw@127.0.0.1:$PORT/replicant" "postgresql://postgres:pw@127.0.0.1:$PORT/replicant_spec"
echo "== maintenance after the round trip (partition_create + partition_secure, as migrate does locally)"
"${PY[@]}" -m replicant_db.cli maintenance run --tasks=partition_create,partition_secure
SU="postgresql://postgres:pw@127.0.0.1:$PORT/replicant"
REPLICANT_DB_DSN=$SU "${PY[@]}" "$HERE/tools/check_partitions.py"
echo "== eval reader + audit-exporter probes"
"$HERE/tools/test_eval_reader.sh" "$C" replicant
"$HERE/tools/test_audit_exporter_reads.sh" "$C" replicant
echo "== grants / RLS"
GATE=0
REPLICANT_DB_DSN=$SU "${PY[@]}" "$HERE/tools/check_grants.py" || GATE=1
REPLICANT_DB_DSN=$SU "${PY[@]}" "$HERE/tools/check_rls.py" || GATE=1
if [ "${STRICT:-0}" = 1 ] && [ "$GATE" = 1 ]; then echo "integration gate FAILED (grants or RLS)"; exit 1; fi
echo "done: all 17 owner schemas migrated, head $REV"
