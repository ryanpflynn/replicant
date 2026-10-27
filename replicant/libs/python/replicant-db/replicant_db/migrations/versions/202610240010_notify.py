"""notify: apply WS-07's reviewed DDL (notify.sql) verbatim (INDEX.md §2 apply order).

Revision ID: 202610240010_notify
Revises: 202610240009_approvals
Create Date: 2026-10-24
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202610240010_notify"
down_revision: str | None = "202610240009_approvals"
branch_labels = None
depends_on = None

replicant_schema: str = "notify"
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None


def upgrade() -> None:
    owner_ddl.upgrade(revision)


def downgrade() -> None:
    owner_ddl.downgrade(revision)
