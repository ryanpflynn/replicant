"""Run a set of Temporal workers until SIGTERM/SIGINT, optionally making their deployment version current."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Sequence

from temporalio.client import Client
from temporalio.worker import Worker

from replicant_temporal.versioning import set_current_version

log = logging.getLogger("replicant_temporal.runner")


def set_current_allowed() -> bool:
    on = os.environ.get("REPLICANT_WORKER_SET_CURRENT_ON_START", "false") == "true"
    if on and os.environ.get("REPLICANT_ENV", "local") in ("prod", "staging"):
        raise SystemExit("REPLICANT_WORKER_SET_CURRENT_ON_START is forbidden in staging and prod")
    return on


async def run_workers(client: Client, workers: Sequence[Worker], *, deployment_name: str, build_id: str,
                      set_current: bool | None = None, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # not the main thread (tests)
            pass
    tasks = [asyncio.create_task(w.run()) for w in workers]
    if set_current if set_current is not None else set_current_allowed():
        async def _cur() -> None:
            try:
                await set_current_version(client, deployment_name, build_id, identity=f"{deployment_name}-local")
                log.info("deployment version set current", extra={"deployment": deployment_name, "build_id": build_id})
            except Exception as e:  # noqa: BLE001
                log.error("set current failed: %s", e)
        tasks.append(asyncio.create_task(_cur()))
    waiter = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait([waiter, *tasks[: len(workers)]], return_when=asyncio.FIRST_COMPLETED)
    for t in done:
        if t is not waiter and t.exception():
            log.error("worker stopped: %s", t.exception())
    await asyncio.gather(*(w.shutdown() for w in workers), return_exceptions=True)
    for t in tasks:
        t.cancel()
