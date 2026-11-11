#!/usr/bin/env bash
# Start a throwaway Postgres (container wrk-pg-<suffix>) with the spec's WS-10 framework and the read-model DDL the
# projector writes (jobs_read, approvals, audit, intake), all from spec/ verbatim. Prints the host port.
# Usage: pg_fixture.sh up <name> <spec-dir> | pg_fixture.sh down <name>
set -euo pipefail
ENGINE="${REPLICANT_CONTAINER_CLI:-podman}"
cmd=$1; C=$2
if [ "$cmd" = down ]; then "$ENGINE" rm -f -v "$C" >/dev/null 2>&1 || true; exit 0; fi
SPEC=$3
"$ENGINE" rm -f -v "$C" >/dev/null 2>&1 || true
"$ENGINE" run -d --name "$C" --memory 512m -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=replicant -p 127.0.0.1::5432 \
  docker.io/pgvector/pgvector:0.8.2-pg17-bookworm >/dev/null
until "$ENGINE" exec "$C" pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done; sleep 2
psqlf() { "$ENGINE" cp "$2" "$C:/tmp/f.sql" >/dev/null; "$ENGINE" exec "$C" psql -U "$1" -d replicant -q -v ON_ERROR_STOP=1 -f /tmp/f.sql >/dev/null; }
psqlf postgres "$SPEC/10-data-platform/config/compose/postgres-init/00-fake-rds-roles.sql"
psqlf postgres "$SPEC/10-data-platform/ddl/00-bootstrap.sql"
psqlf replicant_migrator "$SPEC/10-data-platform/ddl/10-roles-and-grants.sql"
psqlf replicant_migrator "$SPEC/10-data-platform/ddl/20-ops.sql"
T=$(mktemp); { echo "SET ROLE audit_owner;"; grep -v '^CREATE SCHEMA IF NOT EXISTS audit' "$SPEC/08-observability/sql/audit.sql"; } > "$T"
psqlf replicant_migrator "$T"; rm -f "$T"
for f in 07-control-plane-and-ux/data/ddl/tenancy.sql 07-control-plane-and-ux/data/ddl/intake.sql \
         07-control-plane-and-ux/data/ddl/jobs_read.sql 07-control-plane-and-ux/data/ddl/approvals.sql; do
  psqlf replicant_migrator "$SPEC/$f"
done
# job_timeline is RANGE-partitioned on job_id: one partition over the whole job_ ID space (derived IDs are not
# time-ordered), secured like WS-10 maintenance does (ops.secure_partition); plus a partition for audit.events now.
"$ENGINE" exec "$C" psql -U replicant_migrator -d replicant -q -v ON_ERROR_STOP=1 -c "
  CREATE TABLE jobs_read.job_timeline_all PARTITION OF jobs_read.job_timeline FOR VALUES FROM ('job_') TO ('job_~');
  SELECT ops.secure_partition('jobs_read.job_timeline_all');" >/dev/null
"$ENGINE" exec "$C" psql -U postgres -d replicant -q -v ON_ERROR_STOP=1 -c "
  DO \$\$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent WHERE p.oid = 'audit.events'::regclass) THEN
      EXECUTE 'CREATE TABLE audit.events_test PARTITION OF audit.events FOR VALUES FROM (''2020-01-01'') TO (''2100-01-01'')';
    END IF; END \$\$;
  ALTER ROLE svc_replicant_projector PASSWORD 'proj';" >/dev/null
"$ENGINE" port "$C" 5432/tcp | head -1 | sed 's/.*://'
