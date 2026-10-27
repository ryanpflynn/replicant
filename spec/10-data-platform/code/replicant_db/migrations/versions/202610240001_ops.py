"""ops bootstrap: schemas, capability/login roles, grants, ops tables, RLS and ULID helpers.

Applies ddl/10-roles-and-grants.sql and ddl/20-ops.sql verbatim (the reviewed target DDL), so
the spec DDL and the migration cannot diverge. Requires ddl/00-bootstrap.sql to have run once.

Revision ID: 202610240001_ops
Revises:
Create Date: 2026-10-24 00:01:00
"""
from __future__ import annotations

import pathlib

from alembic import op

revision: str = "202610240001_ops"
down_revision: str | None = None
branch_labels = None
depends_on = None

replicant_schema: str = "multi"
replicant_compat: str = "grants"
replicant_requires_reader_revision: str | None = None

# In the replicant-db package these files ship as package data: importlib.resources.files("replicant_db.sql").
_DDL = pathlib.Path(__file__).resolve().parents[4] / "ddl"


def _sql(name: str) -> str:
    # Strip psql meta-commands (\set ...); everything else is plain SQL.
    return "\n".join(l for l in (_DDL / name).read_text().splitlines() if not l.lstrip().startswith("\\"))


def upgrade() -> None:
    op.execute(_sql("10-roles-and-grants.sql"))
    op.execute(_sql("20-ops.sql"))


def downgrade() -> None:
    # Roles are cluster-wide and shared by every later revision; the bootstrap is never downgraded
    # in a real environment. Downgrade exists for the CI round-trip on a disposable database.
    # ops.alembic_version must survive (Alembic deletes the version row after this function).
    op.execute("""
        DROP VIEW IF EXISTS ops.schema_status, ops.top_statements, ops.connection_summary;
        DROP FUNCTION IF EXISTS ops.top_statements(integer), ops.connection_summary();
        DROP TABLE IF EXISTS ops.migration_log, ops.migration_runs, ops.maintenance_runs,
                             ops.partitioned_tables, ops.restore_events;
        DROP FUNCTION IF EXISTS ops.secure_partition(regclass, name), ops.apply_tenant_rls(regclass, name), ops.ulid_floor(text, timestamptz),
                                ops.current_tenant(), ops.principal_scope();
        DROP SCHEMA IF EXISTS tenancy, identity, intake, registry, jobs_read, approvals, memory, ecosystem,
                              cost, evals, audit, notify, github_app, slack_app, jira_app, qa;
    """)
