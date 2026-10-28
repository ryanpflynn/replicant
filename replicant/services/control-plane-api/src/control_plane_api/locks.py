"""Singleton background loops across control-plane-api replicas: Postgres session advisory locks.

`locked_loop` holds `pg_try_advisory_lock(a, b)` on a dedicated connection and runs `fn` every `interval` seconds
while it holds it. Before each run it checks that the lock connection is alive and still holds the lock: a dropped
connection (Postgres restart, idle-session kill, network) releases a session lock on the server, and another replica
may take over, so the loop stops working and goes back to acquiring instead of running unlocked.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text

log = logging.getLogger(__name__)

# pg_locks shows a two-int4 advisory key as classid = a, objid = b, objsubid = 2 (oids, so a negative key wraps)
HELD_SQL = ("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted "
            "AND objsubid = 2 AND classid::bigint = (CAST(:a AS bigint) & 4294967295) "
            "AND objid::bigint = (CAST(:b AS bigint) & 4294967295))")


async def still_held(conn: Any, lock: tuple[int, int]) -> bool:
    """True when `conn`'s session holds the advisory lock (raises when the connection is gone)."""
    held = (await conn.execute(text(HELD_SQL), {"a": lock[0], "b": lock[1]})).scalar()
    await conn.commit()  # never sit idle in a transaction on the lock connection
    return bool(held)


async def locked_loop(db: Any, lock: tuple[int, int], interval: float, fn: Callable[[], Awaitable[Any]], name: str,
                      *, retry_s: float | None = None) -> None:
    """Run `fn()` every `interval` s while this process holds advisory lock `lock`; forever (cancel to stop).

    An exception in `fn` is logged as `<name>: <type>: <message>` and the next run follows after `interval`. Lock
    errors are logged as `<name> lock: ...`. Without the lock (or after losing it) the loop retries every `retry_s`
    (default `interval`) seconds."""
    a, b = lock
    while True:
        try:
            async with db.engine.connect() as lock_conn:
                got = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:a, :b)"), {"a": a, "b": b})).scalar()
                await lock_conn.commit()
                if got:
                    try:
                        while await still_held(lock_conn, lock):
                            try:
                                await fn()
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:  # noqa: BLE001 - one bad run never stops the loop
                                log.warning("%s: %s: %s", name, type(e).__name__, str(e)[:300])
                            await asyncio.sleep(interval)
                        log.warning("%s lock: lost; acquiring again", name)
                    finally:
                        try:
                            await lock_conn.execute(text("SELECT pg_advisory_unlock(:a, :b)"), {"a": a, "b": b})
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # noqa: BLE001 - the connection is gone, and the lock with it
                            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("%s lock: %s: %s", name, type(e).__name__, str(e)[:300])
        await asyncio.sleep(interval if retry_s is None else retry_s)
