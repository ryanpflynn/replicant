"""Alembic environment for database `replicant` (one environment, ONE linear history; ADR-WS10-005).

Reviewed target. Exercised with Alembic 1.20.1 / SQLAlchemy 2.1.6 / psycopg 3.3.6 against
PostgreSQL 17 + pgvector 0.8.2 on 2026-10-24: online upgrade, `upgrade --sql` (offline),
downgrade, and `alembic check` (no drift) all ran (tools/validate.sh).

Invocation never happens by hand. Entry points:
  * `replicant-db migrate apply`        (migrate Job, compose `migrate` service) -> replicant_db.cli
  * `alembic upgrade head --sql`      (CI render step; offline mode, no DB)
  * `alembic check`                   (CI drift step against a disposable Postgres)

Conventions every revision script must follow (CI: tools/lint_migrations.py):
  revision id          "<yyyymmddhhmm>_<schema>"  (≤ 32 chars, fits ops.alembic_version)
  replicant_schema       one of the 16 C-PG schemas, or "multi" (grants/roles only)
  replicant_compat       expand | contract | data | reference | grants (C-PG 1.2.0 rule 8)
  replicant_requires_reader_revision   required when replicant_compat == "contract"
  every DDL statement qualifies the schema; no statement touches a schema other than replicant_schema
"""
from __future__ import annotations

import hashlib
import importlib
import os
import pathlib
import re
import time

from alembic import context
from alembic.runtime.migration import MigrationInfo
from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import Connection

from replicant_db.naming import METADATA, SCHEMAS

config = context.config

# Import every schema's model module so its tables register on METADATA[<schema>].
for _schema in SCHEMAS:
    try:
        importlib.import_module(f"replicant_db.models.{_schema}")
    except ModuleNotFoundError:
        pass  # schema has no ORM models yet (DDL pending from its owning workstream)

target_metadata = [METADATA[s] for s in SCHEMAS]

VERSIONS_DIR = pathlib.Path(__file__).parent / "versions"
_PARTITION_RE = re.compile(r"_\d{4}_\d{2}$")


# Schemas that have ORM models. The owner schemas are applied from their reviewed SQL files verbatim
# (replicant_db.owner_ddl), so the SQL file is the target and the catalog diff in tools/validate_migrations.sh
# proves equivalence; autogenerate compares only modelled schemas (otherwise every owner table would show
# up as "removed" and --verify-drift would always fail). docs/v0/DEVIATIONS.md PLATFORM-D3.
MODELLED_SCHEMAS = frozenset(s for s in SCHEMAS if METADATA[s].tables)


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    """Autogenerate only looks at modelled replicant schemas; never at public (extensions only)."""
    if type_ == "schema":
        return name in MODELLED_SCHEMAS
    return True


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    # Partitions are created by maintenance mode, not by models: ignore child tables named
    # <parent>_YYYY_MM. ops.alembic_version is Alembic's own.
    if type_ == "table" and name and (_PARTITION_RE.search(name) or name == "alembic_version"):
        return False
    return True


COMMON = dict(
    target_metadata=target_metadata,
    version_table="alembic_version",
    version_table_schema="ops",
    include_schemas=True,
    include_name=include_name,
    include_object=include_object,
    compare_type=True,
    compare_server_default=True,
    transaction_per_migration=True,   # each revision commits on its own (needed for autocommit_block)
)


def _script_sha256(revision: str) -> str:
    matches = list(VERSIONS_DIR.glob(f"{revision}*.py"))
    return hashlib.sha256(matches[0].read_bytes()).hexdigest() if len(matches) == 1 else "0" * 64


