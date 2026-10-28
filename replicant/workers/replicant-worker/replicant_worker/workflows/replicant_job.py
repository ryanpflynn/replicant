"""ReplicantJob workflow (C-IDS workflow type `ReplicantJob`). One per job; workflow ID == job_id.

Reference implementation of spec/01-orchestration: handlers and validators follow C-MSG 2.0.0
exactly; projection emission follows C-STATE section 2; IDs follow C-IDS. Determinism rules:
temporal-langgraph.md section 5.

This module keeps ReplicantJobInput, the construction and main loop of ReplicantJobBase, and the registered
ReplicantJob class (every @workflow.* registration). The rest of ReplicantJobBase lives in mixins under
workflows/job/, one module per concern:

    job/prologue.py    harness jobs, router prologue          job/can.py         Continue-As-New, migration prologue
    job/nodes.py       node execution, helpers used by nodes  job/terminal.py    compensation, terminal transition
    job/waits.py       gate and PR waits                      job/activities.py  activity calls, holds, kill switches,
    job/projection.py  projection events, JobSnapshot                            deadlines, notices
    job/updates.py     Update handlers and validators         job/signals.py     Signal handlers
    job/queries.py     Query handlers                         job/common.py      constants and pure helpers
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar

from temporalio import workflow
from temporalio.common import VersioningBehavior
from temporalio.exceptions import ActivityError

from replicant_worker.graphs.dsl import Guard
from replicant_worker.graphs.engines.base import BoundaryReached, EngineResult
from replicant_worker.graphs.graphspec import GraphSpec
from replicant_worker.registry import NodeFailed
from replicant_worker.workflows.state import TERMINAL_STATUS_FOR_OUTCOME, JobState
from replicant_worker import nodes as _nodes  # noqa: F401  (registers core node types)
from replicant_worker.workflows.job.activities import ActivitiesMixin
from replicant_worker.workflows.job.can import CanMixin
from replicant_worker.workflows.job.common import _Can, _etype, _evaluation, _PrologueInterrupt, _summary, _target
from replicant_worker.workflows.job.common import steering_backlog_input  # noqa: F401  (re-exported for callers)
from replicant_worker.workflows.job.nodes import NodesMixin
from replicant_worker.workflows.job.projection import ProjectionMixin
from replicant_worker.workflows.job.prologue import PrologueMixin
from replicant_worker.workflows.job.queries import QueriesMixin
from replicant_worker.workflows.job.signals import SignalsMixin
from replicant_worker.workflows.job.terminal import TerminalMixin
from replicant_worker.workflows.job.updates import UpdatesMixin
from replicant_worker.workflows.job.waits import WaitsMixin


@dataclass
class ReplicantJobInput:
    mode: str = "start"  # start | continue | migration_handoff
    job_id: str = ""
    tenant_id: str = ""
    release_bundle_id: str = ""
    job_request: dict[str, Any] | None = None
    carry: dict[str, Any] | None = None
    handoff: dict[str, Any] | None = None  # {target_build_id, target_release_bundle_id}
    reason: str | None = None
    progress_workflow_id: str | None = None  # where batch.child_progress goes: the batch or its shard (R1V-006)


class ReplicantJobBase(PrologueMixin, NodesMixin, WaitsMixin, CanMixin, TerminalMixin, ActivitiesMixin,
                       ProjectionMixin, UpdatesMixin, SignalsMixin, QueriesMixin):
    """Engine-agnostic workflow body. Concrete subclasses bind `engine_cls` (InterpreterEngine)."""

    engine_cls: ClassVar[type]

    # ============================================================ construction
    def __init__(self, inp: ReplicantJobInput) -> None:
        if inp.mode == "start":
            jr = inp.job_request or {}
            self.s = JobState(
                job_id=inp.job_id, tenant_id=inp.tenant_id, release_bundle_id=inp.release_bundle_id,
                worker_build_id=workflow.info().get_current_build_id() or "",
                requester=jr["requester"]["principal_id"], priority=jr.get("priority", "normal"),
                title=jr["intent"]["title"][:256], targets=[_target(t) for t in jr.get("targets", [])],
                request=jr, request_summary=_summary(jr),
                parent_job_id=(jr.get("related") or {}).get("parent_job_id"),
                evaluation=_evaluation(jr), acting_for=jr["requester"].get("on_behalf_of") or jr["requester"]["principal_id"],
                repo_ids=[t["repo"]["repo_id"] for t in jr.get("targets", [])][:5],
                created_at=workflow.now().isoformat(), progress_workflow_id=inp.progress_workflow_id,
            )
        else:
            self.s = JobState.from_carry(inp.carry or {})
        self.inp = inp
        self.hints: dict[str, bool] = {}
        self.cancel_requested = False
        self._takeover: dict[str, Any] | None = None
        self._cancel_actor: str | None = None
        self._wake = 0
        self._locks: dict[str, asyncio.Lock] = {}
        self._proj: list[Any] = []
        self._bundle: dict[str, Any] = {}
        self._compose: dict[str, Any] = {}
        self._spec: GraphSpec | None = None
        self._engine: Any = None
        self._guards: dict[str, Guard] = {}
        self._main: asyncio.Task | None = None
        self._reroute: tuple[str, str] | None = None
        self._prologue_resume: Any = None
        self._ready = False
        self._sa_pending: dict[str, Any] = {}  # coalesced search attribute updates (one upsert per activation)
        self._audit: list[Any] = []  # approval-record / gate-resolution activities and audit-bearing projections (R2-013)
        self._notice_lock = asyncio.Lock()
        self._sha_pending = 0
        self._unknown_signals = workflow.metric_meter().create_counter(  # replay-safe (R1-030, C-IDS 11)
            "replicant_workflow_unknown_run_signals_total", "run.status_changed Signals for runs this job does not own")

    # ============================================================ main
    async def _run(self, inp: ReplicantJobInput) -> dict[str, Any]:
        if inp.mode == "migration_handoff":
            await self._migration_prologue(inp)
        asyncio.create_task(self._sa_flusher())
        if self.cancel_requested:  # cancelled before the drive task started
            res: Any = await self._compensate()
        else:
            self._main = asyncio.create_task(self._drive())
            try:
                res = await self._main
            except asyncio.CancelledError:  # job.cancel / job.takeover Update, or native Temporal cancel
                if not self._main.done():
                    self._main.cancel()
                try:
                    res = await self._compensate()
                except NodeFailed as f:  # compensation never fails the execution
                    res = ("taken_over" if self._takeover else (self.s.forced_outcome or "cancelled_by_user"),
                           f"compensation incomplete: {f.reason}"[:2000])
            except NodeFailed as f:
                outcome = f.reason if f.reason in TERMINAL_STATUS_FOR_OUTCOME else "node_failed"
                res = (outcome, f"{f.reason}: {f.message}"[:2000])
                await self._compensate_failure()
            except ActivityError as e:  # safety net: every call goes through call(), so this is a bug path
                res = ("infrastructure_failure", f"{_etype(e)}: {e}"[:2000])
                await self._compensate_failure()
        if isinstance(res, _Can):
            await self._continue_as_new(res)
        outcome, reason = res
        outcome = await self._finish(outcome, reason)
        return {"job_id": self.s.job_id, "status": self.s.status, "outcome": outcome}

    async def _sa_flusher(self) -> None:
        """Coalesce search-attribute updates: every emit() and status change queues its value and this
        task issues one UpsertWorkflowSearchAttributes command per workflow activation (measured: 335
        upserts per 5-cycle job without coalescing; history-budget.md)."""
        while True:
            await workflow.wait_condition(lambda: bool(self._sa_pending))
            self._flush_sa()

    def _flush_sa(self) -> None:
        if self._sa_pending:
            updates = list(self._sa_pending.values())
            self._sa_pending.clear()
            workflow.upsert_search_attributes(updates)

    def _queue_sa(self, *updates: Any) -> None:
        for u in updates:
            self._sa_pending[u.key.name] = u

    async def _drive(self) -> Any:
        self._bundle = await self.call("registry.load_bundle_core", {"release_bundle_id": self.s.release_bundle_id})
        self._ready = True
        if self.inp.mode == "start":  # kill switches engaged before this job started (C-MSG 1.5.0, R2-016)
            ks = await self.call("ops.read_kill_switches", {"tenant_id": self.s.tenant_id,
                                 "repos": [t["repo"] for t in self.s.targets], "release_bundle_id": self.s.release_bundle_id})
            for sw in ks.get("engaged", []):
                self._apply_kill_switch(sw)
        ht = (self.s.evaluation or {}).get("harness_task")
        if ht:  # XD-FIX-R1-134: harness job, no router and no graph
            return await self._harness(ht)
        if self.s.workflow_type == "unrouted":
            self._set_status("running", "routing")
            while True:
                try:
                    await self._route()
                    break
                except _PrologueInterrupt as i:
                    r = await self._wait(i.payload)
                    if isinstance(r, _Can):
                        return r
                    self._prologue_resume = r
        self._compose = await self.call(
            "graph.compose", {"release_bundle_id": self.s.release_bundle_id, "workflow_type": self.s.workflow_type,
                              "repo": self.s.targets[0]["repo"] if self.s.targets else None,
                              # MEM: label-selected graph variants (repo scan, DEVIATIONS MEM-D7)
                              "labels": list(self.s.request_summary.get("labels") or [])})
        self._spec = GraphSpec.from_json(self._compose["spec"])
        if self.s.graph_spec_digest and self.s.graph_spec_digest != self._compose["composed_digest"] \
                and self.inp.reason not in ("reroute", "migration"):
            raise NodeFailed("graph_digest_mismatch", "composed graph changed under a pinned bundle")
        self.s.graph_spec_digest = self._compose["composed_digest"]
        sup = ((self.s.request or {}).get("related") or {}).get("supersedes_job_id")
        if sup and "plan_reuse" not in self.s.child_counters:  # XD-FIX-R2-050 restart from the approved plan
            self.s.child_counters["plan_reuse"] = 1
            r = await self.call("artifacts.resolve_superseded_plan", {"job_id": self.s.job_id, "tenant_id": self.s.tenant_id,
                                "supersedes_job_id": sup})
            if r.get("plan_uri") and r.get("plan_approved"):
                self.s.reused_plan_uri = r["plan_uri"]
                self.emit("node.plan_reused", f"approved plan of {sup[:16]} reused; planning skipped")
            else:
                self.emit("plan.reuse_unavailable", "the superseded job's approved plan is unavailable; planning normally")
        self._engine = self.engine_cls(self)
        self._engine.compile(self._spec)
        start_at = self.s.resume_at or self._spec.entry
        res: EngineResult | None = None
        resume_value: Any = None
        while True:
            try:
                res = await (self._engine.resume(resume_value) if res is not None else self._engine.start(start_at))
            except BoundaryReached as b:
                return _Can(b.kind, b.node_key)
            if res.kind == "terminal":
                return (res.outcome, None)
            self.s.resume_at = res.node_key
            r = await self._wait(res.payload or {})
            if isinstance(r, _Can):
                return r
            resume_value = r


# ---------------------------------------------------------------- the registered workflow class
def _engine_cls() -> type:
    from replicant_worker.graphs.engines import InterpreterEngine
    return InterpreterEngine


@workflow.defn(name="ReplicantJob", versioning_behavior=VersioningBehavior.PINNED)
class ReplicantJob(ReplicantJobBase):
    @workflow.init
    def __init__(self, inp: ReplicantJobInput) -> None:
        ReplicantJobBase.__init__(self, inp)

    @workflow.run
    async def run(self, inp: ReplicantJobInput) -> dict[str, Any]:
        return await self._run(inp)

    @workflow.update(name="gate.decide")
    async def u_gate_decide(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self.gate_decide(req)

    @u_gate_decide.validator
    def v_gate_decide(self, req: dict[str, Any]) -> None:
        self.validate_gate_decide(req)

    @workflow.update(name="job.cancel")
    async def u_cancel(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self.job_control(req)

    @u_cancel.validator
    def v_cancel(self, req: dict[str, Any]) -> None:
        self.validate_job_control(req)

    @workflow.update(name="job.pause")
    async def u_pause(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self.job_control(req)

    @u_pause.validator
    def v_pause(self, req: dict[str, Any]) -> None:
        self.validate_job_control(req)

    @workflow.update(name="job.resume")
    async def u_resume(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self.job_control(req)

    @u_resume.validator
    def v_resume(self, req: dict[str, Any]) -> None:
        self.validate_job_control(req)

    @workflow.update(name="job.takeover")
    async def u_takeover(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self.job_control(req)

    @u_takeover.validator
    def v_takeover(self, req: dict[str, Any]) -> None:
        self.validate_job_control(req)

    @workflow.update(name="job.steer")
    async def u_steer(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self.job_steer(req)

    @u_steer.validator
    def v_steer(self, req: dict[str, Any]) -> None:
        self.validate_job_steer(req)

    @workflow.update(name="job.offer_bundle_migration")
    async def u_offer(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self.offer_bundle_migration(req)

    @u_offer.validator
    def v_offer(self, req: dict[str, Any]) -> None:
        self.validate_offer_bundle_migration(req)

    @workflow.update(name="ops.retry_node")
    async def u_retry_node(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self.retry_node(req)

    @u_retry_node.validator
    def v_retry_node(self, req: dict[str, Any]) -> None:
        self.validate_retry_node(req)

    @workflow.signal(name="run.status_changed")
    def s_run(self, sig: dict[str, Any]) -> None:
        self.on_run_status_changed(sig)

    @workflow.signal(name="pr.event")
    def s_pr(self, sig: dict[str, Any]) -> None:
        self.on_pr_event(sig)

    @workflow.signal(name="release.notice")
    def s_notice(self, sig: dict[str, Any]) -> None:
        self.on_release_notice(sig)

    @workflow.signal(name="identity.principal_changed")
    def s_principal(self, sig: dict[str, Any]) -> None:
        self.on_principal_changed(sig)

    @workflow.signal(name="ops.kill_switch")
    def s_kill(self, sig: dict[str, Any]) -> None:
        self.on_kill_switch(sig)

    @workflow.query(name="job.snapshot")
    def q_snapshot(self) -> dict[str, Any]:
        return self.snapshot()

    @workflow.query(name="job.open_gates")
    def q_gates(self) -> dict[str, Any]:
        return self.q_open_gates()

    @workflow.query(name="job.working_memory_refs")
    def q_refs(self) -> dict[str, Any]:
        return self.q_working_memory_refs()


ReplicantJob.engine_cls = _engine_cls()
