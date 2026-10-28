"""Test harness: local Temporal dev server + ReplicantJob worker + stub activities (all task queues)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import sys
from typing import Any, AsyncIterator

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from temporalio.client import Client, WorkflowHandle  # noqa: E402
from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Replayer, Worker, WorkerDeploymentConfig  # noqa: E402
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions  # noqa: E402

from replicant_worker import ids  # noqa: E402
from support.stubs import FakeWorld  # noqa: E402
from replicant_worker.workflows.maintenance import MaintenanceBatch, MaintenanceShard  # noqa: E402
from replicant_worker.workflows.replicant_job import ReplicantJob, ReplicantJobInput  # noqa: E402

SEARCH_ATTRIBUTES = {
    "ReplicantJobId": "Keyword", "ReplicantTenantId": "Keyword", "ReplicantWorkflowType": "Keyword",
    "ReplicantStatus": "Keyword", "ReplicantPhase": "Keyword", "ReplicantReleaseBundleId": "Keyword",
    "ReplicantParentJobId": "Keyword", "ReplicantRiskLevel": "Keyword", "ReplicantRepos": "KeywordList",
    "ReplicantOpenGateIds": "KeywordList", "ReplicantRepairCycle": "Int", "ReplicantProjectionSeq": "Int",
    "ReplicantWaitingSince": "Datetime", "ReplicantAwaitingHuman": "Bool",
}

PASSTHROUGH = ("typing_extensions", "pydantic", "pydantic_core", "replicant_worker.build_config")
RUNNER = SandboxedWorkflowRunner(restrictions=SandboxRestrictions.default.with_passthrough_modules(*PASSTHROUGH))

from replicant_worker.resources import CONTRACTS  # noqa: E402
JOB_REQUEST = json.loads((CONTRACTS / "job-request.schema.json").read_text())["examples"][0]
BUNDLE = "rb_ds5p3wmcj4dag2e9a8ncp4py6k"
ALICE = "usr_01knk8dfr0fvjc7v0b9vhvkzaa"
BOB = "usr_01knk8dfr0fvjc7v0b9vhvkzbb"


async def start_env() -> WorkflowEnvironment:
    args: list[str] = []
    for k, v in SEARCH_ATTRIBUTES.items():
        args += ["--search-attribute", f"{k}={v}"]
    args += ["--dynamic-config-value", "frontend.workerVersioningWorkflowAPIs=true",
             "--log-level", "error"]
    return await WorkflowEnvironment.start_local(dev_server_extra_args=args)


DEFAULT_BUILD = "b20261024-000000000001"


@contextlib.asynccontextmanager
async def workers(client: Client, world: FakeWorld, *, build_id: str = DEFAULT_BUILD,
                  activity_workers: bool = True, set_current: bool = True) -> AsyncIterator[None]:
    """ReplicantJob is PINNED, so its worker must run with Worker Versioning (a PINNED workflow on an
    unversioned worker fails every workflow task: 'versioning behavior cannot be specified without
    deployment options being set with versioned mode', observed with server 1.32.0)."""
    from temporalio.api.workflowservice.v1 import SetWorkerDeploymentCurrentVersionRequest
    from temporalio.common import WorkerDeploymentVersion
    by_q = world.activities(client)
    dep = WorkerDeploymentConfig(version=WorkerDeploymentVersion("replicant-worker", build_id), use_worker_versioning=True)
    ws = [Worker(client, task_queue="replicant-jobs", workflows=[ReplicantJob, MaintenanceBatch, MaintenanceShard], activities=by_q["replicant-jobs"],
                 workflow_runner=RUNNER, deployment_config=dep)]
    # replicant-runner-calls and replicant-llm-calls belong to the replicant-worker deployment version
    ws += [Worker(client, task_queue=q, activities=by_q[q], deployment_config=dep)
           for q in ("replicant-runner-calls", "replicant-llm-calls")]
    if activity_workers:  # other deployments (publisher-worker, replicant-projector): unversioned here
        ws += [Worker(client, task_queue=q, activities=by_q[q]) for q in ("replicant-publisher", "replicant-projections")]
    async with contextlib.AsyncExitStack() as st:
        for w in ws:
            await st.enter_async_context(w)
        if set_current:
            async def setc():
                await client.workflow_service.set_worker_deployment_current_version(
                    SetWorkerDeploymentCurrentVersionRequest(namespace=client.namespace, deployment_name="replicant-worker",
                                                             build_id=build_id, identity="harness"))
                return True
            await wait_for(setc, 30, "set current version")
        yield


def new_job_id(n: int) -> str:
    return ids.derived("job", "test", str(n))


async def start_job(client: Client, job_id: str, *, workflow_type: str = "auto", priority: str = "normal",
                    versioning_override: Any = None, labels: list[str] | None = None,
                    options: dict[str, Any] | None = None, related: dict[str, Any] | None = None) -> WorkflowHandle:
    jr = json.loads(json.dumps(JOB_REQUEST))
    if options:
        jr["options"] = options
    if related:
        jr["related"] = related
    jr["workflow_type"] = workflow_type
    jr["priority"] = priority
    if labels:
        jr.setdefault("constraints", {})["labels"] = labels
    sa = TypedSearchAttributes([
        SearchAttributePair(SearchAttributeKey.for_keyword("ReplicantJobId"), job_id),
        SearchAttributePair(SearchAttributeKey.for_keyword("ReplicantTenantId"), "payments"),
        SearchAttributePair(SearchAttributeKey.for_keyword("ReplicantStatus"), "queued"),
        SearchAttributePair(SearchAttributeKey.for_keyword("ReplicantReleaseBundleId"), BUNDLE),
        SearchAttributePair(SearchAttributeKey.for_keyword("ReplicantWorkflowType"), "unrouted"),
    ])
    kw: dict[str, Any] = {}
    if versioning_override is not None:
        kw["versioning_override"] = versioning_override
    return await client.start_workflow(
        "ReplicantJob", ReplicantJobInput(mode="start", job_id=job_id, tenant_id="payments", release_bundle_id=BUNDLE,
                                      job_request=jr),
        id=job_id, task_queue="replicant-jobs", search_attributes=sa, **kw)


async def wait_for(pred, timeout: float = 60.0, what: str = "condition") -> Any:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    last: Any = None
    while loop.time() < end:
        try:
            last = await pred()
            if last:
                return last
        except Exception as e:  # query before the first task, etc.
            last = e
        await asyncio.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {what}; last={last!r}")


async def open_gate(client: Client, job_id: str, kind: str, timeout: float = 60.0) -> dict[str, Any]:
    async def p():
        r = await client.get_workflow_handle(job_id).query("job.open_gates")
        return next((g for g in r["gates"] if g["kind"] == kind), None)
    return await wait_for(p, timeout, f"{kind} gate on {job_id}")


async def snapshot(client: Client, job_id: str) -> dict[str, Any]:
    return await client.get_workflow_handle(job_id).query("job.snapshot")


def decide_req(gate: dict[str, Any], decision: str, actor: str, channel: str, n: int, **extra: Any) -> dict[str, Any]:
    return {"schema": "replicant.msg.gate-decide/v1", "decision_id": ids.derived("dec", channel, f"{gate['gate_id']}:{n}"),
            "gate_id": gate["gate_id"], "decision": decision, "actor": {"principal_id": actor}, "channel": channel,
            "presented_digest": gate["presented_digest"], "decided_at": _now_z(), **extra}


def _now_z() -> str:
    """control-plane-api stamps decided_at with its receipt time (V10, CCR-01-07)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def pr_event(job_id: str, event: str, n: int, *, actor_id: int = 8812345) -> dict[str, Any]:
    return {"schema": "replicant.msg.pr-event/v1", "delivery_id": f"00000000-0000-0000-0000-{n:012d}", "event": event,
            "job_id": job_id, "repo": "github.com/acme-payments/ledger-api", "pr_number": 1431,
            "actor": {"github_user_id": actor_id, "login": "mtorres-acme", "is_bot": False, "linked": True,
                      "principal_id": ALICE, "repo_permission": "write"},
            "payload_uri": f"s3://local-replicant-local-artifacts/tenants/payments/jobs/{job_id}/events/pr/{n}.json",
            "received_at": "2026-10-24T11:00:00Z"}


async def histories(client: Client, job_id: str) -> list[Any]:
    out = []
    async for wf in client.list_workflows(f"WorkflowId = '{job_id}'"):
        out.append(await client.get_workflow_handle(wf.id, run_id=wf.run_id).fetch_history())
    return out


async def replay_all(hists: list[Any]) -> list[str]:
    r = Replayer(workflows=[ReplicantJob, MaintenanceBatch, MaintenanceShard], workflow_runner=RUNNER)
    fails = []
    for h in hists:
        res = await r.replay_workflow(h, raise_on_replay_failure=False)
        if res.replay_failure is not None:
            fails.append(f"{h.workflow_id}: {res.replay_failure!r}"[:500])
    return fails


def side_effect_activity_ids(hists: list[Any]) -> list[str]:
    """Explicit logical activity IDs in scheduling order (oldest run first). A dependency hold retries a failed
    activity under the same ID (same idempotency key), so attempts that ended failed or timed out are excluded."""
    out = []
    for h in sorted(hists, key=lambda h: h.events[0].event_time.ToDatetime()):
        sched: list[tuple[int, str]] = []
        failed: set[int] = set()
        for e in h.events:
            if e.HasField("activity_task_scheduled_event_attributes"):
                aid = e.activity_task_scheduled_event_attributes.activity_id
                if not aid.isdigit():
                    sched.append((e.event_id, aid))
            elif e.HasField("activity_task_failed_event_attributes"):
                failed.add(e.activity_task_failed_event_attributes.scheduled_event_id)
            elif e.HasField("activity_task_timed_out_event_attributes"):
                failed.add(e.activity_task_timed_out_event_attributes.scheduled_event_id)
        out += [aid for eid, aid in sched if eid not in failed]
    return out
