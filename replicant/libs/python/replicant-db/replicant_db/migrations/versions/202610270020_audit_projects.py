"""audit.event_types: register the project events control-plane-api emits (v0 CONFIG-D10). Without them
`audit.emit` fails its event_type foreign key and the events are only logged (CONTROL-D6). Project access changes use
the existing `admin.role_binding_changed`. audit.event_types belongs to audit_owner (the migrator may SET ROLE to it).

Revision ID: 202610270020_audit_projects
Revises: 202610270010_projects_rbac
Create Date: 2026-10-27
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202610270020_audit_projects"
down_revision: str | None = "202610270010_projects_rbac"
branch_labels = None
depends_on = None

replicant_schema: str = "audit"
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None

UP = """
SET ROLE audit_owner;
INSERT INTO audit.event_types (event_type, category, description) VALUES
  ('admin.project_created',        'admin', 'Project created in a tenant'),
  ('admin.project_changed',        'admin', 'Project name, owners or repos changed'),
  ('admin.project_config_changed', 'admin', 'Project or repo configuration version written (including rollback)')
ON CONFLICT (event_type) DO NOTHING;
SET ROLE replicant_migrator;
"""

DOWN = """
SET ROLE audit_owner;
DELETE FROM audit.event_types WHERE event_type IN ('admin.project_created', 'admin.project_changed',
  'admin.project_config_changed') AND NOT EXISTS (SELECT 1 FROM audit.events e WHERE e.event_type IN
  ('admin.project_created', 'admin.project_changed', 'admin.project_config_changed'));
SET ROLE replicant_migrator;
"""


def upgrade() -> None:
    owner_ddl.execute(UP)


def downgrade() -> None:
    owner_ddl.execute(DOWN)
