"""replicant-projector entrypoint: `run` polls replicant-projections; `healthcheck` checks the deployment version."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

DEPLOYMENT = "replicant-projector"
Q = "replicant-projections"


def setup_logging() -> None:
    from replicant_telemetry.jsonlog import JsonFormatter, json_logging
    json_logging(JsonFormatter(ts=True, record_keys=("job_id",), exc_limit=None))


def _worker(client, build: str):  # noqa: ANN001, ANN202
    from temporalio.worker import Worker

    from replicant_projector.activities import ACTIVITIES
    from replicant_temporal.versioning import deployment_config
    return Worker(client, task_queue=Q, activities=ACTIVITIES, deployment_config=deployment_config(DEPLOYMENT, build),
                  max_concurrent_activities=int(os.environ.get("REPLICANT_PROJECTOR_CONCURRENCY", "32")))


async def _connect():  # noqa: ANN202
    from replicant_temporal import TemporalSettings, connect
    return await connect(TemporalSettings.from_env(identity=f"{DEPLOYMENT}@{os.uname().nodename}"))


async def run() -> None:
    from replicant_temporal.runner import run_workers, set_current_allowed
    from replicant_temporal.versioning import build_id_from_env
    build = build_id_from_env()
    client = await _connect()
    w = _worker(client, build)
    logging.getLogger("replicant_projector").info("replicant-projector starting", extra={"build_id": build})
    await run_workers(client, [w], deployment_name=DEPLOYMENT, build_id=build, set_current=set_current_allowed())


async def serve_in_process(retry_s: float = 5.0) -> None:
    """The projector inside another process (control-plane-api `serve`, SIMPLIFY-D11): the same worker on
    replicant-projections, deployment replicant-projector, run until cancelled. Unlike `run` it leaves signals to the
    host process, and a Temporal outage or a fatal worker error restarts the worker after `retry_s` instead of ending
    the process (the host keeps serving meanwhile)."""
    from replicant_projector import db
    from replicant_temporal.runner import set_current_allowed
    from replicant_temporal.versioning import build_id_from_env, set_current_version
    log = logging.getLogger("replicant_projector")
    build, set_current = build_id_from_env(), set_current_allowed()
    try:
        while True:
            try:
                client = await _connect()
                log.info("replicant-projector starting in process", extra={"build_id": build})
                async with _worker(client, build):   # __aexit__ shuts down (in-flight activities finish)
                    if set_current:                   # like run_workers: once the worker polls
                        try:
                            await set_current_version(client, DEPLOYMENT, build, identity=f"{DEPLOYMENT}-local")
                            set_current = False
                        except Exception as e:  # noqa: BLE001
                            log.error("set current failed: %s", e)
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("replicant-projector worker stopped (%s), restarting in %ss", type(e).__name__, retry_s)
                await asyncio.sleep(retry_s)
    finally:
        await db.close()


async def healthcheck() -> int:
    from replicant_temporal.health import worker_healthcheck
    from replicant_temporal.versioning import build_id_from_env
    rc = await worker_healthcheck(DEPLOYMENT, build_id_from_env(), (Q,))
    if rc == 0 and os.environ.get("REPLICANT_PROJECTOR_HEALTH_DB", "true") == "true":
        try:
            from replicant_projector import db
            await asyncio.wait_for(db.ready(), 5)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"db": "unavailable", "error": str(e)[:200]}))
            return 1
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="replicant-projector")
    ap.add_argument("cmd", choices=["run", "healthcheck"])
    a = ap.parse_args(argv)
    if a.cmd == "healthcheck":
        return asyncio.run(healthcheck())
    setup_logging()
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
