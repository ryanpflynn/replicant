#!/bin/sh
# Runs once on an empty data directory, after WS-10's 00-fake-rds-roles.sql and 00-bootstrap.sql (mounted as
# 00-/01- from libs/python/replicant-db/bootstrap). It only sets the local password of replicant_migrator (prod: IAM
# auth, no password). Everything else (schemas, service roles, grants, RLS) comes from the `migrate` service.
set -eu
MIGRATOR_PASSWORD="$(cat "${REPLICANT_MIGRATOR_PASSWORD_FILE}")"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v migrator_password="$MIGRATOR_PASSWORD" <<'SQL'
ALTER ROLE replicant_migrator PASSWORD :'migrator_password';
SQL
