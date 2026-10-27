"""`<worker> healthcheck` (compose healthchecks): exit 0 when Temporal is reachable, this deployment version has
pollers on all of its task queues, and (when REPLICANT_WORKER_SET_CURRENT_ON_START=true) it is the current version."""
from __future__ import annotations

import json
import os
import sys

from replicant_temporal.client import TemporalSettings, connect
from replicant_temporal.versioning import current_build_id, describe_version_queues, missing_queues


async def worker_healthcheck(deployment_name: str, build_id: str, queues: tuple[str, ...], *, out=sys.stdout) -> int:
    try:
        client = await connect(TemporalSettings.from_env(identity=f"{deployment_name}-healthcheck"))
        have = await describe_version_queues(client, deployment_name, build_id)
        missing = missing_queues(have, queues)
        cur = None
        if os.environ.get("REPLICANT_WORKER_SET_CURRENT_ON_START", "false") == "true":
            cur = await current_build_id(client, deployment_name)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"deployment": deployment_name, "build_id": build_id, "error": str(e)[:300]}), file=out)
        return 1
    ok = not missing and (cur is None or cur == build_id)
    print(json.dumps({"deployment": deployment_name, "build_id": build_id, "task_queues": sorted(have),
                      "missing": missing, "current": cur, "healthy": ok}), file=out)
    return 0 if ok else 1
