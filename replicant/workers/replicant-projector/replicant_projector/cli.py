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
    class Json(logging.Formatter):
        def format(self, r: logging.LogRecord) -> str:
            return json.dumps({"ts": self.formatTime(r), "level": r.levelname.lower(), "logger": r.name,
                               "msg": r.getMessage(), **({"job_id": r.job_id} if getattr(r, "job_id", None) else {})})
    from replicant_telemetry.redact import RedactingFormatter
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(RedactingFormatter(Json()))
    logging.basicConfig(level=os.environ.get("REPLICANT_LOG_LEVEL", "INFO"), handlers=[h], force=True)


async def run() -> None:
    from temporalio.worker import Worker

    from replicant_projector.activities import ACTIVITIES
    from replicant_temporal import TemporalSettings, connect
    from replicant_temporal.runner import run_workers, set_current_allowed
    from replicant_temporal.versioning import build_id_from_env, deployment_config
    build = build_id_from_env()
    client = await connect(TemporalSettings.from_env(identity=f"{DEPLOYMENT}@{os.uname().nodename}"))
    w = Worker(client, task_queue=Q, activities=ACTIVITIES, deployment_config=deployment_config(DEPLOYMENT, build),
               max_concurrent_activities=int(os.environ.get("REPLICANT_PROJECTOR_CONCURRENCY", "32")))
    logging.getLogger("replicant_projector").info("replicant-projector starting", extra={"build_id": build})
    await run_workers(client, [w], deployment_name=DEPLOYMENT, build_id=build, set_current=set_current_allowed())


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
