#!/usr/bin/env bash
# Apply every owning workstream's reviewed DDL, in dependency order, on top of the WS-10 framework
# (bootstrap + revision 202610240001_ops), as replicant_migrator — exactly the privileges Alembic has.
# Usage: apply_owner_ddl.sh <container> <database>. Prints one line per file: OK or the first error.
set -u
C=${1:-ws10-pg17}; DB=${2:-replicant}; SPEC=$(cd "$(dirname "$0")/../.." && pwd)
FILES=(
  "audit_owner|08-observability/sql/audit.sql"
  "replicant_migrator|07-control-plane-and-ux/data/ddl/tenancy.sql"
  "replicant_migrator|07-control-plane-and-ux/data/ddl/identity.sql"
  "replicant_migrator|03-agents-and-tools/ddl/registry-definitions.sql"
  "replicant_migrator|06-releases-and-evals/sql/registry-releases.sql"
  "replicant_migrator|07-control-plane-and-ux/data/ddl/intake.sql"
  "replicant_migrator|07-control-plane-and-ux/data/ddl/jobs_read.sql"
  "replicant_migrator|07-control-plane-and-ux/data/ddl/approvals.sql"
  "replicant_migrator|07-control-plane-and-ux/data/ddl/notify.sql"
  "replicant_migrator|07-control-plane-and-ux/data/ddl/github_app.sql"
  "replicant_migrator|07-control-plane-and-ux/data/ddl/slack_app.sql"
  "replicant_migrator|07-control-plane-and-ux/data/ddl/jira_app.sql"
  "replicant_migrator|04-memory-and-context/sql/memory.sql"
  "replicant_migrator|04-memory-and-context/sql/ecosystem.sql"
  "replicant_migrator|03-agents-and-tools/ddl/cost.sql"
  "replicant_migrator|06-releases-and-evals/sql/evals.sql"
  "replicant_migrator|05-qa/sql/qa-schema.sql"
)
for entry in "${FILES[@]}"; do
  role=${entry%%|*}; f=${entry#*|}
  tmp=$(mktemp)
  # audit DDL runs as audit_owner (it owns the objects); WS-10 already created schema audit.
  if [ "$role" = audit_owner ]; then
    { echo "SET ROLE audit_owner;"; grep -v '^CREATE SCHEMA IF NOT EXISTS audit' "$SPEC/$f"; } > "$tmp"
  else
    cp "$SPEC/$f" "$tmp"
  fi
  podman cp "$tmp" "$C:/tmp/owner.sql" >/dev/null; rm -f "$tmp"
  out=$(podman exec "$C" psql -U replicant_migrator -d "$DB" -q -v ON_ERROR_STOP=1 -f /tmp/owner.sql 2>&1 | grep -E 'ERROR' | head -1)
  printf '%-55s %s\n' "$f" "${out:-OK}"
done
