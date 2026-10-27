"""Temporal access for control-plane-api: pinned ReplicantJob starts (versioning.md §3.1) and Updates (C-MSG §3).

Client: WS-01's `replicant_temporal.connect()` (XD-FIX-R1-016). Payloads use the default converter: M1 has no tenant
payload codec (INTERFACES-LOG WORKER-01; plan §1.3 defers it to P2).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from temporalio.client import Client, WorkflowUpdateFailedError, WorkflowUpdateRPCTimeoutOrCancelledError
from temporalio.common import (PinnedVersioningOverride, Priority, WorkerDeploymentVersion, WorkflowIDConflictPolicy,
                               WorkflowIDReusePolicy)
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from control_plane_api.config import Settings

log = logging.getLogger(__name__)

TASK_QUEUE_JOBS = "replicant-jobs"
DEPLOYMENT = "replicant-worker"
PRIORITY_KEY = {"urgent": 1, "high": 2, "normal": 3, "low": 4}


@dataclass
class UpdateOutcome:
    kind: str                     # result | rejected | unavailable
    result: dict[str, Any] | None = None
    error_type: str | None = None
    message: str | None = None
    details: list[Any] | None = None


class TemporalGateway:
    def __init__(self, settings: Settings, client: Client | None = None) -> None:
        self.settings = settings
        self._client = client
        self._lock = asyncio.Lock()

    async def client(self) -> Client:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    from replicant_temporal import TemporalSettings, connect
                    s = TemporalSettings(address=self.settings.temporal_address,
                                         namespace=self.settings.temporal_namespace, identity="control-plane-api")
                    self._client = await asyncio.wait_for(connect(s), timeout=10)
        return self._client

    async def reachable(self) -> bool:
        try:
            c = await self.client()
            await asyncio.wait_for(c.service_client.check_health(), timeout=3)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("temporal not reachable: %s", type(e).__name__)
            return False

    async def start_job(self, *, workflow_type: str, job_id: str, tenant_id: str, release_bundle_id: str,
                        build_id: str, job_request: dict[str, Any], priority: str = "normal",
                        repos: list[str] | None = None) -> str:
        """Starts the job PINNED to the bundle's worker build. Idempotent: USE_EXISTING + REJECT_DUPLICATE."""
        from replicant_temporal.search_attributes import job_start_attributes
        c = await self.client()
        arg = {"mode": "start", "job_id": job_id, "tenant_id": tenant_id, "release_bundle_id": release_bundle_id,
               "job_request": job_request}
        h = await c.start_workflow(
            workflow_type, arg, id=job_id, task_queue=TASK_QUEUE_JOBS,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            versioning_override=PinnedVersioningOverride(WorkerDeploymentVersion(DEPLOYMENT, build_id)),
            search_attributes=job_start_attributes(job_id=job_id, tenant_id=tenant_id,
                                                   release_bundle_id=release_bundle_id),
            priority=Priority(priority_key=PRIORITY_KEY.get(priority, 3), fairness_key=tenant_id),
            rpc_timeout=dt.timedelta(seconds=10))
        return h.first_execution_run_id or h.result_run_id or ""

    async def execute_update(self, workflow_id: str, name: str, arg: dict[str, Any], update_id: str,
                             timeout_s: float = 10.0) -> UpdateOutcome:
        """C-MSG §3: bounded attempt (10 s), same Update ID on every retry."""
        try:
            c = await self.client()
            h = c.get_workflow_handle(workflow_id)
            res = await asyncio.wait_for(
                h.execute_update(name, arg, id=update_id, rpc_timeout=dt.timedelta(seconds=timeout_s)),
                timeout=timeout_s + 2)
            return UpdateOutcome("result", result=res)
        except WorkflowUpdateFailedError as e:
            cause = e.cause
            if isinstance(cause, ApplicationError):
                return UpdateOutcome("rejected", error_type=cause.type, message=cause.message,
                                     details=list(cause.details or []))
            return UpdateOutcome("rejected", error_type=type(cause).__name__ if cause else "UpdateFailed",
                                 message=str(cause or e))
        except WorkflowUpdateRPCTimeoutOrCancelledError:
            return UpdateOutcome("unavailable", error_type="timeout")
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                return UpdateOutcome("rejected", error_type="WorkflowNotFound", message=e.message)
            return UpdateOutcome("unavailable", error_type=e.status.name if e.status else "rpc", message=e.message)
        except (asyncio.TimeoutError, TimeoutError):
            return UpdateOutcome("unavailable", error_type="timeout")
        except Exception as e:  # noqa: BLE001 - connection errors: keep the outbox row and retry
            return UpdateOutcome("unavailable", error_type=type(e).__name__, message=str(e)[:300])

    async def signal(self, workflow_id: str, name: str, arg: dict[str, Any]) -> None:
        c = await self.client()
        await c.get_workflow_handle(workflow_id).signal(name, arg, rpc_timeout=dt.timedelta(seconds=10))

