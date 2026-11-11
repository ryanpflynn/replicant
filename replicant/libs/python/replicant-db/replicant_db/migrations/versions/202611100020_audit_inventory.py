"""audit.event_types: register the inventory events control-plane-api emits (design §8c, ONBOARD-01; CONFIG-D11
pattern). audit.event_types belongs to audit_owner (the migrator may SET ROLE to it).

Revision ID: 202611100020_audit_inventory
Revises: 202611100010_inventory
Create Date: 2026-11-10
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202611100020_audit_inventory"
down_revision: str | None = "202611100010_inventory"
branch_labels = None
depends_on = None

replicant_schema: str = "audit"
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None

TYPES = ("'inventory.item_decided', 'inventory.rescan_requested', 'inventory.support_requested', "
         "'inventory.admin_denied', 'inventory.readiness_recheck'")

UP = """
SET ROLE audit_owner;
INSERT INTO audit.event_types (event_type, category, description) VALUES
  ('inventory.item_decided',      'admin', 'Fleet admin approved, denied or mapped a catalogue item (MCP server, hook, toolchain)'),
  ('inventory.rescan_requested',  'admin', 'Fleet admin requested an inventory rescan of repos'),
  ('inventory.support_requested', 'admin', 'Project admin asked the platform for support with a catalogue item'),
  ('inventory.admin_denied',      'admin', 'Fleet admin route denied (not a member of the admin group)'),
  ('inventory.readiness_recheck', 'admin', 'Readiness probe re-check requested for a repo')
ON CONFLICT (event_type) DO NOTHING;
SET ROLE replicant_migrator;
"""

DOWN = f"""
SET ROLE audit_owner;
DELETE FROM audit.event_types WHERE event_type IN ({TYPES}) AND NOT EXISTS (SELECT 1 FROM audit.events e WHERE
  e.event_type IN ({TYPES}));
SET ROLE replicant_migrator;
"""


def upgrade() -> None:
    owner_ddl.execute(UP)


def downgrade() -> None:
    owner_ddl.execute(DOWN)
