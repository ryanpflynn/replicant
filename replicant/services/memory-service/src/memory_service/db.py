"""One login role `svc_memory_service` (C-PG: the only writer of schema `memory`). Each transaction sets the RLS
settings: `replicant.tenant_id` and `replicant.principal_scope` (`tenant`, or `admin:memory` only after the caller's
D-25 admin membership was verified for this request)."""
from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from memory_service.config import Settings, read_secret

log = logging.getLogger(__name__)
ADMIN_SCOPE = "admin:memory"


class Database:
    def __init__(self, settings: Settings, engine: AsyncEngine | None = None, set_role: str | None = None) -> None:
        self.settings = settings
        self._engine = engine
        self.set_role = set_role  # tests: connect as a superuser and SET LOCAL ROLE svc_memory_service

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
            self._engine = create_async_engine(url, pool_size=8, max_overflow=8, pool_pre_ping=True,
                                               connect_args={"application_name": "memory-service"})
        return self._engine

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    @contextlib.asynccontextmanager
    async def tx(self, tenant: str | None = None, *, admin: bool = False) -> AsyncIterator[AsyncConnection]:
        async with self.engine.begin() as conn:
            if self.set_role:
                await conn.execute(text(f"SET LOCAL ROLE {self.set_role}"))
            await conn.execute(text("SELECT set_config('replicant.tenant_id', :t, true), "
                                    "set_config('replicant.principal_scope', :s, true)"),
                               {"t": tenant or "", "s": ADMIN_SCOPE if admin else "tenant"})
            yield conn

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("database ping failed: %s", type(e).__name__)
            return False


def rows(res: Any) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in res]


def one(res: Any) -> dict[str, Any] | None:
    r = res.first()
    return dict(r._mapping) if r is not None else None


def jdump(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)
