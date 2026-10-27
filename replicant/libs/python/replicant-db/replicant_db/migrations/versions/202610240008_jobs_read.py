"""jobs_read: apply WS-07's reviewed DDL (jobs_read.sql) verbatim (INDEX.md §2 apply order).

Revision ID: 202610240008_jobs_read
Revises: 202610240007_intake
Create Date: 2026-10-24
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202610240008_jobs_read"
down_revision: str | None = "202610240007_intake"
branch_labels = None
depends_on = None

replicant_schema: str = "jobs_read"
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None


def upgrade() -> None:
    owner_ddl.upgrade(revision)


def downgrade() -> None:
    owner_ddl.downgrade(revision)
