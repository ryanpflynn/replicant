"""replicant_db.session — engines, IAM auth, transaction-scoped tenant context, module roles.

Reviewed target for the shared `replicant-db` package (D-26, C-PG rules 1, 4).
Driver: psycopg 3 (async), via SQLAlchemy dialect `postgresql+psycopg`.

Protocol (normative; schema-management.md §4):

1. Every unit of work is ONE transaction opened through `tx(...)`. The first statements of the
   transaction are
       SELECT set_config('replicant.tenant_id', :tenant, true),
              set_config('replicant.principal_scope', :scope, true)
   and, for control-plane-api modules, `set_config('role', 'svc_cpa_<module>', true)`.
   `is_local => true` means the values die at COMMIT/ROLLBACK, so a pooled connection can never
   leak one tenant's context into another request (SQLAlchemy pool today; PgBouncer transaction
   pooling if ever introduced, ADR-WS10-004).
2. No tenant set => every RLS policy evaluates to false => zero rows / insert rejected
   (fail closed). Validated on PostgreSQL 17.11 (2026-10-24).
3. Cross-tenant scopes are role-gated: 'operator:all-tenants' (read) is honored only for members of
   r_cross_tenant_read, 'system:all-tenants' (read+write) only for r_cross_tenant_write
   (ddl/20-ops.sql). Asking for one without membership silently yields tenant-scoped results.
4. Connections go DIRECTLY to the Aurora writer (or reader) endpoint; there is no RDS Proxy.
   RDS Proxy pins a client to a backend for the rest of the session on "SET and set_config
   commands" [verified: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-proxy-pinning.html,
   2026-10-24], i.e. on every transaction of this protocol, so it would multiplex nothing
   (ADR-WS10-004). Each pod's pool is small and budgeted (postgres.md §5).
5. psycopg's automatic server-side prepared statements stay ON (default prepare_threshold=5):
   valid for direct connections. If PgBouncer is introduced, it must be >= 1.22 with
   max_prepared_statements > 0, otherwise set prepare_threshold=None
   [verified: https://github.com/psycopg/psycopg/blob/master/docs/advanced/prepare.rst, 2026-10-24].
"""
from __future__ import annotations

import contextlib
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

import boto3
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

Scope = Literal["tenant", "operator:all-tenants", "system:all-tenants", "admin:memory", "eval:platform"]
Module = Literal["svc_cpa_tenancy", "svc_cpa_intake", "svc_cpa_registry", "svc_cpa_notify"]


@dataclass(frozen=True)
class DbSettings:
    """Populated from env by each service's settings module (12-factor)."""
    host: str                     # REPLICANT_DB_HOST: Aurora cluster (writer) endpoint; readers use REPLICANT_DB_RO_HOST
    port: int                     # REPLICANT_DB_PORT
    database: str                 # REPLICANT_DB_NAME = "replicant"
    user: str                     # REPLICANT_DB_USER = "svc_<service>"
    region: str | None            # AWS_REGION; None => local password auth
    password: str | None = None   # local only (compose .env); never set in AWS
    pool_size: int = 5
    max_overflow: int = 5
    application_name: str = "replicant"
    sslrootcert: str = "/etc/ssl/certs/rds-global-bundle.pem"


def _iam_token(s: DbSettings) -> str:
    """RDS IAM auth token: 15-minute validity, checked only at connect time.
    [verified: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.html, 2026-10-24]
    Credentials come from EKS Pod Identity (the pod's IAM role holds rds-db:connect for exactly
    this DB user; config/iam/svc-db-connect-policy.json)."""
    client = boto3.client("rds", region_name=s.region)
    return client.generate_db_auth_token(DBHostname=s.host, Port=s.port, DBUsername=s.user, Region=s.region)


def make_engine(s: DbSettings) -> AsyncEngine:
    connect_args: dict[str, object] = {
        "application_name": s.application_name,
        "connect_timeout": 5,
    }
    if s.region:
        connect_args |= {"sslmode": "verify-full", "sslrootcert": s.sslrootcert}
    engine = create_async_engine(
        f"postgresql+psycopg://{s.user}@{s.host}:{s.port}/{s.database}",
        connect_args=connect_args,
        pool_size=s.pool_size,
        max_overflow=s.max_overflow,
        pool_pre_ping=True,
        pool_recycle=900,                  # IAM tokens are checked only at connect; recycling every 15 min
                                           # re-resolves DNS after an Aurora failover and spreads reconnects
        pool_timeout=10,
    )

    @event.listens_for(engine.sync_engine, "do_connect")
    def _provide_password(dialect, conn_rec, cargs, cparams):  # noqa: ANN001
        cparams["password"] = _iam_token(s) if s.region else s.password

    return engine


class TenantContextMissing(RuntimeError):
    pass


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@contextlib.asynccontextmanager
async def tx(
    sessions: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str | None,
    scope: Scope = "tenant",
    module: Module | None = None,
    read_only: bool = False,
    statement_timeout_ms: int | None = None,
) -> AsyncIterator[AsyncSession]:
    """The ONLY way services open a transaction (import-linter forbids AsyncSession.begin elsewhere).

    tenant_id=None is allowed only with scope='operator:all-tenants' (operator views, reconcilers) or for
    tables without tenant data (identity.users, registry global definitions)."""
    if tenant_id is None and scope == "tenant":
        raise TenantContextMissing("tenant_id is required unless scope='operator:all-tenants'")
    async with sessions() as session, session.begin():
        params = {"t": tenant_id or "", "s": scope}
        await session.execute(
            text("SELECT set_config('replicant.tenant_id', :t, true), set_config('replicant.principal_scope', :s, true)"),
            params,
        )
        if module is not None:
            await session.execute(text("SELECT set_config('role', :r, true)"), {"r": module})
        if read_only:
            await session.execute(text("SET TRANSACTION READ ONLY"))
        if statement_timeout_ms is not None:
            await session.execute(text("SELECT set_config('statement_timeout', :v, true)"), {"v": f"{statement_timeout_ms}ms"})
        yield session


