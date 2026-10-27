"""Worker Deployment versioning helpers (temporal-platform.md section 4; C-IDS section 2 build IDs)."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Iterable

from temporalio.client import Client
from temporalio.common import WorkerDeploymentVersion
from temporalio.worker import WorkerDeploymentConfig

BUILD_ID_RE = re.compile(r"^b[0-9]{8}-[0-9a-f]{12}$")
DEFAULT_LOCAL_BUILD_ID = "b20261024-000000000000"  # compose.yaml default (x-replicant-build-args)


def dev_build_id(repo_dir: str | None = None, now: datetime | None = None) -> str:
    """`b<YYYYMMDD>-<sha12>` from git; dirty trees use the first 12 hex of sha256(git diff HEAD) (stack.md
    `replicant dev build`). Falls back to the compose default when git is unavailable."""
    day = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True).stdout.strip()
        diff = subprocess.run(["git", "diff", "HEAD"], cwd=repo_dir, capture_output=True, check=True).stdout
        return f"b{day}-{(hashlib.sha256(diff).hexdigest() if diff else sha)[:12]}"
    except Exception:  # noqa: BLE001
        return DEFAULT_LOCAL_BUILD_ID


def build_id_from_env(var: str = "REPLICANT_WORKER_BUILD_ID") -> str:
    b = os.environ.get(var) or DEFAULT_LOCAL_BUILD_ID
    if not BUILD_ID_RE.match(b):
        raise SystemExit(f"{var}={b!r} is not a C-IDS build ID (b<YYYYMMDD>-<sha12>)")
    return b


def deployment_config(deployment_name: str, build_id: str) -> WorkerDeploymentConfig:
    """Versioned worker config. ReplicantJob is PINNED, so its worker must use versioning."""
    return WorkerDeploymentConfig(version=WorkerDeploymentVersion(deployment_name, build_id), use_worker_versioning=True)


async def set_current_version(client: Client, deployment_name: str, build_id: str, *, identity: str,
                              attempts: int = 60, delay: float = 2.0) -> bool:
    """Make (deployment, build_id) the current version. Retries while the version's pollers register
    (the server refuses a version without pollers on its task queues). Local and dev only."""
    from temporalio.api.workflowservice.v1 import SetWorkerDeploymentCurrentVersionRequest
    last: Exception | None = None
    for _ in range(attempts):
        try:
            await client.workflow_service.set_worker_deployment_current_version(SetWorkerDeploymentCurrentVersionRequest(
                namespace=client.namespace, deployment_name=deployment_name, build_id=build_id, identity=identity))
            return True
        except Exception as e:  # noqa: BLE001 - pollers not yet registered
            last = e
            await asyncio.sleep(delay)
    raise RuntimeError(f"could not set {deployment_name}:{build_id} current: {last}")


async def current_build_id(client: Client, deployment_name: str) -> str | None:
    from temporalio.api.workflowservice.v1 import DescribeWorkerDeploymentRequest
    d = await client.workflow_service.describe_worker_deployment(DescribeWorkerDeploymentRequest(
        namespace=client.namespace, deployment_name=deployment_name))
    rc = d.worker_deployment_info.routing_config
    return rc.current_deployment_version.build_id or None


async def describe_version_queues(client: Client, deployment_name: str, build_id: str) -> set[str]:
    from temporalio.api.deployment.v1 import WorkerDeploymentVersion as PVer
    from temporalio.api.workflowservice.v1 import DescribeWorkerDeploymentVersionRequest
    d = await client.workflow_service.describe_worker_deployment_version(DescribeWorkerDeploymentVersionRequest(
        namespace=client.namespace, deployment_version=PVer(deployment_name=deployment_name, build_id=build_id)))
    return {t.name for t in d.version_task_queues}


def missing_queues(have: Iterable[str], want: Iterable[str]) -> list[str]:
    return sorted(set(want) - set(have))
