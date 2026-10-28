#!/bin/sh
# CONTROL component test environment (containers prefixed ctl-): replicant Postgres migrated with PLATFORM's
# `migrate` image, the Temporal CLI dev server with namespace replicant-local and the C-IDS search attributes.
#   tests/smoke/env.sh up | down
# Secrets are generated into $CTL_HOME (never printed).
set -eu
CTL_HOME="${CTL_HOME:-${TMPDIR:-/tmp}/ctl-smoke}"
NET=ctl-net
PG_IMAGE="${PG_IMAGE:-localhost/replicant/postgres:17-pgvector0.8.2}"
MIGRATE_IMAGE="${MIGRATE_IMAGE:-localhost/replicant/migrate:dev}"
TEMPORAL_IMAGE="${TEMPORAL_IMAGE:-docker.io/temporalio/admin-tools:1.32.1}"
ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
ROLES="svc_control_plane_api svc_identity_service svc_github_integration svc_slack_integration svc_replicant_projector
svc_memory_service svc_ecosystem_indexer svc_context_mcp svc_usage_ingestor svc_eval_worker svc_replicant_worker
svc_publisher_worker svc_github_token_broker svc_retention_sweeper grafana_ro"

gen() { [ -s "$1" ] || (umask 077; python3 -c 'import secrets;print(secrets.token_urlsafe(32),end="")' > "$1"); }

up() {
  mkdir -p "$CTL_HOME/secrets"; chmod 700 "$CTL_HOME"
  gen "$CTL_HOME/secrets/pg_replicant_superuser"; gen "$CTL_HOME/secrets/db_replicant_migrator"
  for r in $ROLES; do gen "$CTL_HOME/secrets/db_$r"; done
  podman network exists $NET || podman network create $NET >/dev/null
  if ! podman container exists ctl-pg; then
    podman run -d --name ctl-pg --network $NET -p 127.0.0.1:55432:5432 \
      -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD_FILE=/run/secrets/pg_replicant_superuser -e POSTGRES_DB=replicant \
      -e REPLICANT_MIGRATOR_PASSWORD_FILE=/run/secrets/db_replicant_migrator \
      -v "$CTL_HOME/secrets:/run/secrets:ro" \
      -v "$ROOT/libs/python/replicant-db/bootstrap/00-fake-rds-roles.sql:/docker-entrypoint-initdb.d/00-fake-rds-roles.sql:ro" \
      -v "$ROOT/libs/python/replicant-db/bootstrap/00-bootstrap.sql:/docker-entrypoint-initdb.d/01-bootstrap.sql:ro" \
      -v "$ROOT/dev/compose/config/postgres-replicant/02-migrator-password.sh:/docker-entrypoint-initdb.d/02-migrator-password.sh:ro" \
      "$PG_IMAGE" postgres -c shared_preload_libraries=pg_stat_statements,pgaudit -c max_connections=200 >/dev/null
  fi
  i=0; until podman exec ctl-pg pg_isready -U postgres -d replicant >/dev/null 2>&1; do i=$((i+1)); [ $i -gt 60 ] && exit 1; sleep 1; done
  sleep 2
  podman run --rm --name ctl-migrate --network $NET -e REPLICANT_ENV=local -e REPLICANT_DB_HOST=ctl-pg -e REPLICANT_DB_NAME=replicant \
    -e REPLICANT_DB_USER=replicant_migrator -e REPLICANT_DB_PASSWORD_FILE=/run/secrets/db_replicant_migrator \
    -e REPLICANT_DB_ROLE_PASSWORDS_DIR=/run/secrets -e REPLICANT_MIGRATE_LOCK_TIMEOUT_SECONDS=120 \
    -v "$CTL_HOME/secrets:/run/secrets:ro" "$MIGRATE_IMAGE" \
    replicant-db migrate --to head --advisory-lock --verify-drift --ensure-roles
  if ! podman container exists ctl-temporal; then
    podman run -d --name ctl-temporal --network $NET -p 127.0.0.1:57233:7233 --entrypoint temporal "$TEMPORAL_IMAGE" \
      server start-dev --ip=0.0.0.0 --port=7233 --headless --namespace=replicant-local --log-level=warn \
      --dynamic-config-value=system.enableDeploymentVersions=true \
      --dynamic-config-value=frontend.workerVersioningWorkflowAPIs=true \
      --dynamic-config-value=frontend.enableUpdateWorkflowExecution=true \
      --search-attribute ReplicantJobId=Keyword --search-attribute ReplicantTenantId=Keyword \
      --search-attribute ReplicantWorkflowType=Keyword --search-attribute ReplicantStatus=Keyword \
      --search-attribute ReplicantPhase=Keyword --search-attribute ReplicantReleaseBundleId=Keyword \
      --search-attribute ReplicantParentJobId=Keyword --search-attribute ReplicantRiskLevel=Keyword \
      --search-attribute ReplicantRepos=KeywordList --search-attribute ReplicantOpenGateIds=KeywordList \
      --search-attribute ReplicantRepairCycle=Int --search-attribute ReplicantProjectionSeq=Int \
      --search-attribute ReplicantWaitingSince=Datetime --search-attribute ReplicantAwaitingHuman=Bool >/dev/null
  fi
  echo "ctl env up: postgres 127.0.0.1:55432, temporal 127.0.0.1:57233, secrets in $CTL_HOME/secrets"
}

down() {
  podman rm -f -v ctl-pg ctl-temporal ctl-migrate >/dev/null 2>&1 || true
  podman ps -a --format '{{.Names}}' | grep '^ctl-' | xargs -r podman rm -f -v >/dev/null 2>&1 || true
  podman network rm $NET >/dev/null 2>&1 || true
  echo "ctl env down"
}

"${1:-up}"
