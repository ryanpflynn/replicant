"""MaintenanceBatch and MaintenanceShard workflows (C-IDS 2.0.0 workflow types; ADR-01-007, XD-W3-R1V-006).

A batch resolves its target selector into an S3 repository list (`children_index_uri`, never into history).
Up to `shard_size` (2,000; C-ARCH 2.0.0 §4.1) repositories it fans out child ReplicantJobs itself with a sliding
window. Above that it starts internal `MaintenanceShard` children (`<job_id>:shard:<n>`, task queue `replicant-jobs`,
ParentClosePolicy.REQUEST_CANCEL, at most `shard_size` repositories each). Each shard runs the same sliding window
for its index range with its share of the batch concurrency, receives its children's `batch.child_progress`
Signals, continues-as-new on its own thresholds, and returns per-status counts that the batch aggregates.
Children are created through `intake.create_child_job` with ParentClosePolicy.ABANDON.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import Priority, SearchAttributeKey, SearchAttributePair, TypedSearchAttributes, VersioningBehavior
from temporalio.exceptions import ApplicationError

from replicant_worker import ids
from replicant_worker.activities.options import CATALOG, PROJECTION

ACTIVE = {"queued", "running", "waiting_human", "paused"}  # waiting_external does not hold a slot (ADR-009)
TERMINAL = {"succeeded", "failed", "cancelled", "rejected", "expired", "taken_over"}
SA_JOB = SearchAttributeKey.for_keyword("ReplicantJobId")
SA_TENANT = SearchAttributeKey.for_keyword("ReplicantTenantId")
SA_PARENT = SearchAttributeKey.for_keyword("ReplicantParentJobId")
SA_BUNDLE = SearchAttributeKey.for_keyword("ReplicantReleaseBundleId")
SA_TYPE = SearchAttributeKey.for_keyword("ReplicantWorkflowType")
SA_STATUS = SearchAttributeKey.for_keyword("ReplicantStatus")
SA_PHASE = SearchAttributeKey.for_keyword("ReplicantPhase")  # C-IDS 2.0.0: also set on MaintenanceBatch (R1V-085)
SA_SEQ = SearchAttributeKey.for_int("ReplicantProjectionSeq")
SPEND_CHECK_EVERY = 10  # cost.batch_spend is read before every 10th child start and on each wave (R2-062)
SHARD_SIZE_MAX = 2000  # C-ARCH 2.0.0 §4.1 / XD-W3-R1V-006
MAX_REPOS = 15000  # C-JOBREQ 2.0.0 target_selector.max_repos
PAGE = 100  # repositories read from children_index_uri per catalog.read_targets call


def child_activity_id(i: int) -> str:
    """R1-011: the C-IDS ordinal is 1..999 (at most 3 digits), so the child index spreads over the cycle field:
    index i -> batch.c<i // 999>.n<i % 999 + 1>.create_child (unique for up to 999,000 children)."""
    return ids.logical_activity_id("batch", i // 999, i % 999 + 1, "create_child")


def _opts(name: str) -> dict[str, Any]:
    d = CATALOG[name].klass
    return {"task_queue": d.task_queue, "start_to_close_timeout": d.start_to_close,
            "schedule_to_close_timeout": d.schedule_to_close, "retry_policy": d.retry}


@dataclass
class FanState:
    """State shared by a batch that fans out itself and by a shard (carried across Continue-As-New)."""
    job_id: str  # the batch's job_id (children's parent_job_id; cost ledger key)
    tenant_id: str
    release_bundle_id: str
    request: dict[str, Any]
    max_concurrency: int = 10
    index_uri: str | None = None  # children_index_uri (S3 list of repositories)
    total: int | None = None  # repositories in the whole batch
    lo: int = 0  # this workflow's index range [lo, hi)
    hi: int = 0
    next_index: int = 0
    page_lo: int = -1
    page: list[dict[str, Any]] = field(default_factory=list)
    children: dict[str, str] = field(default_factory=dict)  # child job_id -> latest status
    seqs: dict[str, int] = field(default_factory=dict)
    cancel_requested: bool = False
    applied: list[str] = field(default_factory=list)
    segment: int = 0
    max_batch_usd: float = 0.0
    spent_usd: float = 0.0
    stopped_reason: str | None = None
    spend_checks: int = 0
    # batch only
    status: str = "running"
    phase: str = "fan_out"
    projection_seq: int = 0
    shard_size: int = SHARD_SIZE_MAX
    shards: dict[str, dict[str, Any]] = field(default_factory=dict)  # shard workflow ID -> result (or {})
    created_at: str | None = None


BatchState = FanState  # name kept for the carry and older references


@dataclass
class BatchInput:
    job_id: str
    tenant_id: str
    release_bundle_id: str
    job_request: dict[str, Any] | None = None
    carry: dict[str, Any] | None = None


@dataclass
class ShardInput:
    batch_job_id: str
    tenant_id: str
    release_bundle_id: str
    job_request: dict[str, Any]
    index_uri: str
    total: int
    lo: int
    hi: int
    max_concurrency: int
    max_batch_usd: float = 0.0
    carry: dict[str, Any] | None = None


class _FanOut:
    """The sliding-window fan-out over [lo, hi) used by both workflow types."""

    s: FanState

    def _init_fan(self) -> None:
        self._changed = 0

    def _counts(self) -> dict[str, int]:
        by: dict[str, int] = {}
        for st in self.s.children.values():
            by[st] = by.get(st, 0) + 1
        return by

    async def _target(self, i: int, prio: Priority) -> dict[str, Any]:
        s = self.s
        if not (s.page_lo <= i < s.page_lo + len(s.page)):
            r = await workflow.execute_activity("catalog.read_targets", {
                "job_id": s.job_id, "tenant_id": s.tenant_id, "children_index_uri": s.index_uri, "offset": i,
                "limit": min(PAGE, s.hi - i)}, priority=prio, **_opts("catalog.read_targets"))
            s.page_lo, s.page = i, list(r["repos"])
        return s.page[i - s.page_lo]

    async def _spend_ok(self, prio: Priority) -> bool:
        """R2-062: stop starting children once the batch's children have spent max_batch_usd (cost ledger,
        summed by parent_job_id; WS-03 XD-FIX-R2-062). Running children finish under their own job budgets."""
        s = self.s
        s.spend_checks += 1
        r = await workflow.execute_activity(
            "cost.batch_spend", {"batch_job_id": s.job_id, "tenant_id": s.tenant_id}, priority=prio,
            **_opts("cost.batch_spend"))
        s.spent_usd = float(r["spent_usd"])
        if s.spent_usd >= s.max_batch_usd:
            s.stopped_reason = "batch_spend_cap"
            self._on_progress("notice.received", f"batch spend cap reached (${s.spent_usd:.0f} of "
                              f"${s.max_batch_usd:.0f}); no new children start")
            return False
        return True

    def _on_progress(self, event_type: str, summary: str) -> None:  # the batch projects; shards do not
        pass

    def _continue_as_new(self) -> None:
        raise NotImplementedError

    async def _fan(self, prio: Priority) -> None:
        s = self.s
        while True:
            started = 0
            while (not s.cancel_requested and s.stopped_reason is None and s.next_index < s.hi
                   and sum(1 for st in s.children.values() if st in ACTIVE) < s.max_concurrency):
                if s.max_batch_usd and started % SPEND_CHECK_EVERY == 0 and not await self._spend_ok(prio):
                    break
                await self._start_child(s.next_index, prio)
                s.next_index += 1
                started += 1
            self._on_progress("phase.entered", "fan-out progress " + ", ".join(
                f"{k} {v}" for k, v in sorted(self._counts().items())))
            exhausted = s.next_index >= s.hi or s.stopped_reason is not None
            if (exhausted or s.cancel_requested) and all(st in TERMINAL for st in s.children.values()):
                return
            info = workflow.info()
            if info.is_continue_as_new_suggested() or info.get_current_history_length() > 8_000:
                await workflow.wait_condition(workflow.all_handlers_finished)
                s.segment += 1
                s.page, s.page_lo = [], -1  # re-read the page after Continue-As-New; keeps the carry small
                self._continue_as_new()
            seen = self._changed
            try:
                await workflow.wait_condition(lambda: self._changed != seen, timeout=timedelta(hours=6))
            except TimeoutError:
                pass

    async def _start_child(self, i: int, prio: Priority) -> None:
        s = self.s
        target = await self._target(i, prio)
        r = await workflow.execute_activity(
            "intake.create_child_job", {"parent_job_id": s.job_id, "tenant_id": s.tenant_id, "index": i,
                                        "repo": target, "release_bundle_id": s.release_bundle_id},
            activity_id=child_activity_id(i), priority=prio,
            **_opts("intake.create_child_job"))
        child_id = r["job_id"]
        jr = copy.deepcopy(s.request)
        jr.pop("target_selector", None)
        jr["targets"] = [{"repo": target, "base_ref": target.get("default_branch")}] if "host" in target else [target]
        jr["workflow_type"] = "maintenance"
        jr.setdefault("related", {})["parent_job_id"] = s.job_id
        from replicant_worker.workflows.replicant_job import ReplicantJobInput  # same build
        await workflow.start_child_workflow(
            "ReplicantJob", ReplicantJobInput(mode="start", job_id=child_id, tenant_id=s.tenant_id,
                                          release_bundle_id=s.release_bundle_id, job_request=jr,
                                          progress_workflow_id=workflow.info().workflow_id),
            id=child_id, task_queue="replicant-jobs", parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            priority=prio,
            search_attributes=TypedSearchAttributes([
                SearchAttributePair(SA_JOB, child_id), SearchAttributePair(SA_TENANT, s.tenant_id),
                SearchAttributePair(SA_PARENT, s.job_id), SearchAttributePair(SA_BUNDLE, s.release_bundle_id),
                SearchAttributePair(SA_TYPE, "maintenance"), SearchAttributePair(SA_STATUS, "queued")]))
        s.children[child_id] = "queued"

    def _progress_signal(self, sig: dict[str, Any]) -> None:
        cid, seq = sig.get("child_job_id"), int(sig.get("seq", 0))
        if cid in self.s.children and seq >= self.s.seqs.get(cid, 0):  # ignore reordered stale progress
            self.s.children[cid] = sig["status"]
            self.s.seqs[cid] = seq
            self._changed += 1

    async def _cancel_children(self) -> None:
        for cid, st in self.s.children.items():
            if st not in TERMINAL:  # native cancel; the child handles it exactly like job.cancel (ADR-001)
                await workflow.get_external_workflow_handle(cid).cancel()


@workflow.defn(name="MaintenanceBatch", versioning_behavior=VersioningBehavior.PINNED)
class MaintenanceBatch(_FanOut):
    @workflow.init
    def __init__(self, inp: BatchInput) -> None:
        if inp.carry:
            self.s = FanState(**dict(inp.carry))
        else:
            self.s = FanState(job_id=inp.job_id, tenant_id=inp.tenant_id, release_bundle_id=inp.release_bundle_id,
                              request=inp.job_request or {}, created_at=workflow.now().isoformat())
        self._init_fan()
        self._proj: list[Any] = []
        self._last_emitted: dict[str, int] = {}
        self._shard_handles: dict[str, Any] = {}

    def _continue_as_new(self) -> None:
        s = self.s
        workflow.continue_as_new(BatchInput(job_id=s.job_id, tenant_id=s.tenant_id,
                                            release_bundle_id=s.release_bundle_id, carry=asdict(s)))

    def _on_progress(self, event_type: str, summary: str) -> None:
        if event_type != "phase.entered" or self._last_emitted != self._counts():
            self.emit(event_type, summary)

    @workflow.run
    async def run(self, inp: BatchInput) -> dict[str, Any]:
        s = self.s
        prio = Priority(priority_key=5, fairness_key=s.tenant_id)
        if s.total is None:
            core = await workflow.execute_activity("registry.load_bundle_core", {"release_bundle_id": s.release_bundle_id},
                                                   priority=prio, **_opts("registry.load_bundle_core"))
            mp = core["policies"]["repair_limits"].get("maintenance", {})
            s.max_concurrency = int(mp.get("max_concurrency", 10))
            s.max_batch_usd = float(mp.get("max_batch_usd", 0) or 0)
            s.shard_size = max(1, min(SHARD_SIZE_MAX, int(mp.get("shard_size", SHARD_SIZE_MAX))))
            r = await workflow.execute_activity(
                "catalog.resolve_targets", {"job_id": s.job_id, "tenant_id": s.tenant_id,
                                            "selector": s.request.get("target_selector")},
                activity_id="batch.c0.n1.resolve_targets", priority=prio, **_opts("catalog.resolve_targets"))
            cap = min(MAX_REPOS, int((s.request.get("target_selector") or {}).get("max_repos", MAX_REPOS)))
            s.index_uri, s.total = r["children_index_uri"], min(int(r["count"]), cap)
            s.lo, s.hi, s.next_index = 0, s.total, 0
            self._set("running", "fan_out")
            self.emit("job.created", f"maintenance batch over {s.total} repositories")
        if s.total <= s.shard_size:
            await self._fan(prio)
            by = self._counts()
        else:
            by = await self._run_shards(prio)
        status = "cancelled" if s.cancel_requested else "succeeded"
        self._set(status, "done")
        stopped = s.stopped_reason or next((r.get("stopped_reason") for r in s.shards.values() if r.get("stopped_reason")), None)
        self.emit("job.terminal", f"batch {status}: " + ", ".join(f"{k} {v}" for k, v in sorted(by.items()))
                  + (f"; stopped: {stopped}" if stopped else ""))
        await workflow.wait_condition(lambda: all(h.done() for h in self._proj))
        return {"job_id": s.job_id, "children_total": sum(by.values()), "children_by_status": by, "status": status,
                **({"shards": len(s.shards)} if s.shards else {}), **({"stopped_reason": stopped} if stopped else {})}

    async def _run_shards(self, prio: Priority) -> dict[str, int]:
        """XD-W3-R1V-006: one MaintenanceShard per shard_size repositories, each with an equal share of the batch
        concurrency. The batch's own history stays small (a few events per shard), so it does not need to
        continue-as-new while it waits."""
        s = self.s
        n = -(-s.total // s.shard_size)  # type: ignore[operator]
        share = max(1, s.max_concurrency // n)
        for k in range(n):
            sid = f"{s.job_id}:shard:{k + 1}"
            if sid in s.shards and s.shards[sid]:
                continue
            lo, hi = k * s.shard_size, min(s.total, (k + 1) * s.shard_size)  # type: ignore[type-var]
            self._shard_handles[sid] = await workflow.start_child_workflow(
                "MaintenanceShard", ShardInput(batch_job_id=s.job_id, tenant_id=s.tenant_id,
                                               release_bundle_id=s.release_bundle_id, job_request=s.request,
                                               index_uri=s.index_uri, total=s.total, lo=lo, hi=hi,  # type: ignore[arg-type]
                                               max_concurrency=share, max_batch_usd=s.max_batch_usd),
                id=sid, task_queue="replicant-jobs", parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL,
                priority=prio)
            s.shards[sid] = {}
        self.emit("phase.entered", f"fan-out over {n} shards of at most {s.shard_size} repositories")
        for sid, h in list(self._shard_handles.items()):
            s.shards[sid] = await h
            self.emit("phase.entered", f"shard {sid.rsplit(':', 1)[1]} of {n} finished")
        by: dict[str, int] = {}
        for r in s.shards.values():
            for k, v in (r.get("children_by_status") or {}).items():
                by[k] = by.get(k, 0) + int(v)
        return by

    # ------------------------------------------------------------------ projection (R1-010)
    def _set(self, status: str, phase: str) -> None:
        self.s.status, self.s.phase = status, phase
        workflow.upsert_search_attributes([SA_STATUS.value_set(status), SA_PHASE.value_set(phase)])

    def emit(self, event_type: str, summary: str) -> None:
        """ProjectionEvent with a JobSnapshot (workflow_type maintenance, phase fan_out) at start, on coalesced
        child changes and shard completions, and at the end (C-STATE 2.1, C-IDS 4.1). Child jobs carry
        parent_job_id = the batch, so the projector derives jobs_read.batches counts from them (XD-FIX-R1-010)."""
        s = self.s
        s.projection_seq += 1
        self._last_emitted = self._counts()
        jr = s.request or {}
        snap = {"schema": "replicant.job-snapshot/v1", "job_id": s.job_id, "tenant_id": s.tenant_id,
                "seq": s.projection_seq, "as_of": workflow.now().isoformat(), "workflow_type": "maintenance",
                "status": s.status, "phase": s.phase, "release_bundle_id": s.release_bundle_id,
                "requester": (jr.get("requester") or {}).get("principal_id", "svc_scheduler"),
                "title": str((jr.get("intent") or {}).get("title", "maintenance batch"))[:256], "priority": "low",
                "targets": [], "cycle": 0, "gates": [], "runs": [], "paused": False}
        event = {"schema": "replicant.projection-event/v1", "job_id": s.job_id, "tenant_id": s.tenant_id,
                 "seq": s.projection_seq, "workflow_run_id": workflow.info().run_id,
                 "event": {"type": event_type, "at": workflow.now().isoformat(), "summary": summary[:300]},
                 "snapshot": snap}
        workflow.upsert_search_attributes([SA_SEQ.value_set(s.projection_seq)])
        h = workflow.start_activity("projection.apply", event, task_queue=PROJECTION.task_queue,
                                    schedule_to_close_timeout=PROJECTION.schedule_to_close,
                                    start_to_close_timeout=PROJECTION.start_to_close, retry_policy=PROJECTION.retry)
        self._proj = [p for p in self._proj if not p.done()] + [h]

    # ------------------------------------------------------------------ messages
    @workflow.signal(name="batch.child_progress")
    def child_progress(self, sig: dict[str, Any]) -> None:
        self._progress_signal(sig)

    @workflow.update(name="batch.cancel")
    async def batch_cancel(self, req: dict[str, Any]) -> dict[str, Any]:
        check = await workflow.execute_activity("identity.check_action", {
            "job_id": self.s.job_id, "tenant_id": self.s.tenant_id, "action": "batch_cancel",
            "principal_id": req["actor"]["principal_id"]}, **_opts("identity.check_action"))
        self.s.applied.append(req["action_id"])
        if not check["allow"]:
            return {"schema": "replicant.msg.job-control-result/v1", "outcome": "denied", "action_id": req["action_id"],
                    "status": "running", "denial_reason": check["reason"][:200]}
        self.s.cancel_requested = True
        await self._cancel_children()
        for sid, r in self.s.shards.items():
            if not r:  # a running shard cancels its own children
                await workflow.get_external_workflow_handle(sid).cancel()
        self._changed += 1
        return {"schema": "replicant.msg.job-control-result/v1", "outcome": "accepted", "action_id": req["action_id"],
                "status": "cancelled"}

    @batch_cancel.validator
    def v_batch_cancel(self, req: dict[str, Any]) -> None:
        if req.get("schema") != "replicant.msg.job-control/v1" or req.get("action") != "batch_cancel":
            raise ApplicationError("payload", type="InvalidPayload", non_retryable=True)
        if req.get("action_id") in self.s.applied:
            raise ApplicationError("duplicate", type="DuplicateAction", non_retryable=True)
        if not str((req.get("actor") or {}).get("principal_id", "")).startswith("usr_"):
            raise ApplicationError("human only", type="ActorNotHuman", non_retryable=True)

    @workflow.query(name="batch.snapshot")
    def batch_snapshot(self) -> dict[str, Any]:
        by = self._counts()
        for r in self.s.shards.values():
            for k, v in (r.get("children_by_status") or {}).items():
                by[k] = by.get(k, 0) + int(v)
        return {"schema": "replicant.batch-snapshot/v1", "job_id": self.s.job_id, "children_total": self.s.total or 0,
                "children_by_status": by, "max_concurrency": self.s.max_concurrency}


@workflow.defn(name="MaintenanceShard", versioning_behavior=VersioningBehavior.PINNED)
class MaintenanceShard(_FanOut):
    """Internal child of a batch above 2,000 repositories (C-IDS 2.0.0, C-MSG 2.0.0 batch.child_progress)."""

    @workflow.init
    def __init__(self, inp: ShardInput) -> None:
        if inp.carry:
            self.s = FanState(**dict(inp.carry))
        else:
            self.s = FanState(job_id=inp.batch_job_id, tenant_id=inp.tenant_id, release_bundle_id=inp.release_bundle_id,
                              request=inp.job_request, max_concurrency=inp.max_concurrency, index_uri=inp.index_uri,
                              total=inp.total, lo=inp.lo, hi=inp.hi, next_index=inp.lo, max_batch_usd=inp.max_batch_usd)
        self._inp = inp
        self._init_fan()

    def _continue_as_new(self) -> None:
        i = self._inp
        workflow.continue_as_new(ShardInput(batch_job_id=i.batch_job_id, tenant_id=i.tenant_id,
                                            release_bundle_id=i.release_bundle_id, job_request=i.job_request,
                                            index_uri=i.index_uri, total=i.total, lo=i.lo, hi=i.hi,
                                            max_concurrency=i.max_concurrency, max_batch_usd=i.max_batch_usd,
                                            carry=asdict(self.s)))

    @workflow.run
    async def run(self, inp: ShardInput) -> dict[str, Any]:
        prio = Priority(priority_key=5, fairness_key=self.s.tenant_id)
        try:
            await self._fan(prio)
        except asyncio.CancelledError:  # the batch was cancelled (REQUEST_CANCEL): cancel this shard's children
            self.s.cancel_requested = True
            await self._cancel_children()
            raise
        return {"lo": self.s.lo, "hi": self.s.hi, "children_by_status": self._counts(),
                **({"stopped_reason": self.s.stopped_reason} if self.s.stopped_reason else {})}

    @workflow.signal(name="batch.child_progress")
    def child_progress(self, sig: dict[str, Any]) -> None:
        self._progress_signal(sig)
