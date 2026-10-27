"""Database access (architecture.md §2.1): one login role `svc_control_plane_api`, one pool; each module
`SET LOCAL ROLE`s to its WS-10 NOLOGIN module role per transaction and sets the RLS settings
(`replicant.tenant_id`, `replicant.principal_scope`, C-PG rule 4) with `set_config(..., is_local => true)`."""
from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from control_plane_api.config import Settings, read_secret

log = logging.getLogger(__name__)

MODULE_ROLES = ("svc_cpa_intake", "svc_cpa_tenancy", "svc_cpa_registry", "svc_cpa_notify")
OPERATOR_SCOPE = "operator:all-tenants"


class Database:
    def __init__(self, settings: Settings, engine: AsyncEngine | None = None) -> None:
        self.settings = settings
        self._engine = engine
        self._has: dict[str, bool] = {}

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            if not self.settings.db_dsn:
                raise RuntimeError("REPLICANT_DB_DSN is not set")
            url = make_url(self.settings.db_dsn)
            pw = read_secret(self.settings.db_password_file)
            if pw:
                url = url.set(password=pw)
            if url.drivername in ("postgresql", "postgres"):
                url = url.set(drivername="postgresql+psycopg")
            self._engine = create_async_engine(url, pool_size=10, max_overflow=10, pool_pre_ping=True,
                                               connect_args={"application_name": "control-plane-api"})
        return self._engine

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    @contextlib.asynccontextmanager
    async def tx(self, *, role: str | None = None, tenant: str | None = None,
                 scope: str | None = None) -> AsyncIterator[AsyncConnection]:
        """One transaction. `role` is a module role (writes); reads use the login's inherited svc_cpa_read.
        `tenant` sets replicant.tenant_id; `scope` sets replicant.principal_scope (operator:all-tenants for operator reads)."""
        async with self.engine.begin() as conn:
            if role and self.settings.db_set_role:
                if role not in MODULE_ROLES:
                    raise ValueError(f"unknown module role {role}")
                await conn.execute(text(f"SET LOCAL ROLE {role}"))
            if tenant is not None:
                await conn.execute(text("SELECT set_config('replicant.tenant_id', :t, true)"), {"t": tenant})
            if scope is not None:
                await conn.execute(text("SELECT set_config('replicant.principal_scope', :s, true)"), {"s": scope})
            yield conn

    async def has_relation(self, qualified: str) -> bool:
        """Whether a table/view exists (M1: cost.*, audit.* may not be migrated yet). Cached per process."""
        if qualified not in self._has:
            async with self.engine.connect() as conn:
                r = await conn.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": qualified})
                self._has[qualified] = bool(r.scalar())
        return self._has[qualified]

    async def has_function(self, qualified: str) -> bool:
        key = "fn:" + qualified
        if key not in self._has:
            async with self.engine.connect() as conn:
                r = await conn.execute(text("SELECT to_regproc(:n) IS NOT NULL"), {"n": qualified})
                self._has[key] = bool(r.scalar())
        return self._has[key]

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("database ping failed: %s", type(e).__name__)
            return False


def row_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping) if row is not None else {}
