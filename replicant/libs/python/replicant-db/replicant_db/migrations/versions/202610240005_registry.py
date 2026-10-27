"""registry: apply WS-03's reviewed DDL (registry-definitions.sql) verbatim (INDEX.md §2 apply order).

Revision ID: 202610240005_registry
Revises: 202610240004_identity
Create Date: 2026-10-24
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202610240005_registry"
down_revision: str | None = "202610240004_identity"
branch_labels = None
depends_on = None

replicant_schema: str = "registry"
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None


def upgrade() -> None:
    owner_ddl.upgrade(revision)


def downgrade() -> None:
    owner_ddl.downgrade(revision)
