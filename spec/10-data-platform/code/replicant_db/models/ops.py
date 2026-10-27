"""ops schema models (Core Tables). Writer: migrate Job only. Mirrors ddl/20-ops.sql exactly;
`alembic check` (CI) proves it."""
from __future__ import annotations

from sqlalchemy import (ARRAY, BigInteger, Boolean, CheckConstraint, Column, ForeignKeyConstraint, Identity,
                        Index, Integer, PrimaryKeyConstraint, SmallInteger, String, Table, Text, text)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

from replicant_db.naming import METADATA

md = METADATA["ops"]
TS = TIMESTAMP(timezone=True)

migration_runs = Table(
    "migration_runs", md,
    Column("run_seq", BigInteger, Identity(always=True), nullable=False),
    Column("started_at", TS, nullable=False, server_default=text("clock_timestamp()")),
    Column("finished_at", TS),
    Column("mode", Text, nullable=False),
    Column("from_revision", String(32)),
    Column("target_revision", String(32), nullable=False),
    Column("to_revision", String(32)),
    Column("outcome", Text, nullable=False, server_default=text("'running'::text")),
    Column("applied", ARRAY(String(32)), nullable=False, server_default=text("'{}'::character varying[]")),
    Column("image_digest", Text, nullable=False),
    Column("git_sha", Text, nullable=False),
    Column("k8s_job", Text),
    Column("argocd_app", Text),
    Column("argocd_sync_rev", Text),
    Column("lock_wait_ms", Integer),
    Column("error_class", Text),
    Column("error_summary", Text),
    PrimaryKeyConstraint("run_seq", name="pk_migration_runs"),
    CheckConstraint("mode IN ('apply','maintenance','restore_reconcile','verify')", name="mode"),
    CheckConstraint("outcome IN ('running','succeeded','noop','failed','lock_timeout','refused_incompatible',"
                    "'refused_checksum','refused_contract_gate')", name="outcome"),
    CheckConstraint("image_digest ~ '^sha256:[0-9a-f]{64}$'", name="image_digest"),
    CheckConstraint("git_sha ~ '^[0-9a-f]{40}$'", name="git_sha"),
    CheckConstraint("length(error_summary) <= 4000", name="error_summary_len"),
)
Index("ix_migration_runs_started_at", migration_runs.c.started_at.desc())

migration_log = Table(
    "migration_log", md,
    Column("revision", String(32), nullable=False),
    Column("down_revision", String(32)),
    Column("schema_name", Text, nullable=False),
    Column("compat", Text, nullable=False),
    Column("requires_reader_revision", String(32)),
    Column("description", Text, nullable=False),
    Column("sql_sha256", Text, nullable=False),
    Column("applied_at", TS, nullable=False, server_default=text("clock_timestamp()")),
    Column("applied_in_run", BigInteger, nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("downgraded_at", TS),
    PrimaryKeyConstraint("revision", name="pk_migration_log"),
    ForeignKeyConstraint(["applied_in_run"], ["ops.migration_runs.run_seq"],
                         name="fk_migration_log_applied_in_run_migration_runs"),
)

maintenance_runs = Table(
    "maintenance_runs", md,
    Column("run_seq", BigInteger, Identity(always=True), nullable=False),
    Column("task", Text, nullable=False),
    Column("target", Text, nullable=False),
    Column("started_at", TS, nullable=False, server_default=text("clock_timestamp()")),
    Column("finished_at", TS),
    Column("outcome", Text, nullable=False, server_default=text("'running'::text")),
    Column("details", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    PrimaryKeyConstraint("run_seq", name="pk_maintenance_runs"),
)
Index("ix_maintenance_runs_task_started_at", maintenance_runs.c.task, maintenance_runs.c.started_at.desc())

partitioned_tables = Table(
    "partitioned_tables", md,
    Column("table_name", Text, nullable=False),
    Column("key_kind", Text, nullable=False),
    Column("key_column", Text, nullable=False),
    Column("ulid_prefix", Text),
    Column("interval_months", SmallInteger, nullable=False, server_default=text("1")),
    Column("premake", SmallInteger, nullable=False, server_default=text("3")),
    Column("retain_months", SmallInteger, nullable=False),
    Column("owner_role", Text, nullable=False, server_default=text("'replicant_migrator'::text")),
    Column("detach_before_drop", Boolean, nullable=False, server_default=text("true")),
    Column("export_before_drop", Boolean, nullable=False, server_default=text("false")),
    PrimaryKeyConstraint("table_name", name="pk_partitioned_tables"),
)

restore_events = Table(
    "restore_events", md,
    Column("restore_seq", BigInteger, Identity(always=True), nullable=False),
    Column("detected_at", TS, nullable=False, server_default=text("clock_timestamp()")),
    Column("restore_point", TS, nullable=False),
    Column("source", Text, nullable=False),
    Column("incident_ref", Text, nullable=False),
    Column("reconcile_state", Text, nullable=False, server_default=text("'pending'::text")),
    Column("steps", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    PrimaryKeyConstraint("restore_seq", name="pk_restore_events"),
)
