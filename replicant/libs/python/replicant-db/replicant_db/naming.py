"""replicant_db.naming — one MetaData per schema, stable constraint names, ID column helpers.

Reviewed target for the shared `replicant-db` package (D-26, C-PG rule 7). Tested with
SQLAlchemy 2.1.6 / Alembic 1.20.1 / psycopg 3.3.6 against PostgreSQL 17.11 (2026-10-24).

Why naming_convention: Alembic can only alter or drop constraints it can name. Without a
convention, PostgreSQL invents names (e.g. "jobs_tenant_id_check") that differ between a database
built from models and one built from migrations, and the drift check would flag them forever.
"""
from __future__ import annotations

import re
from typing import Final

from sqlalchemy import CheckConstraint, Column, MetaData, String, Text
from sqlalchemy.orm import DeclarativeBase

# Token reference: SQLAlchemy docs "Configuring Constraint Naming Conventions"
# [verified: https://docs.sqlalchemy.org/en/21/core/constraints.html, 2026-10-24].
# PostgreSQL truncates identifiers at 63 bytes; tools/check_identifiers.py fails CI on any
# generated name longer than 63 (squawk rule identifier-too-long also catches it).
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

SCHEMAS: Final[tuple[str, ...]] = (
    "ops", "tenancy", "identity", "intake", "registry", "jobs_read", "approvals", "memory",
    "ecosystem", "cost", "evals", "audit", "notify", "github_app", "slack_app", "jira_app", "qa",
)

# One MetaData object per schema. The owning writer's models (replicant_db.models.<schema>)
# attach to it; Alembic's target_metadata is the list of all of them.
METADATA: Final[dict[str, MetaData]] = {
    s: MetaData(schema=s, naming_convention=NAMING_CONVENTION) for s in SCHEMAS
}


def base_for(schema: str) -> type[DeclarativeBase]:
    """Declarative base bound to one schema's MetaData. Used only inside replicant_db.models.<schema>."""
    md = METADATA[schema]

    class _Base(DeclarativeBase):
        metadata = md

    _Base.__name__ = f"{schema.title().replace('_', '')}Base"
    return _Base


# --- Identifier columns (C-IDS §1, §12: text + CHECK regex, never uuid) -----------------------

ID_BODY: Final[str] = "[0-9a-hjkmnp-tv-z]{26}"
TENANT_RE: Final[str] = "^[a-z][a-z0-9]*(-[a-z0-9]+)*$"

# `COLLATE "C"`: byte order. Makes ULID ids sort by creation time (required for ULID-range
# partitioning, ddl/20-ops.sql ops.ulid_floor) and keeps btree comparisons cheap.
IdText = Text().with_variant(Text(collation="C"), "postgresql")


def id_check(column: str, prefix: str) -> CheckConstraint:
    if not re.fullmatch(r"[a-z]{2,6}", prefix):
        raise ValueError(prefix)
    return CheckConstraint(f"{column} ~ '^{prefix}_{ID_BODY}$'", name=f"{column}_format")


def tenant_check(column: str = "tenant_id") -> CheckConstraint:
    return CheckConstraint(
        f"{column} ~ '{TENANT_RE}' AND length({column}) BETWEEN 3 AND 32", name=f"{column}_format"
    )


def digest_check(column: str) -> CheckConstraint:
    return CheckConstraint(f"{column} ~ '^sha256:[0-9a-f]{{64}}$'", name=f"{column}_format")


def tenant_id_column() -> Column[str]:
    """Every tenant-data table has tenant_id text NOT NULL (C-PG rule 4)."""
    return Column("tenant_id", Text(collation="C"), nullable=False)


ALEMBIC_REV_RE: Final = re.compile(r"^\d{12}_[a-z_]{2,19}$")  # e.g. 202610240001_ops (≤ 32 chars)
_ = String  # re-exported for model modules that need bounded varchar
