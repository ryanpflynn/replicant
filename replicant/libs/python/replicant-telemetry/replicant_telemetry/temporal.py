"""Temporal tracing interceptor (ADR-08-007, minimal): spans for workflow starts, Signals/Updates sent to a job, and
every activity execution, all in the job's T-1 trace. Workflow code itself is not traced (determinism).

`replicant_temporal.connect()` adds it automatically when this package is installed and tracing is enabled.
"""
from __future__ import annotations

from typing import Any

import temporalio.activity
import temporalio.client
import temporalio.worker
from opentelemetry.trace import SpanKind

from replicant_telemetry import runtime


def _job_of(workflow_id: str | None) -> str | None:
    if workflow_id and workflow_id.startswith("job_"):
        return workflow_id.split(":", 1)[0]
    return None


def _tenant(arg: Any) -> str | None:
    return arg.get("tenant_id") if isinstance(arg, dict) else None


class _ClientOutbound(temporalio.client.OutboundInterceptor):
    async def start_workflow(self, input: temporalio.client.StartWorkflowInput) -> Any:  # noqa: A002
        job = _job_of(input.id)
        if not job:
            return await super().start_workflow(input)
        arg = input.args[0] if input.args else None
        tenant = _tenant(arg)
        runtime.emit_job_root(job, tenant or "", release_bundle_id=(arg or {}).get("release_bundle_id")
                              if isinstance(arg, dict) else None)
        with runtime.job_span(f"temporal.start_workflow {input.workflow}", job, tenant_id=tenant,
                              kind=SpanKind.CLIENT, attributes={"replicant.workflow_id": input.id}):
            return await super().start_workflow(input)

    async def signal_workflow(self, input: temporalio.client.SignalWorkflowInput) -> None:  # noqa: A002
        job = _job_of(input.id)
        if not job:
            return await super().signal_workflow(input)
        with runtime.job_span(f"temporal.signal {input.signal}", job, kind=SpanKind.CLIENT):
            return await super().signal_workflow(input)

    async def start_workflow_update(self, input: temporalio.client.StartWorkflowUpdateInput) -> Any:  # noqa: A002
        job = _job_of(input.id)
        if not job:
            return await super().start_workflow_update(input)
        with runtime.job_span(f"temporal.update {input.update}", job, kind=SpanKind.CLIENT,
                              attributes={"replicant.update_id": input.update_id or ""}):
            return await super().start_workflow_update(input)


class _ActivityInbound(temporalio.worker.ActivityInboundInterceptor):
    async def execute_activity(self, input: temporalio.worker.ExecuteActivityInput) -> Any:  # noqa: A002
        info = temporalio.activity.info()
        job = _job_of(info.workflow_id)
        if not job:
            return await super().execute_activity(input)
        attrs = {"replicant.workflow_id": info.workflow_id, "replicant.workflow_run_id": info.workflow_run_id,
                 "replicant.activity_id": info.activity_id, "temporal.activity.type": info.activity_type,
                 "temporal.activity.attempt": info.attempt, "temporal.task_queue": info.task_queue}
        with runtime.job_span(f"activity {info.activity_type}", job, kind=SpanKind.SERVER, attributes=attrs):
            return await super().execute_activity(input)


class TracingInterceptor(temporalio.client.Interceptor, temporalio.worker.Interceptor):
    def intercept_client(self, next: temporalio.client.OutboundInterceptor) -> temporalio.client.OutboundInterceptor:  # noqa: A002
        return _ClientOutbound(super().intercept_client(next))

    def intercept_activity(self, next: temporalio.worker.ActivityInboundInterceptor) -> temporalio.worker.ActivityInboundInterceptor:  # noqa: A002
        return _ActivityInbound(super().intercept_activity(next))
