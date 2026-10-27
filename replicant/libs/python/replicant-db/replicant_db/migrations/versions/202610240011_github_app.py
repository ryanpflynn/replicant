"""github_app: apply WS-07's reviewed DDL (github_app.sql) verbatim (INDEX.md §2 apply order).

Revision ID: 202610240011_github_app
Revises: 202610240010_notify
Create Date: 2026-10-24
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202610240011_github_app"
down_revision: str | None = "202610240010_notify"
branch_labels = None
depends_on = None

replicant_schema: str = "github_app"
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None


def upgrade() -> None:
    owner_ddl.upgrade(revision)


def downgrade() -> None:
    owner_ddl.downgrade(revision)
