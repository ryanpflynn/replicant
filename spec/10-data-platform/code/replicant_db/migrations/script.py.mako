"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels = None
depends_on = None

# --- replicant conventions (tools/lint_migrations.py enforces) ---------------------------------
replicant_schema: str = "CHANGE_ME"          # one C-PG schema, or "multi" for grants/roles only
replicant_compat: str = "CHANGE_ME"          # expand | contract | data | reference | grants (C-PG rule 8)
replicant_requires_reader_revision: str | None = None   # mandatory for contract


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
