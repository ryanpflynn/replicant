"""locks.locked_loop against the smoke Postgres (tests/smoke/env.sh; CTL_DB_DSN): one runner per advisory lock, no
work while another session holds it, and re-acquiring after the lock connection drops."""
from __future__ import annotations

import asyncio
import os
import secrets

import pytest
from sqlalchemy import text

from control_plane_api.config import Settings
from control_plane_api.db import Database
from control_plane_api.locks import locked_loop

DSN = os.environ.get("CTL_DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="CTL_DB_DSN not set (start tests/smoke/env.sh)")

HOLDER = ("SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted AND objsubid = 2 "
          "AND classid::bigint = :a AND objid::bigint = :b")


@pytest.fixture
async def db():
    d = Database(Settings(replicant_env="local", db_dsn=DSN, db_password_file=os.environ.get("CTL_DB_PASSWORD_FILE")))
    yield d
    await d.dispose()


def _lock() -> tuple[int, int]:
    return 0x7E57, secrets.randbelow(2**30)  # a key no other test or replica uses


async def _until(cond, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not await cond():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.02)


async def _holder(db: Database, lock: tuple[int, int]) -> int | None:
    async with db.engine.connect() as c:
        return (await c.execute(text(HOLDER), {"a": lock[0], "b": lock[1]})).scalar()


async def _terminate(db: Database, pid: int) -> None:
    async with db.engine.connect() as c:
        assert (await c.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})).scalar()


class Counter:
    def __init__(self) -> None:
        self.n = 0
        self.fail_once = False

    async def __call__(self) -> None:
        self.n += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("one bad run")


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_reacquires_after_the_lock_connection_drops(db) -> None:
    lock, fn = _lock(), Counter()
    task = asyncio.create_task(locked_loop(db, lock, 0.05, fn, "test loop", retry_s=0.05))
    try:
        await _until(lambda: _ready(fn.n >= 2))
        pid = await _holder(db, lock)
        assert pid
        fn.fail_once = True  # an error in fn is logged and the loop keeps the lock
        n = fn.n
        await _until(lambda: _ready(fn.n >= n + 2))
        assert await _holder(db, lock) == pid
        await _terminate(db, pid)
        await _until(lambda: _new_holder(db, lock, pid))
        n = fn.n
        await _until(lambda: _ready(fn.n >= n + 2))
    finally:
        await _stop(task)
    assert await _holder(db, lock) is None  # released on cancel


async def test_no_work_without_the_lock_and_none_after_losing_it(db) -> None:
    lock, fn = _lock(), Counter()
    async with db.engine.connect() as other:
        await other.execute(text("SELECT pg_advisory_lock(:a, :b)"), {"a": lock[0], "b": lock[1]})
        await other.commit()
        task = asyncio.create_task(locked_loop(db, lock, 0.05, fn, "test loop", retry_s=0.05))
        try:
            await asyncio.sleep(0.4)
            assert fn.n == 0  # another session holds it
            await other.execute(text("SELECT pg_advisory_unlock(:a, :b)"), {"a": lock[0], "b": lock[1]})
            await other.commit()
            await _until(lambda: _ready(fn.n >= 2))
            pid = await _holder(db, lock)
            # A competitor queues for the lock; the loop's session dies, so the server hands the lock to the
            # competitor at once. The loop must notice and stop working while the competitor holds it.
            waiter = asyncio.create_task(other.execute(text("SELECT pg_advisory_lock(:a, :b)"),
                                                       {"a": lock[0], "b": lock[1]}))
            await asyncio.sleep(0.2)
            await _terminate(db, pid)
            await waiter
            await other.commit()
            assert await _holder(db, lock) != pid
            await asyncio.sleep(0.2)
            n = fn.n
            await asyncio.sleep(0.5)
            assert fn.n == n, "worked while another session held the lock"
            await other.execute(text("SELECT pg_advisory_unlock(:a, :b)"), {"a": lock[0], "b": lock[1]})
            await other.commit()
            await _until(lambda: _ready(fn.n >= n + 2))  # and takes over again once it is free
        finally:
            await _stop(task)


async def _ready(v: bool) -> bool:
    return v


async def _new_holder(db: Database, lock: tuple[int, int], old: int) -> bool:
    pid = await _holder(db, lock)
    return bool(pid) and pid != old
