"""eval-worker's audited cross-tenant reads: WS-10 ddl/40-eval-metrics-reader.sql (CCR-06-15).

Applied after every owner's DDL because it names their columns (INDEX.md §2). Re-runnable.

Revision ID: 202610240019_eval_reader
Revises: 202610240018_qa
Create Date: 2026-10-24
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202610240019_eval_reader"
down_revision: str | None = "202610240018_qa"
branch_labels = None
depends_on = None

replicant_schema: str = "multi"
replicant_compat: str = "grants"
replicant_requires_reader_revision: str | None = None


def upgrade() -> None:
    owner_ddl.execute(owner_ddl.read_sql("40-eval-metrics-reader.sql"))
    owner_ddl.execute("SET ROLE replicant_migrator")


def downgrade() -> None:
    owner_ddl.execute(
        "DO $drop$ DECLARE f regprocedure; BEGIN FOR f IN SELECT p.oid::regprocedure FROM pg_proc p "
        "WHERE p.pronamespace = 'ops'::regnamespace AND p.proname LIKE 'eval\\_%' LOOP "
        "EXECUTE format('DROP FUNCTION IF EXISTS %s CASCADE', f); END LOOP; END $drop$")
