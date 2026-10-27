"""SQLAlchemy 2.0 models for schema ``ecosystem`` (core tables). Writer: ecosystem-indexer
only (C-PG). Readers (context-mcp, replicant-worker, memory-service) use these classes for
SELECT only; the import-linter contract ``ecosystem-writer-only`` forbids session writes
from other packages.

Target DDL: ``spec/04-memory-and-context/sql/ecosystem.sql`` (drift-checked by
``models/check_drift.py``).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, MetaData, Numeric, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TZ = TIMESTAMP(timezone=True)


class EcosystemBase(DeclarativeBase):
    metadata = MetaData(schema="ecosystem")


class GraphVersion(EcosystemBase):
    __tablename__ = "graph_versions"

    graph_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    committed_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))
    source_run_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class Repo(EcosystemBase):
    __tablename__ = "repos"
    __table_args__ = (Index("repos_tenant", "tenant_id"),)

    repo_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    repo: Mapped[str] = mapped_column(Text, unique=True)
    default_branch: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    primary_languages: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    frameworks: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    indexed_sha: Mapped[str | None] = mapped_column(Text)
    indexed_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    zoekt_indexed_sha: Mapped[str | None] = mapped_column(Text)
    zoekt_indexed_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    first_seen_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))
    last_seen_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))


class Service(EcosystemBase):
    __tablename__ = "services"
    __table_args__ = (
        Index("services_tenant", "tenant_id", postgresql_where=text("removed_at IS NULL")),
        Index("services_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
    )

    service_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    system_ref: Mapped[str | None] = mapped_column(Text)
    service_group: Mapped[str | None] = mapped_column(Text)
    owner_group: Mapped[str | None] = mapped_column(Text)
    lifecycle: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[int | None] = mapped_column(SmallInteger)
    description: Mapped[str | None] = mapped_column(Text)
    sources: Mapped[list[str]] = mapped_column(ARRAY(Text))
    first_seen_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))
    last_seen_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))
    graph_seq: Mapped[int] = mapped_column(BigInteger, ForeignKey("ecosystem.graph_versions.graph_seq"))
    removed_at: Mapped[dt.datetime | None] = mapped_column(TZ)


class Contract(EcosystemBase):
    __tablename__ = "contracts"
    __table_args__ = (Index("contracts_repo", "repo_id"), Index("contracts_service", "service_ref"))

    contract_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("ecosystem.repos.repo_id"))
    path: Mapped[str] = mapped_column(Text)
    service_ref: Mapped[str | None] = mapped_column(Text, ForeignKey("ecosystem.services.service_ref"))
    tenant_id: Mapped[str] = mapped_column(Text)
    current_digest: Mapped[str | None] = mapped_column(Text)
    removed_at: Mapped[dt.datetime | None] = mapped_column(TZ)


class ContractVersion(EcosystemBase):
    __tablename__ = "contract_versions"
    __table_args__ = (Index("contract_versions_commit", "repo_id", "commit_sha"),)

    contract_ref: Mapped[str] = mapped_column(Text, ForeignKey("ecosystem.contracts.contract_ref"), primary_key=True)
    digest: Mapped[str] = mapped_column(Text, primary_key=True)
    repo_id: Mapped[str] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Text)
    commit_sha: Mapped[str] = mapped_column(Text)
    git_blob_sha: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    extractor: Mapped[str] = mapped_column(Text)
    normalized: Mapped[dict[str, Any]] = mapped_column(JSONB)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    extracted_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))


class Edge(EcosystemBase):
    __tablename__ = "edges"
    __table_args__ = (
        Index("edges_to", "to_service", "edge_kind"),
        Index("edges_via", "via", postgresql_where=text("via <> ''::text")),
    )

    from_service: Mapped[str] = mapped_column(Text, ForeignKey("ecosystem.services.service_ref"), primary_key=True)
    to_service: Mapped[str] = mapped_column(Text, ForeignKey("ecosystem.services.service_ref"), primary_key=True)
    edge_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    via: Mapped[str] = mapped_column(Text, primary_key=True, server_default=text("''::text"))
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    calls_per_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    error_ratio: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    tenant_id: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))
    last_seen_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))
    graph_seq: Mapped[int] = mapped_column(BigInteger, ForeignKey("ecosystem.graph_versions.graph_seq"))