# ---------------------------------------------------------------------------------------------
# Temporal activities (replicant-projector, eval-worker) — pattern
# ---------------------------------------------------------------------------------------------
# * One engine per worker process, created at worker start (not per activity).
# * Each activity execution is one `tx(...)`; the tenant comes from the activity input
#   (ProjectionEvent.tenant_id), never from ambient state.
# * Activities must be idempotent because Temporal retries them: projection.apply uses the seq
#   guard (C-STATE §2.1); every other write is INSERT ... ON CONFLICT on a natural key derived
#   from the logical activity ID.
# * Activity heartbeats are not used for DB work; statement_timeout (30 s default) is shorter
#   than the activity start_to_close so a stuck query fails the attempt cleanly.
# * Retryable errors: OperationalError / connection errors / serialization_failure (40001) /
#   deadlock_detected (40P01) / lock_not_available (55P03) => raise (Temporal retries).
#   Non-retryable: IntegrityError on a CHECK (bad payload) => ApplicationError(non_retryable=True).
#
# FastAPI (control-plane-api) — pattern
# * Engine in the lifespan handler; dependency `get_tx(module=...)` wraps `tx()` per request
#   after authorization has produced (tenant_id, scope).
# * LISTEN replicant_job_updates uses one dedicated psycopg AsyncConnection per pod to the writer
#   endpoint, outside the pool (LISTEN is session state). On reconnect it re-LISTENs and forces a
#   re-fetch of every subscribed job (NOTIFY is not durable across failover).

PG_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01", "55P03", "57P01", "57P02", "57P03", "08000", "08003", "08006"})


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def settings_from_env(service: str) -> DbSettings:
    """Env contract (XD-11-01). AWS: REPLICANT_DB_HOST/PORT/NAME/USER + REPLICANT_DB_IAM_AUTH=true.
    Local/CI: either REPLICANT_DB_DSN (postgresql://user@host:port/db) or the discrete variables,
    plus REPLICANT_DB_PASSWORD_FILE (compose secret). A password never appears in an env var."""
    import pathlib
    from urllib.parse import urlparse

    iam = os.environ.get("REPLICANT_DB_IAM_AUTH", "false") == "true"
    pw_file = os.environ.get("REPLICANT_DB_PASSWORD_FILE")
    password = pathlib.Path(pw_file).read_text().strip() if pw_file and not iam else None
    if dsn := os.environ.get("REPLICANT_DB_DSN"):
        u = urlparse(dsn)
        host, port, database, user = u.hostname or "", u.port or 5432, (u.path or "/replicant").lstrip("/"), u.username or ""
    else:
        host = os.environ["REPLICANT_DB_HOST"]
        port = int(os.environ.get("REPLICANT_DB_PORT", "5432"))
        database = os.environ.get("REPLICANT_DB_NAME", "replicant")
        user = os.environ.get("REPLICANT_DB_USER", f"svc_{service.replace('-', '_')}")
    return DbSettings(
        host=host, port=port, database=database, user=user,
        region=os.environ.get("AWS_REGION") if iam else None,
        password=password,
        application_name=service,
    )


def sync_engine_from_env(application_name: str = "replicant-db-migrate"):
    """Synchronous engine for the migrate/maintenance image and CI tools (R1-251: one env contract).

    Resolution order: REPLICANT_DB_DSN (postgresql[+psycopg]://user[:password]@host:port/db), else the
    discrete REPLICANT_DB_HOST/PORT/NAME/USER. Password: inline in the DSN (CI only), else
    REPLICANT_DB_PASSWORD_FILE (compose secret), else an IAM token when REPLICANT_DB_IAM_AUTH=true (AWS)."""
    import pathlib
    from urllib.parse import urlparse

    from sqlalchemy import create_engine, event, pool

    dsn = os.environ.get("REPLICANT_DB_DSN")
    if dsn:
        u = urlparse(dsn)
        user, host, port, db, inline_pw = u.username, u.hostname, u.port or 5432, u.path.lstrip("/"), u.password
    else:
        user = os.environ.get("REPLICANT_DB_USER", "replicant_migrator")
        host, port = os.environ["REPLICANT_DB_HOST"], int(os.environ.get("REPLICANT_DB_PORT", "5432"))
        db, inline_pw = os.environ.get("REPLICANT_DB_NAME", "replicant"), None
    iam = os.environ.get("REPLICANT_DB_IAM_AUTH", "false") == "true"
    connect_args: dict[str, object] = {"application_name": application_name, "connect_timeout": 10}
    if iam:
        connect_args |= {"sslmode": "verify-full", "sslrootcert": "/etc/ssl/certs/rds-global-bundle.pem"}
    engine = create_engine(f"postgresql+psycopg://{user}@{host}:{port}/{db}", poolclass=pool.NullPool,
                           connect_args=connect_args)
    pw_file = os.environ.get("REPLICANT_DB_PASSWORD_FILE")
    settings = DbSettings(host=host, port=port, database=db, user=user,
                          region=os.environ.get("AWS_REGION") if iam else None)

    @event.listens_for(engine, "do_connect")
    def _pw(dialect, conn_rec, cargs, cparams):  # noqa: ANN001
        if inline_pw:
            cparams["password"] = inline_pw
        elif pw_file:
            cparams["password"] = pathlib.Path(pw_file).read_text().strip()
        elif iam:
            cparams["password"] = _iam_token(settings)

    return engine
