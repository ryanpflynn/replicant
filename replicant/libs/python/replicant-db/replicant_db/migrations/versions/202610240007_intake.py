"""intake: apply WS-07's reviewed DDL (intake.sql) verbatim (INDEX.md §2 apply order).

Revision ID: 202610240007_intake
Revises: 202610240006_registry_releases
Create Date: 2026-10-24
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202610240007_intake"
down_revision: str | None = "202610240006_registry_releases"
branch_labels = None
depends_on = None

replicant_schema: str = "intake"
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None


def upgrade() -> None:
    owner_ddl.upgrade(revision)


def downgrade() -> None:
    owner_ddl.downgrade(revision)
