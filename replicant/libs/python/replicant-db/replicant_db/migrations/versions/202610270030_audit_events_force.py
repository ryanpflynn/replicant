"""audit.events: FORCE row-level security on the partitioned parent (v0 SEC-D1).

Every other table with a tenant_id column has RLS enabled and forced; audit.events had ENABLE only, so its owner
audit_owner (and therefore any SECURITY DEFINER function it owns) bypassed every policy, including for UPDATE and
DELETE. With FORCE, audit_owner gets exactly what audit.emit()/audit.emit_restored() need: INSERT and SELECT (the
idempotency lookup after ON CONFLICT DO NOTHING). No UPDATE or DELETE policy exists for anyone, so the table is
append-only even for its owner; retention drops whole partitions (DDL, unaffected by RLS). Leaf partitions are
already forced by ops.secure_partition. Idempotent; DDL runs as audit_owner, which owns the table.

Revision ID: 202610270030_audit_events_force
Revises: 202610270020_audit_projects
Create Date: 2026-10-27
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202610270030_audit_events_force"
down_revision: str | None = "202610270020_audit_projects"
branch_labels = None
depends_on = None

replicant_schema: str = "audit"
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None

UP = """
SET ROLE audit_owner;
DROP POLICY IF EXISTS events_owner_append ON audit.events;
CREATE POLICY events_owner_append ON audit.events FOR INSERT TO audit_owner WITH CHECK (true);
DROP POLICY IF EXISTS events_owner_read ON audit.events;
CREATE POLICY events_owner_read ON audit.events FOR SELECT TO audit_owner USING (true);
ALTER TABLE audit.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.events FORCE ROW LEVEL SECURITY;
SET ROLE replicant_migrator;
"""

DOWN = """
SET ROLE audit_owner;
ALTER TABLE audit.events NO FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS events_owner_append ON audit.events;
DROP POLICY IF EXISTS events_owner_read ON audit.events;
SET ROLE replicant_migrator;
"""


def upgrade() -> None:
    owner_ddl.execute(UP)


def downgrade() -> None:
    owner_ddl.execute(DOWN)
