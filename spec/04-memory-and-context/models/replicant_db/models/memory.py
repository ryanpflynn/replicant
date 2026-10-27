"""SQLAlchemy 2.0 models for schema ``memory`` (core tables). WS-04 sketch for the shared
``replicant-db`` package (D-26). Writer: memory-service only (C-PG). Other services must not
import this module for writes (import-linter contract ``memory-writer-only``).

The reviewed target DDL is ``spec/04-memory-and-context/sql/memory.sql``. Columns, types,
nullability, defaults, primary/foreign keys and indexes here match it exactly; CI checks this
with ``alembic.autogenerate.compare_metadata`` against a database migrated from that DDL
(validated 2026-10-24, see ``models/check_drift.py``). CHECK constraints are declared
by name where the DDL names them; triggers, RLS policies, and SQL functions are created by
migrations through ``alembic_utils`` (``PGFunction``, ``PGTrigger``, ``PGPolicy``) because
autogenerate does not model them.

Libraries: SQLAlchemy 2.0 (``Mapped[]``/``mapped_column``), pgvector-python 0.5.x
(``pgvector.sqlalchemy.HALFVEC``).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Text,
    cast,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "memory"
TZ = TIMESTAMP(timezone=True)

ID_BODY = "[0-9a-hjkmnp-tv-z]{26}"
TENANT_RE = "^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
PRINCIPAL_RE = f"^(usr_{ID_BODY}|svc_[a-z][a-z0-9-]{{2,40}})$"


class MemoryBase(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


def tenant_col() -> Mapped[str]:
    return mapped_column(Text, nullable=False)


class EmbeddingSpace(MemoryBase):
    __tablename__ = "embedding_spaces"
    __table_args__ = (
        Index("embedding_spaces_one_active", text("(true)"), unique=True, postgresql_where=text("state = 'active'")),
    )

    space_id: Mapped[str] = mapped_column(Text, primary_key=True)
    gateway_model_id: Mapped[str] = mapped_column(Text)
    dimensions: Mapped[int] = mapped_column(Integer)
    distance: Mapped[str] = mapped_column(Text, server_default=text("'cosine'::text"))
    input_type: Mapped[str] = mapped_column(Text, server_default=text("'document'::text"))
    state: Mapped[str] = mapped_column(Text)
    dedupe_merge_threshold: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    dedupe_judge_threshold: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    conflict_probe_threshold: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    created_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))
    activated_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    retired_at: Mapped[dt.datetime | None] = mapped_column(TZ)


class GovernancePolicy(MemoryBase):
    __tablename__ = "governance_policies"
    __table_args__ = (
        Index("governance_policies_one_current", "tenant_id", unique=True, postgresql_where=text("is_current")),
    )

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    policy_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    schema_id: Mapped[str] = mapped_column(Text, server_default=text("'replicant.memory-governance/v1'::text"))
    digest: Mapped[str] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))
    change_note: Mapped[str | None] = mapped_column(Text)


class Entry(MemoryBase):
    __tablename__ = "entries"
    __table_args__ = (
        CheckConstraint("(scope_level = 'org') = (tenant_id = 'replicant-system')", name="entries_org_tenant"),
        CheckConstraint(
            "(scope_level IN ('org','tenant') AND service_group IS NULL AND repo IS NULL)"
            " OR (scope_level = 'service_group' AND service_group IS NOT NULL AND repo IS NULL)"
            " OR (scope_level IN ('repo','path') AND repo IS NOT NULL)",
            name="entries_scope_shape",
        ),
        CheckConstraint("(state = 'purged') = (purged_at IS NOT NULL)", name="entries_purged"),
        Index("entries_retrieval", "tenant_id", "state", "memory_type", "scope_level"),
        Index("entries_repo_active", "repo", "memory_type", postgresql_where=text("state = 'active'")),
        Index(
            "entries_subject",
            "tenant_id",
            "subject_key",
            postgresql_where=text("subject_key IS NOT NULL AND state IN ('candidate', 'active')"),
        ),
        Index("entries_candidates", "tenant_id", "created_at", postgresql_where=text("state = 'candidate'")),
        Index(
            "entries_review_due",
            "review_due_at",
            postgresql_where=text("state = 'active' AND review_due_at IS NOT NULL"),
        ),
        Index("entries_quarantine", "quarantine_id", postgresql_where=text("quarantine_id IS NOT NULL")),
    )

    entry_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = tenant_col()
    memory_type: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    state_seq: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    state_changed_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))
    current_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    scope_level: Mapped[str] = mapped_column(Text)
    service_group: Mapped[str | None] = mapped_column(Text)
    repo: Mapped[str | None] = mapped_column(Text)
    subject_key: Mapped[str | None] = mapped_column(Text)
    enforced: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    confidence_alpha: Mapped[Decimal] = mapped_column(Numeric(10, 4), server_default=text("1"))
    confidence_beta: Mapped[Decimal] = mapped_column(Numeric(10, 4), server_default=text("1"))
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(6, 5),
        Computed("round(confidence_alpha / (confidence_alpha + confidence_beta), 5)", persisted=True),
    )
    evidence_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    independent_evidence_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    distinct_author_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    distinct_repo_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_confirmed_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    probation_until: Mapped[dt.datetime | None] = mapped_column(TZ)
    review_due_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    candidate_expires_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    quarantine_id: Mapped[str | None] = mapped_column(Text)
    activation_hold: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    hold_reason: Mapped[str | None] = mapped_column(Text)
    injection_count: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    last_injected_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text)
    purged_at: Mapped[dt.datetime | None] = mapped_column(TZ)


class EntryVersion(MemoryBase):
    __tablename__ = "entry_versions"
    __table_args__ = (
        CheckConstraint("(purged_at IS NULL) = (statement IS NOT NULL)", name="entry_versions_content"),
        Index("entry_versions_tsv", "tsv", postgresql_using="gin"),
        Index("entry_versions_trgm", "statement", postgresql_using="gin", postgresql_ops={"statement": "gin_trgm_ops"}),
        Index("entry_versions_repo", "tenant_id", "repo", "scope_level"),
    )

    entry_id: Mapped[str] = mapped_column(Text, ForeignKey("memory.entries.entry_id", ondelete="RESTRICT"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = tenant_col()
    scope_level: Mapped[str] = mapped_column(Text)
    service_group: Mapped[str | None] = mapped_column(Text)
    repo: Mapped[str | None] = mapped_column(Text)
    repo_id: Mapped[str | None] = mapped_column(Text)
    path_globs: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    languages: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    frameworks: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    workflow_types: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    subject_key: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    statement: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    structured: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    structured_schema: Mapped[str] = mapped_column(Text)
    examples: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    token_estimate: Mapped[int | None] = mapped_column(Integer)
    content_digest: Mapped[str] = mapped_column(Text)
    change_kind: Mapped[str] = mapped_column(Text)
    change_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text)
    purged_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(statement, '')), 'B') || "
            "setweight(to_tsvector('english', coalesce(rationale, '')), 'C') || "
            "setweight(to_tsvector('simple', coalesce(replace(subject_key, '.', ' '), '')), 'A')",
            persisted=True,
        ),
    )


class EntryEmbedding(MemoryBase):
    __tablename__ = "entry_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["entry_id", "version"],
            ["memory.entry_versions.entry_id", "memory.entry_versions.version"],
            ondelete="CASCADE",
        ),
        Index("entry_embeddings_tenant", "space_id", "tenant_id", "memory_type"),
        # LIST-partitioned by tenant (R2-051); partitions are created by memory.ensure_tenant_partitions()
        {"postgresql_partition_by": "LIST (tenant_id)"},
    )

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    entry_id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    space_id: Mapped[str] = mapped_column(Text, ForeignKey("memory.embedding_spaces.space_id"), primary_key=True)
    memory_type: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(HALFVEC())  # no typmod: dimensions vary by space
    input_digest: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))


# One partial expression HNSW index per embedding space (ADR-04-003). Declared after the
# class so the cast can reference the mapped column; renders exactly as in memory.sql.
Index(
    "entry_embeddings_hnsw_vc4_1024",
    cast(EntryEmbedding.embedding, HALFVEC(1024)).label("embedding_1024"),
    postgresql_using="hnsw",
    postgresql_ops={"embedding_1024": "halfvec_cosine_ops"},
    postgresql_with={"m": 16, "ef_construction": 128},
    postgresql_where=text("space_id = 'vc4-1024'"),
)


class Provenance(MemoryBase):
    __tablename__ = "provenance"
    __table_args__ = (
        Index("provenance_entry", "entry_id"),
        Index("provenance_author", "tenant_id", "author_principal_id", "observed_at"),
        Index("provenance_source", "repo", "pr_number", "comment_id"),
        Index(
            "provenance_one_per_source",
            "entry_id",
            "independence_key",
            unique=True,
            postgresql_where=text("withdrawn_at IS NULL"),
        ),
        Index("provenance_job", "job_id", postgresql_where=text("job_id IS NOT NULL")),
    )

    provenance_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    entry_id: Mapped[str] = mapped_column(Text, ForeignKey("memory.entries.entry_id", ondelete="RESTRICT"))
    tenant_id: Mapped[str] = tenant_col()
    supports_version: Mapped[int] = mapped_column(Integer)
    source_kind: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    repo: Mapped[str | None] = mapped_column(Text)
    repo_id: Mapped[str | None] = mapped_column(Text)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    comment_id: Mapped[int | None] = mapped_column(BigInteger)
    review_id: Mapped[int | None] = mapped_column(BigInteger)
    thread_node_id: Mapped[str | None] = mapped_column(Text)
    comment_digest: Mapped[str | None] = mapped_column(Text)
    source_excerpt: Mapped[str | None] = mapped_column(Text)
    author_principal_id: Mapped[str | None] = mapped_column(Text)
    author_github_user_id: Mapped[int | None] = mapped_column(BigInteger)
    author_repo_permission: Mapped[str | None] = mapped_column(Text)
    author_is_codeowner: Mapped[bool | None] = mapped_column(Boolean)
    author_association: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(Text)
    cycle: Mapped[int | None] = mapped_column(Integer)
    delivery_id: Mapped[str | None] = mapped_column(Text)
    release_bundle_id: Mapped[str | None] = mapped_column(Text)
    extraction_prompt_name: Mapped[str | None] = mapped_column(Text)
    extraction_prompt_version: Mapped[int | None] = mapped_column(Integer)
    extraction_model: Mapped[str | None] = mapped_column(Text)
    extraction_trace_id: Mapped[str | None] = mapped_column(Text)
    generalization: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    independence_key: Mapped[str] = mapped_column(Text)
    approving_curator: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[dt.datetime] = mapped_column(TZ)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))
    withdrawn_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    withdrawn_reason: Mapped[str | None] = mapped_column(Text)


class StateTransition(MemoryBase):
    __tablename__ = "state_transitions"
    __table_args__ = (
        CheckConstraint(
            "actor_kind <> 'admin' OR (actor_principal_id ~ '^usr_' AND curator_action_id IS NOT NULL)",
            name="st_admin_is_human",
        ),
        CheckConstraint(
            "actor_kind NOT IN ('system_policy', 'system_safety') OR (actor_principal_id = 'svc_memory-service'"
            " AND governance_tenant_id IS NOT NULL AND governance_policy_version IS NOT NULL)",
            name="st_system_has_policy",
        ),
        CheckConstraint(
            "actor_kind <> 'system_safety' OR to_state IN ('quarantined', 'disabled', 'rejected', 'expired')",
            name="st_safety_tightens",
        ),
        ForeignKeyConstraint(
            ["governance_tenant_id", "governance_policy_version"],
            ["memory.governance_policies.tenant_id", "memory.governance_policies.policy_version"],
            name="st_policy_fk",
        ),
        Index("state_transitions_entry_at", "entry_id", "at"),
        Index("state_transitions_tenant_at", "tenant_id", "at"),
    )

    transition_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    entry_id: Mapped[str] = mapped_column(Text, ForeignKey("memory.entries.entry_id", ondelete="RESTRICT"))
    tenant_id: Mapped[str] = tenant_col()
    from_state: Mapped[str | None] = mapped_column(Text)
    to_state: Mapped[str] = mapped_column(Text)
    entry_version: Mapped[int] = mapped_column(Integer)
    at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))
    actor_kind: Mapped[str] = mapped_column(Text)
    actor_principal_id: Mapped[str] = mapped_column(Text)
    governance_tenant_id: Mapped[str | None] = mapped_column(Text)
    governance_policy_version: Mapped[int | None] = mapped_column(Integer)
    curator_action_id: Mapped[str | None] = mapped_column(Text)
    quarantine_id: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str] = mapped_column(Text)
    reason_text: Mapped[str | None] = mapped_column(Text)
    audit_event_id: Mapped[str | None] = mapped_column(Text)
    leak_id: Mapped[str | None] = mapped_column(Text)       # actor_kind 'leak_purge' (R1-103)


class Conflict(MemoryBase):
    __tablename__ = "conflicts"
    __table_args__ = (
        ForeignKeyConstraint(["entry_a", "version_a"], ["memory.entry_versions.entry_id", "memory.entry_versions.version"]),
        ForeignKeyConstraint(["entry_b", "version_b"], ["memory.entry_versions.entry_id", "memory.entry_versions.version"]),
        Index(
            "conflicts_one_open_per_pair", "entry_a", "entry_b", "kind", unique=True, postgresql_where=text("state = 'open'")
        ),
        Index("conflicts_queue", "tenant_id", "detected_at", postgresql_where=text("state = 'open'")),
    )

    conflict_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = tenant_col()
    entry_a: Mapped[str] = mapped_column(Text, ForeignKey("memory.entries.entry_id"))
    version_a: Mapped[int] = mapped_column(Integer)
    entry_b: Mapped[str] = mapped_column(Text, ForeignKey("memory.entries.entry_id"))
    version_b: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)
    detected_by: Mapped[str] = mapped_column(Text)
    detector_detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    state: Mapped[str] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)
    blocks_activation: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    detected_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))
    resolved_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    resolved_by: Mapped[str | None] = mapped_column(Text)
    curator_action_id: Mapped[str | None] = mapped_column(Text)


class Snapshot(MemoryBase):
    __tablename__ = "snapshots"
    __table_args__ = (Index("snapshots_tenant_created", "tenant_id", "created_at"),)

    snapshot_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = tenant_col()
    policy_ref: Mapped[str] = mapped_column(Text)
    policy_digest: Mapped[str] = mapped_column(Text)
    spec_digest: Mapped[str] = mapped_column(Text)
    item_count: Mapped[int] = mapped_column(Integer)
    export_key: Mapped[str] = mapped_column(Text)          # blob first, row second (NOT NULL)
    export_sha256: Mapped[str] = mapped_column(Text)
    renderer_version: Mapped[str] = mapped_column(Text)
    first_job_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))


class SnapshotItem(MemoryBase):
    __tablename__ = "snapshot_items"
    __table_args__ = (
        ForeignKeyConstraint(["entry_id", "version"], ["memory.entry_versions.entry_id", "memory.entry_versions.version"]),
        Index("snapshot_items_entry", "entry_id", "version"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    snapshot_id: Mapped[str] = mapped_column(
        Text, ForeignKey("memory.snapshots.snapshot_id", ondelete="RESTRICT"), primary_key=True
    )
    entry_id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    tenant_id: Mapped[str] = tenant_col()
    section: Mapped[str] = mapped_column(Text)
    precedence_rank: Mapped[int] = mapped_column(Integer)
    scope_specificity: Mapped[int] = mapped_column(SmallInteger)
    base_score: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    overrides: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    # holdout arms are per (job, entry): memory.holdout_assignments (R1-096)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, primary_key=True)


class JobSnapshot(MemoryBase):
    __tablename__ = "job_snapshots"
    __table_args__ = (
        CheckConstraint("(reason IN ('create', 'eval_pinned')) = (base_snapshot_id IS NULL)", name="job_snapshots_base"),
        Index("job_snapshots_idem", "idempotency_key", unique=True),
        Index("job_snapshots_snapshot", "snapshot_id"),
    )

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = tenant_col()
    snapshot_id: Mapped[str] = mapped_column(Text, ForeignKey("memory.snapshots.snapshot_id"))
    base_snapshot_id: Mapped[str | None] = mapped_column(Text, ForeignKey("memory.snapshots.snapshot_id"))
    reason: Mapped[str] = mapped_column(Text)
    scope_tenant_id: Mapped[str] = mapped_column(Text)
    evaluation_mode: Mapped[str | None] = mapped_column(Text)
    replay_of_job_id: Mapped[str | None] = mapped_column(Text)
    repos: Mapped[list[str]] = mapped_column(ARRAY(Text))
    repo: Mapped[str] = mapped_column(Text)
    workflow_type: Mapped[str] = mapped_column(Text)
    release_bundle_id: Mapped[str] = mapped_column(Text)
    pull_as_of: Mapped[dt.datetime] = mapped_column(TZ)
    requester_principal_id: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))


class Injection(MemoryBase):
    __tablename__ = "injections"
    __table_args__ = (
        Index("injections_job", "job_id"),
        Index("injections_refs", "injected_refs", postgresql_using="gin"),
        Index("injections_tenant_created", "tenant_id", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text)
    tenant_id: Mapped[str] = tenant_col()
    snapshot_id: Mapped[str] = mapped_column(Text, ForeignKey("memory.snapshots.snapshot_id"))
    role: Mapped[str] = mapped_column(Text)
    cli: Mapped[str] = mapped_column(Text)
    node_key: Mapped[str] = mapped_column(Text)
    cycle: Mapped[int] = mapped_column(Integer)
    injected_refs: Mapped[list[str]] = mapped_column(ARRAY(Text))
    cut_refs: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    token_count: Mapped[int] = mapped_column(Integer)
    budget_tokens: Mapped[int] = mapped_column(Integer)
    tokenizer: Mapped[str] = mapped_column(Text)
    rendered_sha256: Mapped[str] = mapped_column(Text)
    renderer_version: Mapped[str] = mapped_column(Text)
    withheld_refs: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    replay_of_run_id: Mapped[str | None] = mapped_column(Text)
    artifact_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=text("now()"))


CORE_TABLES = [m.__table__ for m in MemoryBase.__subclasses__()]