def _record(ctx, step: MigrationInfo, heads, run_args) -> None:  # noqa: ANN001
    """on_version_apply: write ops.migration_log in the same transaction as the revision."""
    if context.is_offline_mode():
        return
    conn: Connection = ctx.connection
    now = time.monotonic()
    elapsed_ms = int((now - config.attributes.get("_last_step_t", now)) * 1000)
    config.attributes["_last_step_t"] = now
    if step.is_upgrade:
        run_seq = config.attributes.get("run_seq")
        if run_seq is not None and not conn.execute(
                text("SELECT EXISTS (SELECT 1 FROM ops.migration_runs WHERE run_seq = :r)"), {"r": run_seq}).scalar():
            run_seq = None  # ops was rebuilt during this process (CI round-trip): start a fresh run row
        if run_seq is None:
            # Very first run: ops.migration_runs was just created by the bootstrap revision.
            meta = config.attributes.get("run_meta")
            run_seq = conn.execute(
                text("INSERT INTO ops.migration_runs (mode, target_revision, image_digest, git_sha, k8s_job) "
                     "VALUES ('apply', 'head', :img, :sha, :job) RETURNING run_seq"),
                {"img": getattr(meta, "image_digest", "sha256:" + "0" * 64), "sha": getattr(meta, "git_sha", "0" * 40),
                 "job": getattr(meta, "k8s_job", None)},
            ).scalar()
            config.attributes["run_seq"] = run_seq
        script = step.revision_map.get_revision(step.up_revision_id)
        mod = script.module
        conn.execute(
            text(
                """
                INSERT INTO ops.migration_log (revision, down_revision, schema_name, compat,
                    requires_reader_revision, description, sql_sha256, applied_in_run, duration_ms)
                VALUES (:rev, :down, :schema, :compat, :needs, :descr, :sha, :run, :ms)
                ON CONFLICT (revision) DO UPDATE SET downgraded_at = NULL, applied_at = clock_timestamp(),
                    applied_in_run = EXCLUDED.applied_in_run, sql_sha256 = EXCLUDED.sql_sha256
                """
            ),
            {
                "rev": script.revision,
                "down": script.down_revision,
                "schema": getattr(mod, "replicant_schema"),
                "compat": getattr(mod, "replicant_compat"),
                "needs": getattr(mod, "replicant_requires_reader_revision", None),
                "descr": (script.doc or "")[:500],
                "sha": _script_sha256(script.revision),
                "run": run_seq,
                "ms": elapsed_ms,
            },
        )
    elif conn.execute(text("SELECT to_regclass('ops.migration_log') IS NOT NULL")).scalar():
        conn.execute(
            text("UPDATE ops.migration_log SET downgraded_at = clock_timestamp() WHERE revision = :rev"),
            {"rev": step.up_revision_id},
        )


def run_migrations_offline() -> None:
    """`alembic upgrade <rev> --sql`: render SQL for review and for squawk. No DB connection."""
    context.configure(url="postgresql+psycopg://render@localhost/replicant", literal_binds=True,
                      dialect_opts={"paramstyle": "named"}, **COMMON)
    # Emit the same session guards the online path sets, so the rendered SQL that squawk lints
    # and reviewers read is exactly what runs.
    context.execute("SET lock_timeout = '5s'")
    context.execute("SET statement_timeout = '15min'")
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # replicant_db.cli passes an already-open connection that holds the advisory lock.
    conn: Connection | None = config.attributes.get("connection")
    if conn is None:
        # CI / developer drift checks only (disposable DB). Production never takes this path.
        from replicant_db.session import sync_engine_from_env

        engine = sync_engine_from_env("replicant-db-alembic")
        with engine.connect() as c:
            _configure_and_run(c)
        return
    _configure_and_run(conn)


def _configure_and_run(conn: Connection) -> None:
    # Session-level guards for DDL. lock_timeout keeps an ALTER from queueing behind a long
    # transaction and blocking every reader behind it (the classic lock-queue outage).
    conn.execute(text("SET lock_timeout = '5s'"))
    conn.execute(text("SET statement_timeout = '15min'"))
    conn.execute(text("SET search_path = pg_catalog, public"))
    conn.execute(text("SET ROLE replicant_migrator"))   # locally the compose user may be a superuser
    conn.commit()
    config.attributes["_last_step_t"] = time.monotonic()
    context.configure(connection=conn, on_version_apply=_record, **COMMON)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
