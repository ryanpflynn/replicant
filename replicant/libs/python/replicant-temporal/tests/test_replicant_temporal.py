"""replicant-temporal against a local dev server: header propagation and versioned workers."""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import VersioningBehavior
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from replicant_temporal.interceptors import ReplicantContext, ReplicantContextInterceptor, current_context, use_context
from replicant_temporal.search_attributes import dev_server_args, job_start_attributes
from replicant_temporal.versioning import (
    BUILD_ID_RE,
    current_build_id,
    deployment_config,
    describe_version_queues,
    dev_build_id,
    set_current_version,
)


@activity.defn(name="t.echo_ctx")
async def echo_ctx(_: str) -> dict:
    c = current_context()
    return {"job_id": c.job_id if c else None, "tenant_id": c.tenant_id if c else None}


@workflow.defn(name="TCtx", versioning_behavior=VersioningBehavior.PINNED)
class TCtx:
    @workflow.run
    async def run(self, x: str) -> dict:
        return await workflow.execute_activity("t.echo_ctx", x, start_to_close_timeout=timedelta(seconds=10))


def test_build_ids() -> None:
    assert BUILD_ID_RE.match(dev_build_id())
    assert not BUILD_ID_RE.match("b2026-1")


def test_header_and_versioning() -> None:
    async def main() -> None:
        env = await WorkflowEnvironment.start_local(dev_server_extra_args=[
            *dev_server_args(), "--dynamic-config-value", "frontend.workerVersioningWorkflowAPIs=true", "--log-level", "error"])
        try:
            from temporalio.client import Client
            c = await Client.connect(env.client.service_client.config.target_host, namespace=env.client.namespace,
                                     interceptors=[ReplicantContextInterceptor()])
            tq = f"tq-{uuid.uuid4().hex[:8]}"
            build = "b20261024-0000000000aa"
            w = Worker(c, task_queue=tq, workflows=[TCtx], activities=[echo_ctx],
                       deployment_config=deployment_config("t-deploy", build))
            async with w:
                await set_current_version(c, "t-deploy", build, identity="test", attempts=30, delay=1)
                assert await current_build_id(c, "t-deploy") == build
                assert tq in await describe_version_queues(c, "t-deploy", build)
                job = "job_01m5p7x7r0bt69j0h0fbxepw90"
                with use_context(ReplicantContext(job_id=job, tenant_id="payments")):
                    r = await c.execute_workflow("TCtx", "x", id=job, task_queue=tq,
                                                 search_attributes=job_start_attributes(
                                                     job_id=job, tenant_id="payments",
                                                     release_bundle_id="rb_ds5p3wmcj4dag2e9a8ncp4py6k"))
                assert r == {"job_id": job, "tenant_id": "payments"}, r
        finally:
            await env.shutdown()
    asyncio.run(main())
