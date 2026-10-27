#!/bin/sh
# Temporal persistence + SQL visibility schema on PostgreSQL 16 (same plugin as prod: postgres12).
# Idempotent: `create` tolerates an existing database; setup-schema -v 0.0 then update-schema to latest.
# Mirrors temporalio/samples-server compose/scripts/setup-postgres.sh (checked 2026-10-24).
set -eu
: "${POSTGRES_SEEDS:?}" "${POSTGRES_USER:?}" "${SQL_PASSWORD:?}"
export SQL_PASSWORD
until nc -z -w 5 "$POSTGRES_SEEDS" "${DB_PORT:-5432}"; do sleep 1; done
T="temporal-sql-tool --plugin postgres12 --ep $POSTGRES_SEEDS -u $POSTGRES_USER -p ${DB_PORT:-5432}"
for db in temporal temporal_visibility; do
  $T --db "$db" create || echo "database $db exists"
done
if ! $T --db temporal update-schema -d /etc/temporal/schema/postgresql/v12/temporal/versioned 2>/dev/null; then
  $T --db temporal setup-schema -v 0.0
  $T --db temporal update-schema -d /etc/temporal/schema/postgresql/v12/temporal/versioned
fi
if ! $T --db temporal_visibility update-schema -d /etc/temporal/schema/postgresql/v12/visibility/versioned 2>/dev/null; then
  $T --db temporal_visibility setup-schema -v 0.0
  $T --db temporal_visibility update-schema -d /etc/temporal/schema/postgresql/v12/visibility/versioned
fi
echo "temporal schema ready"
