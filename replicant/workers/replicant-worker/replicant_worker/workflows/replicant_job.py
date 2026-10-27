"""ReplicantJob workflow (C-IDS workflow type `ReplicantJob`). One per job; workflow ID == job_id.

Reference implementation of spec/01-orchestration: handlers and validators follow C-MSG 2.0.0
exactly; projection emission follows C-STATE section 2; IDs follow C-IDS. Determinism rules:
temporal-langgraph.md section 5.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar

from temporalio import workflow
from temporalio.common import Priority, SearchAttributeKey, VersioningBehavior
from temporalio.exceptions import ActivityError, ApplicationError, RetryState

from replicant_worker import ids
from replicant_worker.activities.options import CATALOG, PROJECTION
from replicant_worker.graphs.dsl import Guard, _risk_at_least
from replicant_worker.graphs.engines.base import BoundaryReached, EngineResult
from replicant_worker.graphs.graphspec import GraphSpec
from replicant_worker.registry import NodeFailed
from replicant_worker.workflows.state import TERMINAL_STATUS_FOR_OUTCOME, GateRecord, JobState
from replicant_worker import nodes as _nodes  # noqa: F401  (registers core node types)
from replicant_worker.workflows.context import NodeContext
from replicant_worker.registry import get as get_node_type

# ---------------------------------------------------------------- search attributes (C-IDS 4.1)
SA_JOB = SearchAttributeKey.for_keyword("ReplicantJobId")
SA_TENANT = SearchAttributeKey.for_keyword("ReplicantTenantId")
SA_TYPE = SearchAttributeKey.for_keyword("ReplicantWorkflowType")
SA_STATUS = SearchAttributeKey.for_keyword("ReplicantStatus")
SA_PHASE = SearchAttributeKey.for_keyword("ReplicantPhase")
SA_BUNDLE = SearchAttributeKey.for_keyword("ReplicantReleaseBundleId")
SA_RISK = SearchAttributeKey.for_keyword("ReplicantRiskLevel")
SA_REPOS = SearchAttributeKey.for_keyword_list("ReplicantRepos")
SA_OPEN_GATES = SearchAttributeKey.for_keyword_list("ReplicantOpenGateIds")
SA_CYCLE = SearchAttributeKey.for_int("ReplicantRepairCycle")
SA_SEQ = SearchAttributeKey.for_int("ReplicantProjectionSeq")
SA_WAITING = SearchAttributeKey.for_datetime("ReplicantWaitingSince")
SA_HUMAN = SearchAttributeKey.for_bool("ReplicantAwaitingHuman")

# History ceiling: build_config.HISTORY_CAN_EVENTS (default 8,000) plus the server's suggestion (4,096 / 4 MiB)
PROJECTION_DRAIN = timedelta(minutes=10)  # C-STATE section 2.1
PR_DEBOUNCE = timedelta(seconds=120)  # C-MSG pr.event default
PR_WAIT_EXPIRY = timedelta(days=30)  # ADR-009
PAUSE_EXPIRY = timedelta(days=30)
# R2-014: gate expiry versus decisions delayed by an outage
DECISION_SKEW = timedelta(seconds=60)  # tolerated clock skew of control-plane-api's decided_at
LATE_FIRE = timedelta(seconds=60)  # an expiry timer that fires later than this was delayed by an outage
EXPIRY_GRACE = timedelta(minutes=15)  # then decisions stamped before expires_at may still arrive
MAX_DECISION_DELAY = timedelta(hours=6)  # outbox delivery delay accepted for a decision made before expiry
HANDOFF_ROUTING_TIMEOUT = timedelta(minutes=30)  # Migration Handoff Protocol v1 (R2-015)
DEPENDENCY_BLOCK_DEFAULTS = {"probe_initial_seconds": 60, "probe_max_seconds": 900, "jitter_seconds": 120,
                             "max_hours": 72}
AUDIT_EVENT_PREFIXES = ("gate.", "job.terminal", "bundle.")  # drained without the 10-minute cap (R2-013)

_ID = {k: re.compile(v) for k, v in {
    "decision_id": r"^dec_[0-9a-hjkmnp-tv-z]{26}$", "gate_id": r"^gate_[0-9a-hjkmnp-tv-z]{26}$",
    "message_id": r"^msg_[0-9a-hjkmnp-tv-z]{26}$", "action_id": r"^act_[0-9a-hjkmnp-tv-z]{26}$",
    "operation_id": r"^opr_[0-9a-hjkmnp-tv-z]{26}$",
    "principal": r"^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$",
    "digest": r"^sha256:[0-9a-f]{64}$", "bundle": r"^rb_[0-9a-hjkmnp-tv-z]{26}$",
}.items()}
DECISIONS_BY_KIND = {
    "plan_approval": {"approve", "reject", "request_changes"},
    "publish_approval": {"approve", "reject", "request_changes", "rerun_qa"},  # rerun_qa: CCR-05-04
    "clarification": {"answer", "reject"},
    "budget_extension": {"approve", "reject"},
    "bundle_migration": {"approve", "reject"},
}
# deadline kind (release.notice kind) -> terminal outcome when the job has not migrated in time
DEADLINE_OUTCOME = {"bundle_revoked": "bundle_revoked_not_migrated",
                    "worker_version_retire_scheduled": "worker_retired_not_migrated",
                    "model_retiring": "model_retired_not_migrated"}
GATE_STATE_FOR = {"approve": "approved", "reject": "rejected", "request_changes": "changes_requested",
                  "answer": "answered", "rerun_qa": "cancelled"}  # rerun_qa supersedes the revision (C-MSG 1.6.0)
LOG_CHANNELS = {"ui", "cli", "slack", "github", "gitea"}  # GateView.decision_log channels (CCR-07-07)
PUBLISH_NODE_TYPES = ("publisher",)
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "rejected", "expired", "taken_over"}


def _reject(kind: str, msg: str, *details: Any) -> ApplicationError:
    return ApplicationError(msg, *details, type=kind, non_retryable=True)


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


@dataclass
class _Can:
    kind: str
    resume_at: str | None


class _PrologueInterrupt(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("prologue interrupt")
        self.payload = payload


class ReplicantJobBase:
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
                              "repo": self.s.targets[0]["repo"] if self.s.targets else None})
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

    # ============================================================ harness jobs (XD-FIX-R1-134, eval-harness.md §3.8)
    async def _harness(self, ht: dict[str, Any]) -> tuple[str, str | None]:
        """Run the harness steps of options.evaluation.harness_task in order: wait (up to 15 min) for the
        inputs manifest eval-worker staged at the precomputed run prefix, then the normal Runner-call sequence
        (grant signed in core) with role `eval`. A step's run outcome never fails the job; only infrastructure
        errors do (NodeFailed from the activity wrapper)."""
        s = self.s
        if s.workflow_type == "unrouted":
            s.workflow_type = s.request_summary.get("workflow_type") or "qa_only"
            self._queue_sa(SA_TYPE.value_set(s.workflow_type))
        self._set_status("running", "qa")
        hctx = NodeContext(self, "harness", 1, lambda p: (_ for _ in ()).throw(RuntimeError("harness steps never wait")))
        for i, t in enumerate(s.targets):
            if not t.get("base_sha"):
                r = await hctx.act("scm.resolve_base", {"repo": t["repo_ref"], "base_ref": t.get("base_ref")},
                                   effect=f"resolve_base_{i}")
                t["base_ref"], t["base_sha"] = r["base_ref"], r["base_sha"]
        spec = await self.call("eval.read_harness_spec", {"job_id": s.job_id, "tenant_id": s.tenant_id,
                               "kind": ht["kind"], "spec_uri": ht["spec_uri"]})
        done = 0
        for step in spec["steps"]:
            ctx = NodeContext(self, step["node_key"], int(step["ordinal"]), hctx._interrupt_fn)
            lid = ctx.lid("run")
            run_id = ids.run_id_for(s.tenant_id, ids.idempotency_key(workflow.info().workflow_id, lid))
            await self.call("eval.await_inputs", {"job_id": s.job_id, "tenant_id": s.tenant_id, "run_id": run_id,
                            "timeout_seconds": 900})
            s.active_nodes[step["node_key"]] = {"ordinal": int(step["ordinal"]), "started_at": workflow.now().isoformat()}
            self.emit("node.started", f"harness step {step['node_key']} n{step['ordinal']}", node_key=step["node_key"])
            out = await ctx.run_agent(role="eval", agent=step["agent"], environment=step["environment"],
                                      workload_class=step.get("workload_class", "utility"),
                                      network_profile=step["network_profile"], inputs={"harness_inputs": True},
                                      raise_on_failure=False)
            s.active_nodes.pop(step["node_key"], None)
            s.exports[f"{step['node_key']}_n{step['ordinal']}"] = {"outcome": out.outcome or out.phase, "run_id": out.run_id}
            self.emit("node.completed", f"harness step {step['node_key']}: {out.outcome or out.phase}",
                      node_key=step["node_key"])
            done += 1
        return ("harness_completed", f"{done} harness steps ran")

    # ============================================================ router prologue (router.md)
    async def _route(self) -> None:
        def interrupt_fn(payload: dict[str, Any]) -> Any:
            if self._prologue_resume is not None:
                v, self._prologue_resume = self._prologue_resume, None
                return v
            raise _PrologueInterrupt({**payload, "node_key": "router"})

        ctx = NodeContext(self, "router", 1, interrupt_fn)
        req = dict(self.s.request or {})
        decision = await ctx.act("router.classify", {"request": req, "router_rules": self.policy("router_rules")},
                                 effect="classify")
        attempt = 0
        while decision.get("needs_intent"):
            attempt += 1
            ans = self.s.clarification_answer
            try:  # router.md: the model being unavailable never blocks or fails intake; use the fallback type
                intent = await ctx.act("router.parse_intent", {
                    "request_id": req.get("request_id"), "candidates": decision["candidates"], "attempt": attempt,
                    # R1-017: the requester's answer travels in the activity input (<= 16 KiB). It is untrusted
                    # text: the activity puts it only inside a delimited untrusted-input block (G-24).
                    "clarification_answer": {"decision_id": ans["decision_id"], "text": ans["text"],
                                             "trust": "untrusted"} if ans else None},
                    effect="intent", block=False)
            except NodeFailed:
                intent = {"workflow_type": decision["fallback"], "confidence": 0.0}
                decision = {**decision, "workflow_type": decision["fallback"], "method": "fallback",
                            "needs_intent": False, "rules_fired": [*decision.get("rules_fired", []), "llm_unavailable"]}
                break
            thr = self.policy("router_rules")["thresholds"]
            if intent["confidence"] >= thr["accept"] and intent["workflow_type"] in decision["candidates"]:
                decision = {**decision, "workflow_type": intent["workflow_type"], "method": "llm",
                            "confidence": intent["confidence"], "needs_intent": False}
            elif attempt > int(thr.get("max_clarifications", 2)):
                decision = {**decision, "workflow_type": decision["fallback"], "method": "fallback", "needs_intent": False}
            else:
                self._set_status(self.s.status, "clarification")  # C-IDS phase (R1-023)
                d = await ctx.gate("clarification", packet={"questions": intent.get("questions", [])})
                if d.get("decision") != "answer":
                    raise NodeFailed("clarification_rejected", "requester declined to clarify")
                self._set_status("running", "routing")
                decision = {**decision, "clarification_answer_ref": d.get("answer_ref")}
                ctx.ordinal += 1  # next intent parse and gate are new logical steps
        wf_type = decision["workflow_type"]
        for t in self.s.targets:
            if not t.get("base_sha"):
                r = await ctx.act("scm.resolve_base", {"repo": t["repo_ref"], "base_ref": t.get("base_ref")},
                                  effect=f"resolve_base_{self.s.targets.index(t)}")
                t["base_ref"], t["base_sha"] = r["base_ref"], r["base_sha"]
        self.s.workflow_type = wf_type
        pr0 = self.s.targets[0].get("pull_request") if self.s.targets else None
        if wf_type == "pr_repair" and pr0:  # standalone repair on an existing PR (ADR-009)
            self.s.pr = {"repo": self.s.targets[0]["repo"], "number": pr0["number"], "head_ref": pr0.get("head_ref"),
                         "head_sha": pr0.get("head_sha"), "state": "open",
                         "url": f"https://{self.s.targets[0]['repo']}/pull/{pr0['number']}"}
        rl = self.policy("repair_limits")
        self.s.repair_max = int(rl["repair_cycles"].get("per_workflow_type", {}).get(wf_type, rl["repair_cycles"]["default"]))
        self.s.exports["router"] = {"outcome": wf_type, "method": decision.get("method", "rule"),
                                    "confidence": decision.get("confidence", 1.0),
                                    "rules_fired": list(decision.get("rules_fired", []))[:20]}
        self.s.memo.clear()
        self.s.clarification_answer = None  # untrusted text leaves the carry once routing is done
        self._queue_sa(SA_TYPE.value_set(wf_type), SA_REPOS.value_set([t["repo"] for t in self.s.targets]))
        self.emit("job.routed", f"routed to {wf_type} ({decision.get('method', 'rule')})")

    # ============================================================ node execution (engine callback)
    async def run_node(self, key: str, interrupt_fn: Any) -> None:
        assert self._spec is not None
        node = self._spec.nodes[key]
        reentry = key in self.s.active_nodes
        if not reentry:
            await self._safe_point(key)
        if self.s.reused_plan_uri and (node.type == "plan_run" or (node.type == "human_gate"
                                       and node.config.get("gateKind") == "plan_approval")):
            # XD-FIX-R2-050: the superseded job's approved plan stands in for planning and the plan gate
            self.s.refs["plan_uri"] = self.s.reused_plan_uri
            self.s.exports[key] = {"outcome": "completed" if node.type == "plan_run" else "approve",
                                   "decision": "approve", "required": True, "reused": True}
            self.emit("node.completed", f"{key}: reused approved plan", node_key=key)
            return
        if node.type != "human_gate" and node.when is not None and not self._guard(node.when):
            self.s.exports[key] = {"outcome": "skipped"}
            self.emit("node.completed", f"{key} skipped", node_key=key)
            return
        if not reentry:
            self.s.visits[key] = self.s.visits.get(key, 0) + 1
            if node.max_visits and self.s.visits[key] > node.max_visits:
                raise NodeFailed("loop_limit_exceeded", f"{key} visited more than {node.max_visits} times")
            self.s.active_nodes[key] = {"ordinal": self.s.next_ordinal(key), "started_at": workflow.now().isoformat()}
            if self.s.boundary_crossed == key:
                self.s.boundary_crossed = None
            self._set_status("running", node.phase)
            self.emit("node.started", f"{key} started", node_key=key)
        ordinal = self.s.active_nodes[key]["ordinal"]
        ctx = NodeContext(self, key, ordinal, interrupt_fn)
        try:
            result = await get_node_type(node.type).run(ctx, node.config)
        except NodeFailed as f:
            self.emit("node.failed", f"{key} failed: {f.reason}", node_key=key)
            raise
        self.s.exports[key] = {"outcome": result.outcome, **result.exports}
        for name, uri in result.refs.items():
            self.s.refs[name] = uri
        lid_prefix = f"{key}.c{self.s.cycle}."  # every ordinal of this node in this cycle is finished
        for k in [k for k in self.s.memo if k.startswith(lid_prefix) or k.startswith(f"gate:{key}:")]:
            del self.s.memo[k]
        del self.s.active_nodes[key]
        self.emit("node.completed", f"{key}: {result.outcome}", node_key=key)
        if self._reroute:
            wf_type, start_at = self._reroute
            self.s.workflow_type = wf_type
            raise BoundaryReached(start_at, "reroute")

    def route(self, key: str) -> list[str]:
        assert self._spec is not None
        out: list[str] = []
        for e in self._spec.outgoing(key):
            if len(e.source) > 1:
                continue
            if e.to:
                out.append(e.to)
                continue
            for b in e.switch:
                if self._guard(b.when):
                    out.append(b.to)
                    break
            else:
                out.append(e.default)  # type: ignore[arg-type]
        return out

    async def _safe_point(self, key: str) -> None:
        """Node boundary checks, in order: cancellation is handled by task cancellation; then queued
        notices, deadlines and job age, migration, revocation hold, configured boundaries, history size,
        kill switches, pause."""
        assert self._spec is not None
        await self.process_notices()
        self.check_deadlines()
        if self.s.migration and self.s.migration.get("state") == "approved":
            raise BoundaryReached(key, "migration")
        if "bundle_revoked" in self.s.deadlines:  # C-MSG: hold with status waiting_human for the required offer
            await self.hold_while(lambda: bool(self.s.migration and self.s.migration.get("state") == "approved"),
                                  kind="revoked", status="waiting_human", node_key=key,
                                  detail="release bundle revoked: waiting for the required migration")
            raise BoundaryReached(key, "migration")
        node = self._spec.nodes[key]
        for b in self._spec.boundaries:
            if b.before != key or self.s.boundary_crossed == key:
                continue
            if b.kind == "phase" and self.s.phase == node.phase:
                continue  # phase boundaries apply only when entering the phase (not on self-loops)
            raise BoundaryReached(key, b.kind)
        if self._history_heavy():
            raise BoundaryReached(key, "history")
        if self.kill_engaged("starts") or self.kill_engaged("runs"):
            await self.hold_while(lambda: not (self.kill_engaged("starts") or self.kill_engaged("runs")),
                                  kind="kill_switch", node_key=key, detail="new work held by operators")
        if self.s.paused and self.s.pause_mode == "at_next_node":
            self._set_status("paused", self.s.phase)
            self.emit("job.paused", "paused at node boundary", node_key=key)
            try:
                await workflow.wait_condition(lambda: not self.s.paused, timeout=min(PAUSE_EXPIRY, self._until_deadline()))
            except TimeoutError:
                self.check_deadlines()
                raise NodeFailed("pause_expired", "paused for 30 days") from None
            self.emit("job.resumed", "resumed", node_key=key)

    def _history_heavy(self) -> bool:
        from replicant_worker import build_config
        info = workflow.info()
        return info.is_continue_as_new_suggested() or info.get_current_history_length() > build_config.HISTORY_CAN_EVENTS

    def history_heavy(self) -> bool:
        return self._history_heavy()

    def in_parallel_branch(self, key: str) -> bool:
        return bool(self._spec) and any(len(e.source) > 1 and key in e.source for e in self._spec.edges)

    # ============================================================ waits (interrupt payloads)
    async def _wait(self, payload: dict[str, Any]) -> Any:
        if payload["wait"] == "gate":
            return await self._wait_gate(payload["gate_id"], payload["node_key"])
        if payload["wait"] == "pr_feedback":
            return await self._wait_pr(payload["node_key"])
        raise RuntimeError(f"unknown wait {payload}")

    async def _wait_gate(self, gate_id: str, node_key: str) -> Any:
        g = self.s.gates[gate_id]
        self._set_status("waiting_human", self.s.phase)
        while g.state == "open":
            await self.process_notices()
            self.check_deadlines()
            if self.s.migration and self.s.migration.get("state") == "approved" and g.kind != "bundle_migration":
                return _Can("migration", node_key)
            if self._history_heavy():
                return _Can("history", node_key)
            if g.held and not self.s.paused:  # pause at_next_gate ended: notify approvers now
                g.held = False
                if g.held_since and g.expires_at:  # R2-023: the expiry clock was suspended while held
                    held_for = workflow.now() - datetime.fromisoformat(g.held_since)
                    g.expires_at = (datetime.fromisoformat(g.expires_at) + held_for).isoformat()
                    self.emit("gate.unheld", f"{g.kind} gate released; expiry moved by {int(held_for.total_seconds())} s",
                              gate_id=g.gate_id)
                g.held_since = None
                await self._notify("notify.gate_opened", {"job_id": self.s.job_id, "gate_id": g.gate_id,
                                    "kind": g.kind, "channels": g.channels, "packet_uri": g.packet_uri,
                                    "eligible": g.eligible}, node_key=g.node_key, effect="notify_unhold",
                                    ordinal=g.ordinal)
            now = workflow.now()
            due = self._gate_next_due(g)
            wake = self._wake
            try:
                await workflow.wait_condition(lambda: g.state != "open" or self._wake != wake,
                                              timeout=max(min(due - now, self._until_deadline()), timedelta(seconds=1)),
                                              timeout_summary=f"gate {g.kind} timers")
            except TimeoutError:
                await self._gate_timer_fired(g)
        return {"gate_id": gate_id, "state": g.state}

    def _gate_next_due(self, g: GateRecord) -> datetime:
        cands = []
        if g.grace_until:
            cands.append(datetime.fromisoformat(g.grace_until))
        elif g.expires_at and not g.held:  # R2-023: no expiry while held by pause at_next_gate
            cands.append(datetime.fromisoformat(g.expires_at))
        if g.next_reminder_at and not g.held:
            cands.append(datetime.fromisoformat(g.next_reminder_at))
        esc = g.schedule.get("escalation_seconds", [])
        if g.escalation_level < len(esc):
            cands.append(datetime.fromisoformat(g.opened_at) + timedelta(seconds=esc[g.escalation_level]))
        return min(cands) if cands else workflow.now() + timedelta(days=30)

    async def _gate_timer_fired(self, g: GateRecord) -> None:
        now = workflow.now()
        if g.held:
            return
        if g.expires_at and now >= datetime.fromisoformat(g.expires_at):
            late = now - datetime.fromisoformat(g.expires_at)
            if g.grace_until is None and late > LATE_FIRE:
                # R2-014: the timer fired late (Temporal or worker outage). Decisions that control-plane-api
                # stamped before expires_at may still be in its outbox; V10 accepts them during the grace.
                g.grace_until = (now + EXPIRY_GRACE).isoformat()
                self.emit("gate.expiry_grace", f"{g.kind} gate expiry delayed {int(late.total_seconds())} s; "
                          f"accepting decisions made before expiry for {int(EXPIRY_GRACE.total_seconds() // 60)} min",
                          gate_id=g.gate_id)
                return
            if g.grace_until and now < datetime.fromisoformat(g.grace_until):
                return
            if g.schedule.get("on_expiry") == "keep_open_and_alert":
                g.expires_at = None  # stays open; operators are alerted once
                await self._notify("notify.job_event", {"job_id": self.s.job_id, "kind": "gate_overdue",
                                   "gate_id": g.gate_id}, node_key=g.node_key, effect="overdue", ordinal=g.ordinal)
                return
            g.state = "expired"  # on_expiry expire (job ends gate_expired) or decline (migration declined)
            self._gate_closed(g)
            self.emit("gate.expired", f"{g.kind} gate expired", gate_id=g.gate_id)
            await self._notify("notify.gate_resolved", {"job_id": self.s.job_id, "gate_id": g.gate_id,
                               "state": "expired"}, node_key=g.node_key, effect="notify_expired", ordinal=g.ordinal)
            if g.kind == "bundle_migration":
                self._on_migration_decided(g)
            return
        esc = g.schedule.get("escalation_seconds", [])
        if g.escalation_level < len(esc) and now >= datetime.fromisoformat(g.opened_at) + timedelta(seconds=esc[g.escalation_level]):
            g.escalation_level += 1
            await self._notify("notify.gate_escalated", {"job_id": self.s.job_id, "gate_id": g.gate_id,
                                "level": g.escalation_level}, node_key=g.node_key,
                                effect=f"escalate_{g.escalation_level}", ordinal=g.ordinal)
            self.emit("gate.escalated", f"{g.kind} gate escalated to level {g.escalation_level}", gate_id=g.gate_id)
        if g.next_reminder_at and now >= datetime.fromisoformat(g.next_reminder_at):
            g.reminders_sent += 1
            await self._notify("notify.gate_reminder", {"job_id": self.s.job_id, "gate_id": g.gate_id,
                                "n": g.reminders_sent}, node_key=g.node_key,
                                effect=f"remind_{g.reminders_sent}", ordinal=g.ordinal)
            rem = g.schedule.get("reminder_seconds", [])
            nxt = rem[g.reminders_sent] if g.reminders_sent < len(rem) else None
            repeat = g.schedule.get("reminder_repeat_seconds")
            opened = datetime.fromisoformat(g.opened_at)
            g.next_reminder_at = (opened + timedelta(seconds=nxt)).isoformat() if nxt is not None else \
                ((now + timedelta(seconds=repeat)).isoformat() if repeat else None)
            self.emit("gate.reminded", f"{g.kind} gate reminder {g.reminders_sent}", gate_id=g.gate_id)

    def _pr_timing(self) -> tuple[timedelta, timedelta]:
        rl = self.policy("repair_limits")
        pw = rl.get("pr_wait", {})
        days = pw.get("per_workflow_type", {}).get(self.s.workflow_type, {}).get("expiry_days", pw.get("expiry_days", 30))
        return timedelta(seconds=int(rl.get("debounce_seconds", 120))), timedelta(days=int(days))

    async def _wait_pr(self, node_key: str) -> Any:
        s = self.s
        debounce, expiry = self._pr_timing()
        if s.evaluation:  # XD-06-02: read scripted review round k instead of waiting for pr.event
            k = s.cycle + 1
            # WS-06 eval-harness.md §3.3 (R1V-014): the activity follows the GateScript's review_rounds_key itself
            r = await self._aux_act("eval.read_review_round", {"gate_script_uri": s.evaluation.get("gate_script_uri"),
                                    "round": k},
                                    node_key=node_key, effect=f"round_{k}", ordinal=1)
            self._set_status("waiting_external", "pr_review")
            return r  # {"event": "feedback", "batch": [...]} or {"event": "merged"} when rounds are exhausted
        self._set_status("waiting_external", "pr_review")
        if s.pr_last_activity_at is None:
            s.pr_last_activity_at = workflow.now().isoformat()
        while True:
            for ev in s.pr_buffer:
                if ev["event"] in ("pull_request_merged", "pull_request_closed"):
                    s.pr_buffer.clear()
                    if s.pr:
                        s.pr["state"] = "merged" if ev["event"] == "pull_request_merged" else "closed"
                    return {"event": "merged" if ev["event"] == "pull_request_merged" else "closed"}
            now = workflow.now()
            last = datetime.fromisoformat(s.pr_last_activity_at)
            feedback = [e for e in s.pr_buffer if e["event"] in (
                "review_submitted", "review_comment_created", "issue_comment_created", "replicant_command")]
            if feedback and (any(e["event"] == "review_submitted" for e in feedback) or now - last >= debounce):
                s.pr_buffer.clear()
                return {"event": "feedback", "batch": feedback}
            if now - last >= expiry:
                return {"event": "expired"}
            rem_days = self.policy("repair_limits").get("pr_wait", {}).get("reminder_days", [])
            if s.pr_reminders_sent < len(rem_days) and now - last >= timedelta(days=rem_days[s.pr_reminders_sent]):
                s.pr_reminders_sent += 1
                s.pr_reminders_total += 1
                await self._notify("notify.job_event", {"job_id": s.job_id, "kind": "pr_waiting_for_review",
                                   "pr": s.pr, "days": rem_days[s.pr_reminders_sent - 1]},
                                   node_key=node_key, effect=f"pr_remind_{s.pr_reminders_total}", ordinal=1)
                continue
            await self.process_notices()
            self.check_deadlines()
            if self.s.migration and self.s.migration.get("state") == "approved":
                return _Can("migration", node_key)
            if self._history_heavy():
                return _Can("history", node_key)
            timeout = (debounce - (now - last)) if feedback else (expiry - (now - last))
            timeout = min(timeout, self._until_deadline())
            if not feedback and s.pr_reminders_sent < len(rem_days):
                timeout = min(timeout, timedelta(days=rem_days[s.pr_reminders_sent]) - (now - last))
            n, wake = len(s.pr_buffer), self._wake
            try:
                await workflow.wait_condition(lambda: len(s.pr_buffer) != n or self._wake != wake,
                                              timeout=max(timeout, timedelta(seconds=1)),
                                              timeout_summary="pr_review wait")
            except TimeoutError:
                pass

    # ============================================================ continue-as-new (versioning.md 4)
    async def _continue_as_new(self, req: _Can) -> None:
        s = self.s
        s.resume_at = req.resume_at
        if req.kind == "repair_cycle":
            s.cycle += 1
            s.visits = {}
            s.qa_fix_used = s.qa_env_retries = 0  # repair_limits.qa.* are per cycle (WS-05)
            # counters of finished cycles are never used again (logical IDs carry the cycle number)
            suffix = f"@c{s.cycle}"
            s.ordinals = {k: v for k, v in s.ordinals.items() if k.endswith(suffix)}
            s.gate_ordinals = {k: v for k, v in s.gate_ordinals.items() if k.endswith(suffix)}
            s.qa_gate_with_failure = False
        if req.kind in ("phase", "repair_cycle"):
            s.boundary_crossed = req.resume_at
        if req.kind == "reroute":
            s.graph_spec_digest = None
            s.boundary_crossed = None
        s.segment += 1
        s.prune(workflow.now())
        if len(s.steering_queue) > 4:  # keep the carry small: stage the backlog to S3 (XD-FIX-R1-100 shape)
            arg = steering_backlog_input(s)
            node_key = arg["node_key"]
            r = await self._aux_act("steering.stage_backlog", arg, node_key=node_key, effect="steering",
                                    ordinal=arg["ordinal"])
            s.steering_refs.extend(r.get("keys") or [r["uri"]])
            s.steering_queue = []
        self._queue_sa(SA_CYCLE.value_set(s.cycle))
        self.emit("phase.entered", f"continue-as-new ({req.kind}) before {req.resume_at}")
        await self._drain()
        self._flush_sa()  # search attributes are copied to the next run (C-IDS 3)
        if req.kind == "migration":
            m = s.migration or {}
            s.migration = {**m, "state": "handing_off"}
            workflow.continue_as_new(ReplicantJobInput(
                mode="migration_handoff", carry=s.carry(), reason="migration",
                handoff={"target_build_id": m["target_build_id"],
                         "target_release_bundle_id": m["target_release_bundle_id"]}))
        workflow.continue_as_new(ReplicantJobInput(mode="continue", carry=s.carry(), reason=req.kind))

    async def _drain(self) -> None:
        # R2-013: approval records, gate-resolution notices and audit-bearing projections (gate.*,
        # bundle.*, job.terminal) are awaited without a cap; each is bounded by its own schedule-to-close.
        await workflow.wait_condition(lambda: all(h.done() for h in self._audit) and self._sha_pending == 0)
        try:
            await workflow.wait_condition(lambda: all(h.done() for h in self._proj), timeout=PROJECTION_DRAIN)
        except TimeoutError:
            pass  # snapshot-only events: the reconciler repairs any gap (C-STATE 2.1)
        await workflow.wait_condition(workflow.all_handlers_finished)

    async def _migration_prologue(self, inp: ReplicantJobInput) -> None:
        """Migration Handoff Protocol v1 (versioning.md section 6). FROZEN: every build must issue
        exactly this command sequence first in a migration_handoff run, so that the run's history
        replays on both the source and the target build."""
        h = inp.handoff or {}
        source_build = self.s.worker_build_id
        prev_status, prev_phase = self.s.status, self.s.phase
        self._set_status("running", "migrating")  # C-IDS phase (R1-023)
        ok = True
        try:  # step 1: pin this execution to the target build (retried for 30 minutes, RELEASE_OVERRIDE)
            await workflow.execute_activity(
                "release.apply_migration_override",
                {"workflow_id": workflow.info().workflow_id, "run_id": workflow.info().run_id,
                 "target_build_id": h["target_build_id"], "target_release_bundle_id": h["target_release_bundle_id"]},
                activity_id="release.c0.n1.apply_migration_override", **_opts("release.apply_migration_override"))
        except ActivityError:
            ok = False
        if ok:  # step 2: wait until the server routes this execution's tasks to the target build
            try:
                await workflow.wait_condition(lambda: workflow.info().get_current_build_id() == h["target_build_id"],
                                              timeout=HANDOFF_ROUTING_TIMEOUT, timeout_summary="handoff routing")
            except TimeoutError:
                ok = False
        if not ok:
            # Failure path (versioning.md 6.3, R2-015). Only the source build reaches this branch: re-pin the
            # execution to the source build (best effort), alert operators, and continue on N.
            try:
                await workflow.execute_activity(
                    "release.apply_migration_override",
                    {"workflow_id": workflow.info().workflow_id, "run_id": workflow.info().run_id,
                     "target_build_id": source_build, "target_release_bundle_id": self.s.release_bundle_id},
                    activity_id="release.c0.n1.revert_migration_override", **_opts("release.apply_migration_override"))
            except ActivityError:
                pass  # the execution keeps the override; operators see the alert below
            self.s.migration = None
            self._set_status(prev_status, prev_phase)
            self.emit("bundle.migration_failed", f"migration to {h['target_release_bundle_id'][:12]} failed; "
                      "the job continues on its current bundle")
            await self._notify("notify.job_event", {"job_id": self.s.job_id, "kind": "migration_failed",
                               "audience": "operators", "target_release_bundle_id": h["target_release_bundle_id"]},
                               node_key="release", effect="notify_migration_failed", ordinal=1)
            return
        # Only the target build reaches this line.
        self.s.release_bundle_id = h["target_release_bundle_id"]
        self.s.worker_build_id = h["target_build_id"]
        self.s.migration = None
        self.s.graph_spec_digest = None
        for k in ("bundle_revoked", "model_retiring"):  # bundle-bound deadlines are met by moving bundles
            self.s.deadlines.pop(k, None)
        if source_build != h["target_build_id"]:
            self.s.deadlines.pop("worker_version_retire_scheduled", None)
        self.s.hold = None
        self.s.waiting = None
        self.s.release_blocked = None  # the new bundle is not rolled back; open gates re-evaluate V12
        for g in self.s.gates.values():
            g.release_block = None
        self._set_status(prev_status, prev_phase)
        self._queue_sa(SA_BUNDLE.value_set(self.s.release_bundle_id))
        self.emit("bundle.migrated", f"migrated to {self.s.release_bundle_id[:12]}")

    # ============================================================ compensation and terminal
    async def _compensate(self) -> tuple[str, str | None]:
        s = self.s
        reason = "taken_over" if self._takeover else (s.forced_outcome or "cancelled_by_user")
        why = "takeover" if self._takeover else "job_cancelled"
        if self._takeover:
            self._set_status("running", "takeover")  # C-IDS phase (R1-023)
        cancelled = [r for r in s.runs.values() if r.active]
        for r in cancelled:
            await self._aux_act("runner.cancel_run", {"run_id": r.run_id, "reason": why},
                                node_key=r.node_key, effect="cancel", ordinal=r.ordinal, cycle=r.cycle)
            r.active = False
        if self._takeover:
            # C-MSG job.takeover (R1-008): wait until each cancelled run's finalize step has uploaded the
            # workspace archive and diff, bounded by finalize_seconds + 300 s, then publish that diff.
            diff_uri = s.refs.get("latest_diff_bundle_uri")
            for r in cancelled:
                deadline = workflow.now() + timedelta(seconds=r.finalize_seconds + 300)
                st: dict[str, Any] = {"phase": r.phase}
                while workflow.now() < deadline:
                    st = await self.call("runner.get_run", {"run_id": r.run_id}, block=False)
                    if st["phase"] in ("Succeeded", "Failed", "Cancelled"):
                        break
                    await workflow.sleep(timedelta(seconds=10))
                r.phase = st["phase"]
                if st.get("result_uri"):
                    res = await self.call("runner.read_result", {"run_id": r.run_id, "result_uri": st["result_uri"],
                                          "manifest_uri": st.get("artifact_manifest_uri")}, block=False)
                    for k, ref in (("diff_bundle", "latest_diff_bundle_uri"), ("handoff", "latest_handoff_uri")):
                        if res.get("outputs", {}).get(k):
                            s.refs[ref] = res["outputs"][k]
                    diff_uri = s.refs.get("latest_diff_bundle_uri")
            tko = self._takeover["takeover_id"]
            include = list(self._takeover["include"])
            pushed = await self._takeover_push(diff_uri) if "branch" in include else False
            if not pushed and "branch" in include:  # archive + patch instead of a pushed branch (XD-FIX-R2-001)
                include = [i for i in include if i != "branch"] + [i for i in ("workspace_archive",) if i not in include]
            pkg = await self._aux_act("takeover.assemble_package", {"job_id": s.job_id, "takeover_id": tko,
                                      "include": include, "refs": dict(s.refs),
                                      "branch": f"replicant-takeover/{s.job_id}" if pushed else None},
                                      node_key="takeover", effect="package", ordinal=s.takeover_ordinal)
            s.refs["takeover_package_uri"] = pkg["package_uri"]
        elif s.pr and s.pr.get("state") == "draft":
            await self._aux_act("publisher.close_pr", {"job_id": s.job_id, "pr": s.pr},
                                node_key="publish", effect="close_pr", ordinal=1)
        return (reason, None)

    async def _takeover_push(self, diff_uri: str | None) -> bool:
        """XD-FIX-R2-001 (+ WS-07 correction): takeover pushes go to `replicant-takeover/<job_id>`. When a critical
        risk class (for example ci_executed_paths) fires on the takeover diff, the requester must approve a
        publish_approval gate first. The Publisher raises PushRejected("ci_paths_takeover_unattested") when the
        tenant has not attested onboarding item OB-1; the takeover then ships an archive and patch without a branch."""
        s = self.s
        o = s.takeover_ordinal
        try:
            risk = await self.call("risk.classify", {"job_id": s.job_id, "tenant_id": s.tenant_id, "stage": "takeover",
                                   "refs": {**s.refs, "latest_diff_bundle_uri": diff_uri}, "targets": s.targets,
                                   "workflow_type": s.workflow_type}, block=False)
        except NodeFailed:
            risk = {"level": "critical", "rules_fired": ["risk_unavailable"]}
        if risk["level"] == "critical":
            kind = "publish_approval"
            gord = s.next_gate_ordinal(kind)
            gate_id = ids.gate_id_for(s.job_id, kind, s.cycle, gord)
            built = await self._aux_act("gates.build_packet", {"job_id": s.job_id, "tenant_id": s.tenant_id,
                                        "gate_id": gate_id, "kind": kind, "revision": 1, "refs": dict(s.refs), "risk": risk,
                                        "extra": {"takeover": True, "diff_bundle_uri": diff_uri}},
                                        node_key="takeover", effect="packet_takeover", ordinal=o)
            now = workflow.now()
            g = GateRecord(gate_id=gate_id, kind=kind, node_key="takeover", cycle=s.cycle, ordinal=o, state="open",
                           opened_at=now.isoformat(), presented_digest=built["digest"], packet_uri=built["packet_uri"],
                           channels=["ui", "slack", "cli"], eligible={"principals": [s.requester], "requester_may_decide": True},
                           expires_at=(now + timedelta(days=7)).isoformat(),
                           schedule={"expiry_seconds": 604800, "on_expiry": "expire"},
                           subject={k: built[k] for k in ("diff_sha256",) if built.get(k)})
            s.gates[gate_id] = g
            self.on_gate_opened(g)
            await self._notify("notify.gate_opened", {"job_id": s.job_id, "gate_id": gate_id, "kind": kind,
                               "channels": g.channels, "packet_uri": g.packet_uri, "eligible": g.eligible},
                               node_key="takeover", effect="notify_open_takeover", ordinal=o)
            try:
                r = await self._wait_gate(gate_id, "takeover")
            except NodeFailed:  # deadline or job age while waiting: no push
                r = None
            if r is None or isinstance(r, _Can) or s.gates[gate_id].state != "approved":
                self.emit("pr.takeover_branch_skipped", "takeover branch not pushed: approval not given")
                return False
        try:
            await self._aux_act("publisher.push_takeover_branch", {"job_id": s.job_id, "repo": self.primary_repo(),
                                "diff_bundle_uri": diff_uri, "branch": f"replicant-takeover/{s.job_id}"},
                                node_key="takeover", effect="push_branch", ordinal=o, block=False)
        except NodeFailed as f:
            if f.reason == "PushRejected":
                self.emit("pr.takeover_branch_skipped", "takeover branch rejected by the Publisher; archive and patch only")
                return False
            raise
        return True

    async def _compensate_failure(self) -> None:
        """Compensation for jobs that end failed or expired (R2-010): cancel runs still active (for example a
        parallel sibling) and close a draft PR. Best effort: a failure here never changes the outcome."""
        s = self.s
        try:
            for r in [r for r in s.runs.values() if r.active]:
                await self._aux_act("runner.cancel_run", {"run_id": r.run_id, "reason": "job_cancelled",
                                    "detail": "job ended"}, node_key=r.node_key, effect="cancel", ordinal=r.ordinal,
                                    cycle=r.cycle, block=False)
                r.active = False
            if s.pr and s.pr.get("state") == "draft":
                await self._aux_act("publisher.close_pr", {"job_id": s.job_id, "pr": s.pr},
                                    node_key="publish", effect="close_pr", ordinal=1, block=False)
        except NodeFailed as e:
            workflow.logger.warning("compensation incomplete", extra={"error": str(e)[:200]})

    async def _finish(self, outcome: str | None, reason: str | None) -> str:
        outcome = outcome or "infrastructure_failure"
        if self.s.evaluation and outcome == "pr_merged":
            outcome = "ready_to_publish"  # scripted rounds ended with `merged` (WS-06 eval-harness.md §3.3, R1V-014)
        status = TERMINAL_STATUS_FOR_OUTCOME.get(outcome, "failed")
        self.s.terminal = {"outcome": outcome, "reason": reason, "at": workflow.now().isoformat()}
        if not self.s.evaluation and self._ready:
            mem = {"job_id": self.s.job_id, "tenant_id": self.s.tenant_id, "outcome": outcome, "status": status,
                   "memory_snapshot_id": self.s.memory_snapshot_id, "cycle": self.s.cycle, "refs": dict(self.s.refs)}
            try:  # best effort: memory bookkeeping never changes the job's outcome (WS-04 XD-04-01)
                await self._aux_act("memory.record_outcome", mem, node_key="finalize", effect="record_outcome",
                                    ordinal=1, block=False)
                await self._aux_act("memory.write_episode", mem, node_key="finalize", effect="write_episode",
                                    ordinal=1, block=False)
            except NodeFailed as e:  # retries exhausted or non-retryable
                workflow.logger.warning("memory bookkeeping failed", extra={"error": str(e)[:200]})
        self._set_status(status, "done")
        self.emit("job.terminal", f"job {status} ({outcome})")
        # notifications.md §2 `job.succeeded` / `job.failed` (and the other terminal statuses) to the requester.
        # Fire-and-forget (block=False): a notification failure never changes the outcome. Patched for replay safety.
        if workflow.patched("notify-terminal-v1") and self.notifications_enabled() and self._ready:
            try:
                await self._aux_act("notify.job_event", {
                    "job_id": self.s.job_id, "kind": status, "outcome": outcome, "reason": (reason or "")[:500],
                    "pr": self.s.pr, "title": self.s.request_summary.get("title")},
                    node_key="finalize", effect="notify_terminal", ordinal=1, block=False)
            except NodeFailed as e:
                workflow.logger.warning("terminal notification not delivered", extra={"error": str(e)[:200]})
        await self._drain()
        self._flush_sa()
        return outcome

    # ============================================================ helpers used by nodes
    def policy(self, policy_type: str) -> dict[str, Any]:
        return self._bundle["policies"][policy_type]

    def memory_policy_ref(self) -> dict[str, Any] | None:
        """The bundle's memory policy reference {ref, digest} for memory.create_snapshot (XD-FIX-R1-100)."""
        m = (self._bundle.get("policy_refs") or {}).get("memory") if self._bundle else None
        return dict(m) if m else None

    def gate_facts(self, kind: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic facts for gates.resolve_routing (WS-07 evaluates gate_rules on these)."""
        s = self.s
        return {"workflow_type": s.workflow_type, "risk_level": s.risk["level"], "rules_fired": s.risk["rules_fired"],
                "requester": s.requester, "tenant_id": s.tenant_id, "repos": [t["repo"] for t in s.targets],
                "require_gates": s.request_summary.get("require_gates", []), "cycle": s.cycle,
                "changed_paths": (s.exports.get("verify") or s.exports.get("verify_repair") or {}).get("paths", [])[:200],
                "release_bundle_id": s.release_bundle_id, **(extra or {})}

    def reapproval_waived(self, key: str) -> bool:
        """D-19 rebase-and-reverify: after a clean, mechanical rebase (no conflicts) with unchanged
        risk, a publish gate already approved in this cycle is not asked again, unless the gate's
        routing says reapprove_after_rebase == 'always'."""
        prev = self.s.exports.get(key, {})
        bc = self.s.exports.get("base_check", {})
        return (prev.get("reapprove") != "always" and prev.get("decision") == "approve" and prev.get("required") is True
                and bc.get("moved") is True and bc.get("clean") is True
                and prev.get("risk_level") == self.s.risk["level"])

    def approved_diff(self) -> tuple[str | None, str | None]:
        """(diff sha256, decision_id) the Publisher must verify (WS-09 G-03, XD-07-02): the diff digest
        in the latest approved publish_approval packet of this cycle; when no publish gate was required,
        the digest the verifier (and the diff-stage risk rules) checked."""
        for g in reversed(list(self.s.gates.values())):
            if g.kind == "publish_approval" and g.cycle == self.s.cycle and g.state == "approved" and g.subject.get("diff_sha256"):
                return g.subject["diff_sha256"], (g.decision or {}).get("decision_id")
        return self.s.verified_diff_sha256, None

    def priority(self) -> Priority:
        """Task-queue priority and tenant fairness for every activity (ADR-01-006)."""
        key = {"urgent": 1, "high": 2, "normal": 3, "low": 4}.get(self.s.priority, 3)
        if self.s.parent_job_id:
            key = 5  # maintenance children yield to interactive work
        return Priority(priority_key=key, fairness_key=self.s.tenant_id)

    def notifications_enabled(self) -> bool:
        return not self.s.evaluation  # evaluation mode suppresses every notify.* (XD-06-02)

    def security_alert(self, run_id: str, message: str) -> None:
        self.emit("node.failed", f"run {run_id} stopped by security automation", run_id=run_id)

    def set_status(self, status: str, phase: str) -> None:
        self._set_status(status, phase)

    async def resolve_gate_from_script(self, g: GateRecord) -> None:
        """Evaluation mode (CCR-06-01, ADR-06-006): resolve an opened gate from the gate script via the
        same transition the gate.decide handler uses. No Update, no human, no authorization activity."""
        ev = self.s.evaluation or {}
        n = sum(1 for x in self.s.gates.values() if x.kind == g.kind)
        if ev.get("mode") == "shadow":
            d = {"decision": "stop_case" if g.kind == "clarification" else "approve", "rule_index": -1}
        else:
            d = await self._aux_act("eval.read_gate_script", {"gate_script_uri": ev["gate_script_uri"], "gate_kind": g.kind,
                                    "ordinal": n, "cycle": self.s.cycle}, node_key=g.node_key,
                                    effect=f"script_{g.kind[:8]}", ordinal=g.ordinal)
        decision_id = ids.derived("dec", "system", f"{g.gate_id}:script")
        # R1-019: no principal decided; the record names the human the evaluation acts for and source=script
        self._apply_decision(g, {"decision_id": decision_id, "decision": d["decision"], "principal_id": self.s.acting_for,
                                 "channel": "system", "comment": d.get("comment"), "answer_text": d.get("answer_text")},
                             source="script", rule_index=d.get("rule_index"))

    def node_when_true(self, key: str) -> bool:
        w = self._spec.nodes[key].when if self._spec else None
        return bool(w) and self._guard(w)

    def binding_environment(self) -> str:
        return self._compose["environment"]

    def environment_digest(self) -> str:
        """Pinned digest of the binding's environment definition, recorded by graph.compose (R1V-012)."""
        return self._compose["environment_digest"]

    def session_state_grace_days(self) -> int:
        """sessionStateGraceDays of the bundle's retention policy (0-7, default 7; R1V-010, WS-10 lifecycle.md)."""
        ret = (self._bundle.get("policies") or {}).get("retention") or {}
        return max(0, min(7, int(ret.get("sessionStateGraceDays", 7))))

    def primary_repo(self) -> dict[str, Any]:
        t = self.s.targets[0]
        return {**t["repo_ref"], "base_ref": t.get("base_ref"), "base_sha": t.get("base_sha")}

    def set_base_sha(self, sha: str) -> None:
        self.s.targets[0]["base_sha"] = sha

    def remaining_budget_usd(self) -> float:
        return float(self.s.request_summary.get("budget_usd_max") or self.policy("repair_limits").get("default_job_budget_usd", 50))

    def request_reroute(self, wf_type: str, start_at: str) -> None:
        self._reroute = (wf_type, start_at)

    def upsert_risk(self, level: str) -> None:
        self._queue_sa(SA_RISK.value_set(level))

    def consume_steering(self, consumed: list[str]) -> None:
        if not consumed:
            return
        self.s.steering_queue = [m for m in self.s.steering_queue if m["message_id"] not in consumed]
        self.s.steering_refs = []
        now = workflow.now().isoformat()
        self.s.steering_log += [{"message_id": m, "outcome": "consumed", "at": now} for m in consumed]

    def on_gate_opened(self, g: GateRecord) -> None:
        self._set_status("waiting_human", self.s.phase)
        self._upsert_gate_sas()
        self.emit("gate.opened", f"{g.kind} gate opened", gate_id=g.gate_id)

    def _gate_closed(self, g: GateRecord) -> None:
        self._upsert_gate_sas()

    def _upsert_gate_sas(self) -> None:
        open_ids = [g.gate_id for g in self.s.gates.values() if g.state == "open"]
        self._queue_sa(SA_OPEN_GATES.value_set(open_ids), SA_HUMAN.value_set(bool(open_ids)))

    def _guard(self, src: str) -> bool:
        g = self._guards.get(src)
        if g is None:
            g = self._guards[src] = Guard(src)
        return g(self.s.guard_context(self._spec.params if self._spec else {}, {}))

    def _set_status(self, status: str, phase: str) -> None:
        sas = []
        if status != self.s.status:
            self.s.status = status
            sas.append(SA_STATUS.value_set(status))
            if self.s.parent_job_id:
                asyncio.ensure_future(self._signal_parent(status, phase))
        if phase != self.s.phase:
            self.s.phase = phase
            sas.append(SA_PHASE.value_set(phase))
        # C-IDS 4.1 (R1-023): ReplicantWaitingSince is set when a wait starts and cleared when it ends
        if sas and sas[0].key.name == "ReplicantStatus":
            waiting = status in ("waiting_human", "waiting_external", "paused")
            sas.append(SA_WAITING.value_set(workflow.now()) if waiting else SA_WAITING.value_unset())
        if sas:
            self._queue_sa(*sas)

    async def _signal_parent(self, status: str, phase: str) -> None:
        """SignalExternalWorkflowExecution to the MaintenanceBatch parent (ADR-01-007, CCR-01-04).
        Fire-and-forget: a missing or closed parent never affects the child."""
        try:
            await workflow.get_external_workflow_handle(self.s.progress_workflow_id or self.s.parent_job_id).signal(  # type: ignore[arg-type]
                "batch.child_progress", {"schema": "replicant.msg.batch-child-progress/v1", "child_job_id": self.s.job_id,
                                         "status": status, "phase": phase, "seq": self.s.projection_seq + 1})
        except Exception as e:
            workflow.logger.info("parent progress signal failed", extra={"error": str(e)[:200]})

    async def _notify(self, name: str, arg: Any, *, node_key: str, effect: str, ordinal: int) -> Any:
        if not self.notifications_enabled():
            return {"suppressed": "evaluation"}
        return await self._aux_act(name, arg, node_key=node_key, effect=effect, ordinal=ordinal)

    async def _aux_act(self, name: str, arg: Any, *, node_key: str, effect: str, ordinal: int,
                       cycle: int | None = None, block: bool = True) -> Any:
        lid = ids.logical_activity_id(node_key, self.s.cycle if cycle is None else cycle, ordinal, effect)
        if lid in self.s.memo:
            return self.s.memo[lid]
        r = await self.call(name, arg, activity_id=lid, block=block)
        self.s.memo[lid] = r
        return r

    def start_side_effect(self, name: str, arg: Any, *, activity_id: str) -> None:
        """Fire-and-forget side-effecting activity (for example qa.index_results). The handle is drained before
        Continue-As-New and close like approval records (R2-013)."""
        if activity_id in self.s.memo:
            return
        self.s.memo[activity_id] = True
        self._audit.append(workflow.start_activity(name, arg, activity_id=activity_id, **_opts(name)))

    # ============================================================ activity calls and holds (R2-010, R2-012)
    async def call(self, name: str, arg: Any, *, activity_id: str | None = None, summary: str | None = None,
                   block: bool = True) -> Any:
        """Every workflow activity call goes through here (NodeContext.act included).

        * Non-retryable failure (retry_state NON_RETRYABLE_FAILURE): NodeFailed(<error type>), so the job ends
          with a terminal outcome, compensation and a final projection. The execution never fails.
        * Retries exhausted (MAXIMUM_ATTEMPTS_REACHED or TIMEOUT): a dependency is down. With block=True the
          job holds as `dependency` (status paused) and re-probes with jittered backoff until the call
          succeeds, an operator resumes or cancels, or repair_limits.dependency_block.max_hours passes
          (then NodeFailed('dependency_unavailable')). The retry reuses the same activity ID, so the
          idempotency key is unchanged. With block=False: NodeFailed('dependency_unavailable') at once.
        """
        if CATALOG[name].side_effect and not activity_id:
            raise ValueError(f"activity {name} has side effects and needs an explicit activity ID")
        if name.startswith("notify.") and isinstance(arg, dict) and "tenant_id" not in arg:
            arg = {**arg, "tenant_id": self.s.tenant_id}  # replicant-worker v0: POST /internal/v1/notifications needs it
        d = CATALOG[name].klass
        while True:
            try:
                r = await workflow.execute_activity(
                    name, arg, task_queue=d.task_queue, start_to_close_timeout=d.start_to_close,
                    schedule_to_close_timeout=d.schedule_to_close, heartbeat_timeout=d.heartbeat,
                    retry_policy=d.retry, activity_id=activity_id, summary=summary or (activity_id or name),
                    priority=self.priority())
            except ActivityError as e:
                if e.retry_state not in (RetryState.MAXIMUM_ATTEMPTS_REACHED, RetryState.TIMEOUT):
                    det = e.cause.details[0] if isinstance(e.cause, ApplicationError) and e.cause.details \
                        and isinstance(e.cause.details[0], dict) else None
                    raise NodeFailed(_etype(e), f"{name}: {e.cause or e}"[:500], details=det) from None
                if not block:
                    raise NodeFailed("dependency_unavailable", f"{name}: {_etype(e)}") from None
                await self._blocked_on_dependency(name, _etype(e))
                continue
            if self.s.hold and self.s.hold.get("kind") == "dependency":
                self._release_hold(f"{name} succeeded again")
            return r

    def _block_policy(self) -> dict[str, Any]:
        pol = (self._bundle.get("policies", {}).get("repair_limits", {}) if self._bundle else {}).get("dependency_block", {})
        return {**DEPENDENCY_BLOCK_DEFAULTS, **pol}

    async def _blocked_on_dependency(self, name: str, etype: str) -> None:
        s, pol = self.s, self._block_policy()
        now = workflow.now()
        if not s.hold or s.hold.get("kind") != "dependency":
            s.hold = {"kind": "dependency", "activity": name, "error_type": etype, "since": now.isoformat(),
                      "probes": 0, "prev_status": s.status}
            self._set_status("paused", s.phase)
            self.emit("job.blocked_on_dependency", f"waiting for a platform dependency ({name}: {etype}); "
                      "retrying automatically; operators may resume or cancel")
        h = s.hold
        if now - datetime.fromisoformat(h["since"]) > timedelta(hours=float(pol["max_hours"])):
            raise NodeFailed("dependency_unavailable", f"{name} unavailable for more than {pol['max_hours']} h")
        h["probes"] += 1
        base = min(float(pol["probe_initial_seconds"]) * 2 ** (h["probes"] - 1), float(pol["probe_max_seconds"]))
        jitter = int(hashlib.sha256(f"{s.job_id}:{h['probes']}".encode()).hexdigest()[:8], 16) % max(1, int(pol["jitter_seconds"]))
        h["next_probe_at"] = (now + timedelta(seconds=base + jitter)).isoformat()
        self.set_waiting("dependency_outage", since=h["since"], attempt=h["probes"], dependency="internal_service",
                         next_probe_at=h["next_probe_at"])
        wake = self._wake
        try:  # job.resume ("retry now") bumps _wake; job.cancel cancels this task
            await workflow.wait_condition(lambda: self._wake != wake, timeout=timedelta(seconds=base + jitter),
                                          timeout_summary="dependency re-probe")
        except TimeoutError:
            pass

    async def wait_for_dependency(self, dep: str, health: dict[str, Any], *, node_key: str) -> None:
        """R2-012 circuit breaker. A run failed because a platform dependency (ai-gateway, repo-mirror, exec
        node pool) is down. Hold the node (status paused) without re-provisioning, probe the breaker with
        jittered backoff, and restart only after it closes, plus a per-job jitter so 150 jobs do not
        re-provision sandboxes in one herd. Runner admission (tenant quota, 429 Retry-After) meters the rest."""
        s, pol = self.s, self._block_policy()
        s.hold = {"kind": "platform_dependency", "dependency": dep, "since": workflow.now().isoformat(), "probes": 0,
                  "prev_status": s.status, "breaker": health.get("state", "open")}
        self._set_status("paused", s.phase)
        self.emit("job.blocked_on_dependency", f"{dep} unavailable (breaker {health.get('state', 'open')}); "
                  "the run restarts automatically when it recovers", node_key=node_key)
        state = health.get("state", "open")
        while state != "closed":
            if workflow.now() - datetime.fromisoformat(s.hold["since"]) > timedelta(hours=float(pol["max_hours"])):
                raise NodeFailed("dependency_unavailable", f"{dep} unavailable for more than {pol['max_hours']} h")
            s.hold["probes"] += 1
            base = min(float(pol["probe_initial_seconds"]) * 2 ** (s.hold["probes"] - 1), float(pol["probe_max_seconds"]))
            self.set_waiting("dependency_outage", since=s.hold["since"], attempt=s.hold["probes"], dependency=dep,
                             next_probe_at=(workflow.now() + timedelta(seconds=base)).isoformat())
            wake = self._wake
            try:
                await workflow.wait_condition(lambda: self._wake != wake, timeout=timedelta(seconds=base),
                                              timeout_summary=f"{dep} breaker probe")
            except TimeoutError:
                pass
            try:
                state = (await self.call("platform.dependency_health", {"dependency": dep, "tenant_id": s.tenant_id},
                                         block=False)).get("state", "open")
            except NodeFailed:
                state = "open"
        jitter = int(hashlib.sha256(f"{s.job_id}:{dep}:resume".encode()).hexdigest()[:8], 16) % max(1, int(pol["jitter_seconds"]))
        await workflow.sleep(timedelta(seconds=jitter))  # spread restarts after recovery
        self._release_hold(f"{dep} recovered")

    def set_waiting(self, reason: str, *, since: str | None = None, attempt: int = 1, **extra: Any) -> None:
        """JobSnapshot.waiting (C-MSG 1.7.0): why the job waits; projected, cleared when work resumes."""
        w = {"reason": reason, "since": since or (self.s.waiting or {}).get("since") or workflow.now().isoformat(),
             "attempt": max(1, int(attempt)), **{k: v for k, v in extra.items() if v is not None}}
        if w != self.s.waiting:
            self.s.waiting = w
            self.emit("job.waiting", f"waiting: {reason.replace('_', ' ')} (attempt {w['attempt']})")

    def clear_waiting(self) -> None:
        if self.s.waiting is not None:
            self.s.waiting = None
            self.emit("job.waiting_ended", "no longer waiting")

    def _release_hold(self, why: str) -> None:
        self.s.waiting = None
        h, self.s.hold = self.s.hold or {}, None
        self._set_status(h.get("prev_status") or "running", self.s.phase)
        self.emit("job.released", f"hold {h.get('kind')} released: {why}")

    async def hold_while(self, pred: Any, *, kind: str, detail: str, node_key: str | None = None,
                         status: str = "paused") -> None:
        """Hold the job until pred() is true (kill switch, revoked bundle). Deadlines and the job age limit
        still apply (check_deadlines raises NodeFailed); cancel and takeover cancel this task."""
        if pred():
            return
        s = self.s
        s.hold = {"kind": kind, "since": workflow.now().isoformat(), "detail": detail[:200], "prev_status": s.status}
        s.waiting = {"reason": "bundle_revoked" if kind == "revoked" else "operator_hold", "since": s.hold["since"],
                     "attempt": 1}
        self._set_status(status, s.phase)
        self.emit("job.held", f"{kind}: {detail}"[:300], node_key=node_key)
        while not pred():
            await self.process_notices()
            self.check_deadlines()
            wake = self._wake
            try:
                await workflow.wait_condition(lambda: pred() or self._wake != wake, timeout=self._until_deadline(),
                                              timeout_summary=f"hold {kind}")
            except TimeoutError:
                pass
        self._release_hold("condition met")

    # ---- kill switches (C-MSG 1.5.0 ops.kill_switch, R2-016)
    def _apply_kill_switch(self, sig: dict[str, Any]) -> None:
        key = f"{sig['scope']}:{sig.get('scope_value') or '*'}"
        cur = self.s.kill_switches.get(key)
        if cur and cur["issued_at"] > sig["issued_at"]:
            return  # latest issued_at per (scope, scope_value) wins
        if sig["state"] == "engaged":
            self.s.kill_switches[key] = {"switches": list(sig["switches"]), "issued_at": sig["issued_at"]}
        else:
            self.s.kill_switches.pop(key, None)
        self._wake += 1

    def kill_engaged(self, switch: str) -> bool:
        return any(switch in v["switches"] for v in self.s.kill_switches.values())

    # ---- deadlines (release.notice deadlines, hard job lifetime)
    def _max_age_at(self) -> datetime | None:
        if not self.s.created_at or not self._bundle:
            return None
        days = float(self.policy("repair_limits").get("job_max_age_days", 60))
        return datetime.fromisoformat(self.s.created_at) + timedelta(days=days)

    def check_deadlines(self) -> None:
        now = workflow.now()
        for kind, at in sorted(self.s.deadlines.items()):
            if now >= datetime.fromisoformat(at):
                raise NodeFailed(DEADLINE_OUTCOME[kind], "model_retired" if kind == "model_retiring"  # R1V-129 reason
                                 else f"{kind} deadline {at} passed before migration")
        m = self._max_age_at()
        if m is not None and now >= m:
            raise NodeFailed("job_max_age_exceeded", f"job older than repair_limits.job_max_age_days ({m.isoformat()})")

    def _until_deadline(self) -> timedelta:
        cands = [datetime.fromisoformat(a) for a in self.s.deadlines.values()]
        m = self._max_age_at()
        if m is not None:
            cands.append(m)
        if not cands:
            return timedelta(days=30)
        return max(min(cands) - workflow.now(), timedelta(seconds=1))

    # ---- notices processed in the main flow (bundle_patched, model_retiring, principal changes)
    async def process_notices(self) -> None:
        if not self.s.notice_queue or self._notice_lock.locked():
            return
        async with self._notice_lock:
            while self.s.notice_queue:
                n = self.s.notice_queue[0]
                await self._process_notice(n)
                self.s.notice_queue.pop(0)

    async def _process_notice(self, n: dict[str, Any]) -> None:
        s = self.s
        kind = n["kind"]
        if kind in ("bundle_patched", "model_retiring"):
            s.notice_seq += 1
            target = n.get("recommended_release_bundle_id")
            el = await self._aux_act("release.check_migration_eligibility", {
                "job_id": s.job_id, "tenant_id": s.tenant_id, "current_release_bundle_id": s.release_bundle_id,
                "target_release_bundle_id": target, "workflow_type": s.workflow_type, "resume_at": s.resume_at,
                "carry_schema": "replicant.wf-carry/v1", "lineage": "patch" if kind == "bundle_patched" else "model_successor",
                "open_gate_kinds": sorted({g.kind for g in s.gates.values() if g.state == "open"})},
                node_key="release", effect="patch_check" if kind == "bundle_patched" else "successor_check",
                ordinal=min(s.notice_seq, 999), cycle=0, block=False) if target else {"eligible": False, "reason": "no target"}
            if kind == "bundle_patched":
                if el.get("eligible"):  # ADR-01-003: build already moved by operators; record B' (D-03)
                    s.release_bundle_id = target
                    s.worker_build_id = el.get("target_build_id") or workflow.info().get_current_build_id() or s.worker_build_id
                    self._queue_sa(SA_BUNDLE.value_set(target))
                    self.emit("bundle.patched", f"emergency patch bundle {target[:12]} recorded")
                else:
                    self.emit("notice.rejected", f"bundle_patched notice ignored: {el.get('reason', '')[:120]}")
                return
            if el.get("eligible"):  # model_retiring: forced migration without consent (C-MSG 1.5.0, R2-044)
                s.migration = {"target_release_bundle_id": target, "target_build_id": el["target_build_id"],
                               "state": "approved", "required": True, "consent": "not_required:model_retiring"}
                self.emit("bundle.migration_forced", f"model retiring: migrating to {target[:12]} at the next safe point")
                self._wake += 1
            else:
                self.emit("notice.rejected", f"model successor not eligible: {el.get('reason', '')[:120]}; "
                          "the job stops at the retirement date unless migrated")
            return
        if kind == "principal_changed":
            await self._reroute_gates(n["principal_id"], n["change"])
        if kind == "contested":
            self._contest(n["decision_id"])

    def _contest(self, decision_id: str) -> None:
        """XD-FIX-R1-173: an approval accepted before the approver's removal propagated. If no publish activity
        was scheduled for it and the gate's routing says reopen_if_unpublished, the approval is withdrawn and a new
        gate instance (same packet and digest) must be approved before anything is published; otherwise the
        contest is recorded and operators are alerted."""
        s = self.s
        g = next((x for x in s.gates.values() if (x.decision or {}).get("decision_id") == decision_id), None)
        if g is None or g.state != "approved":
            self.emit("notice.received", "contested decision not found among approved gates")
            return
        if g.gate_id in s.publish_gate_ids:
            self.emit("gate.decision_contested_after_publish", f"{g.kind} approval contested after publish", gate_id=g.gate_id,
                      decision_id=decision_id)
            return
        if g.routing.get("on_contested_decision") != "reopen_if_unpublished":
            self.emit("gate.decision_identity_contested", f"{g.kind} approval contested; recorded and alerted", gate_id=g.gate_id,
                      decision_id=decision_id)
            return
        g.state = "cancelled"  # the approval is discarded (the record and decision_log keep it)
        s.contested.append({"gate_id": g.gate_id, "kind": g.kind, "decision_id": decision_id})
        self._gate_closed(g)
        self.emit("gate.decision_identity_contested", f"{g.kind} approval withdrawn (approver identity contested); "
                  "re-approval required before publishing", gate_id=g.gate_id, decision_id=decision_id)

    async def _reroute_gates(self, principal: str, change: str) -> None:
        """C-MSG identity.principal_changed (R1-020): a gate whose only eligible principal changed is
        re-routed (eligibility re-resolved by activity) and re-notified; the requester's deprovisioning
        also notifies tenant admins."""
        s = self.s
        if principal == s.requester and change in ("deprovisioned", "suspended"):
            s.reroute_seq += 1
            await self._notify("notify.job_event", {"job_id": s.job_id, "kind": "requester_deprovisioned",
                               "audience": "tenant_admins"}, node_key="identity", effect="notify_admins",
                               ordinal=min(s.reroute_seq, 999))
        for g in [g for g in s.gates.values() if g.state == "open"]:
            if g.eligible.get("principals") != [principal]:
                continue
            s.reroute_seq += 1
            o = min(s.reroute_seq, 999)
            routing = await self._aux_act("gates.resolve_routing", {"gate_kind": g.kind, "facts": self.gate_facts(
                g.kind, {"exclude_principals": [principal], "reroute_reason": change})},
                node_key="identity", effect="reroute", ordinal=o)
            g.eligible, g.channels = dict(routing["eligible"]), list(routing["channels"])
            self.emit("gate.rerouted", f"{g.kind} gate re-routed after an approver change ({change})", gate_id=g.gate_id)
            await self._notify("notify.gate_opened", {"job_id": s.job_id, "gate_id": g.gate_id, "kind": g.kind,
                               "channels": g.channels, "packet_uri": g.packet_uri, "eligible": g.eligible},
                               node_key="identity", effect="renotify", ordinal=o)

    # ============================================================ projection (C-STATE 2)
    def emit(self, event_type: str, summary: str, **refs: Any) -> None:
        s = self.s
        s.projection_seq += 1
        self._queue_sa(SA_SEQ.value_set(s.projection_seq))
        event = {"schema": "replicant.projection-event/v1", "job_id": s.job_id, "tenant_id": s.tenant_id,
                 "seq": s.projection_seq, "workflow_run_id": workflow.info().run_id,
                 "event": {"type": event_type, "at": workflow.now().isoformat(), "summary": summary[:300],
                           **({"refs": {k: v for k, v in refs.items() if v}} if refs else {})},
                 "snapshot": self.snapshot()}
        h = workflow.start_activity(
            "projection.apply", event, task_queue=PROJECTION.task_queue,
            schedule_to_close_timeout=PROJECTION.schedule_to_close, start_to_close_timeout=PROJECTION.start_to_close,
            retry_policy=PROJECTION.retry, summary=f"projection seq {s.projection_seq}")
        if event_type.startswith(AUDIT_EVENT_PREFIXES):  # the projector audits these (R2-013)
            self._audit = [a for a in self._audit if not a.done()] + [h]
        else:
            self._proj = [p for p in self._proj if not p.done()] + [h]

    def snapshot(self) -> dict[str, Any]:
        s = self.s
        snap: dict[str, Any] = {
            "schema": "replicant.job-snapshot/v1", "job_id": s.job_id, "tenant_id": s.tenant_id,
            "seq": max(1, s.projection_seq), "as_of": workflow.now().isoformat(),
            "workflow_type": s.workflow_type, "status": s.status, "phase": s.phase,
            "release_bundle_id": s.release_bundle_id, "requester": s.requester, "title": s.title,
            "priority": s.priority, "risk": s.risk, "cycle": s.cycle,
            "targets": [{k: v for k, v in {"repo": t["repo"], "base_ref": t.get("base_ref"),
                                           "base_sha": t.get("base_sha")}.items() if v} for t in s.targets],
            "repair_budget": {"used": s.repair_used, "max": s.repair_max},
            "gates": [{**_gate_view(g), **({"qa_reruns_used": min(2, s.qa_reruns)} if g.kind == "publish_approval"
                                            and g.cycle == s.cycle else {})} for g in list(s.gates.values())[-50:]],
            "runs": [{k: v for k, v in {"run_id": r.run_id, "node_key": r.node_key, "cycle": r.cycle,
                                        "ordinal": r.ordinal, "role": r.role, "phase": r.phase, "reason": r.reason,
                                        "isolation": r.isolation, "started_at": r.started_at,
                                        "finished_at": r.finished_at, "artifact_manifest_uri": r.artifact_manifest_uri,
                                        "result_uri": r.result_uri}.items() if v is not None}
                     for r in list(s.runs.values())[-100:]],
            "steering": s.steering_log[-200:], "paused": s.paused,
            "pending_notices": [n for n in s.pending_notices if n in (
                "newer_bundle_available", "worker_version_draining", "worker_version_retire_scheduled",
                "bundle_revoked", "bundle_patched", "model_retiring", "bundle_rolled_back")][-20:],  # R1V-007
        }
        if s.worker_build_id:
            snap["worker_deployment_version"] = {"deployment_name": "replicant-worker", "build_id": s.worker_build_id}
        if s.parent_job_id:
            snap["parent_job_id"] = s.parent_job_id
        if s.memory_snapshot_id:
            snap["memory_snapshot_id"] = s.memory_snapshot_id
        if s.active_nodes:
            k, v = next(iter(s.active_nodes.items()))
            snap["current_node"] = {"node_key": k, "cycle": s.cycle, "ordinal": v["ordinal"], "started_at": v["started_at"]}
        if s.pr and s.pr.get("number"):
            snap["pull_request"] = {k: v for k, v in {"repo": s.pr.get("repo"), "number": s.pr["number"],
                                                      "url": s.pr.get("url"), "head_sha": s.pr.get("head_sha"),
                                                      "state": s.pr.get("state", "open")}.items() if v}
        if s.terminal:
            snap["terminal"] = {k: v for k, v in s.terminal.items() if v is not None}
        if s.evaluation:
            snap["evaluation"] = {k: s.evaluation[k] for k in ("mode", "eval_run_id", "shadow_of_job_id") if s.evaluation.get(k)}
        if s.waiting:
            snap["waiting"] = dict(s.waiting)
        return snap

    # ============================================================ Updates (C-MSG 2.1)
    def _common_validate(self, req: dict[str, Any], id_field: str, schema: str) -> None:
        if not isinstance(req, dict) or req.get("schema") != schema or not _ID[id_field].match(str(req.get(id_field, ""))):
            raise _reject("InvalidPayload", f"payload does not match {schema}")
        actor = req.get("actor") or {}
        if not _ID["principal"].match(str(actor.get("principal_id", ""))):
            raise _reject("InvalidPayload", "actor.principal_id invalid")
        if req.get("channel") not in (None, "ui", "slack", "github", "gitea", "cli", "api", "system"):
            raise _reject("InvalidPayload", "channel invalid")

    async def gate_decide(self, req: dict[str, Any]) -> dict[str, Any]:
        gate_id, decision_id = req["gate_id"], req["decision_id"]
        lock = self._locks.setdefault(gate_id, asyncio.Lock())
        async with lock:
            g = self.s.gates[gate_id]
            if g.state != "open":  # handler re-check of V4 under the lock
                return self._superseded(g, req)
            try:  # R2-011: after the validator accepted the Update, the handler never fails
                check = await workflow.execute_activity("identity.check_decision", {
                    "job_id": self.s.job_id, "tenant_id": self.s.tenant_id, "gate_id": gate_id, "kind": g.kind,
                    "principal_id": req["actor"]["principal_id"], "channel": req["channel"],
                    "eligible": g.eligible, "repos": [t["repo"] for t in self.s.targets],
                    "requester": self.s.requester}, **_opts("identity.check_decision"))
            except ActivityError:
                # identity-service, Okta, the broker or GitHub stayed unavailable for the whole retry budget
                # (about 2 minutes). Fail closed with the retryable CCR-07-02 reason; the decision_id is not
                # recorded, so the sender may re-submit it later under a new Update ID (XD-FIX-R2-011).
                check = {"allow": False, "reason": "identity_unverifiable"}
            if g.state != "open":  # another decision won while we were checking
                return self._superseded(g, req)
            if not check["allow"]:
                self._log_decision(g, req, "denied", denial_reason=check["reason"])
                self.emit("gate.decision_denied", f"{g.kind} decision denied ({check['reason']})",
                          gate_id=gate_id, decision_id=decision_id)
                return {"schema": "replicant.msg.gate-decide-result/v1", "outcome": "denied", "gate_id": gate_id,
                        "decision_id": decision_id, "denial_reason": check["reason"]}
            return self._apply_decision(g, {"decision_id": decision_id, "decision": req["decision"],
                                            "principal_id": req["actor"]["principal_id"], "channel": req["channel"],
                                            "comment": req.get("comment"), "answer_text": req.get("answer_text"),
                                            "decided_at": req.get("decided_at"),
                                            "override_release_block": req.get("override_release_block")},
                                        source="update")

    async def _fill_text_sha(self, g: GateRecord, decision_id: str, handle: Any) -> None:
        try:
            r = await handle
            for e in g.decision_log:
                if e["decision_id"] == decision_id and r.get("text_sha256"):
                    e["text_sha256"] = r["text_sha256"]
                    self.emit("gate.approval_recorded", f"{g.kind} approval record written", gate_id=g.gate_id,
                              decision_id=decision_id)
        except Exception:  # noqa: BLE001 - the record is retried by Temporal and reconciled by WS-08
            pass
        finally:
            self._sha_pending -= 1

    def _superseded(self, g: GateRecord, req: dict[str, Any]) -> dict[str, Any]:
        """Another decision won (C-MSG handler step 2/4). Logged and projected as gate.decision_superseded,
        which the projector audits (XD-FIX-R1-157)."""
        self._log_decision(g, req, "superseded")
        self.emit("gate.decision_superseded", f"{g.kind} decision via {req.get('channel')} superseded",
                  gate_id=g.gate_id, decision_id=req["decision_id"])
        return {"schema": "replicant.msg.gate-decide-result/v1", "outcome": "superseded", "gate_id": g.gate_id,
                "decision_id": req["decision_id"], **_winning(g)}

    def _log_decision(self, g: GateRecord, d: dict[str, Any], outcome: str, *, denial_reason: str | None = None) -> None:
        """GateView.decision_log entry (CCR-07-07): every attempt the workflow saw, newest last, at most 20 carried
        (the contract allows 50). text_key points at the approval_text object, never the text."""
        channel = d.get("channel")
        principal = (d.get("actor") or {}).get("principal_id") or d.get("principal_id")
        if channel not in LOG_CHANNELS or not principal:
            return  # scripted (system) and API decisions are not approver attempts
        e = {"decision_id": d["decision_id"], "outcome": outcome, "decision": d["decision"], "principal_id": principal,
             "channel": channel, "decided_at": str(d.get("decided_at") or workflow.now().isoformat()),
             "presented_digest": d.get("presented_digest") or g.presented_digest}
        if denial_reason:
            e["denial_reason"] = denial_reason
        if d.get("comment") or d.get("answer_text"):
            e["text_key"] = f"tenants/{self.s.tenant_id}/jobs/{self.s.job_id}/gates/{g.gate_id}/text-{d['decision_id']}.json"
        g.decision_log = (g.decision_log + [e])[-20:]

    def _apply_decision(self, g: GateRecord, d: dict[str, Any], *, source: str,
                        rule_index: int | None = None) -> dict[str, Any]:
        """THE gate transition (ADR-01-009). Called by the gate.decide handler (after V1-V10 and the
        authoritative identity check) and by the evaluation-mode resolver (XD-06-02)."""
        now = workflow.now()
        decision_id = d["decision_id"]
        if d["decision"] == "stop_case":  # XD-FIX-R1-143: no decision object, no approval record, gate cancelled
            g.state = "cancelled"
            self.s.remember(decision_id, now)
            self._gate_closed(g)
            self.emit("gate.auto_resolved", f"{g.kind} stop_case by gate script rule {rule_index}", gate_id=g.gate_id)
            self._wake += 1
            return {}
        g.decision = {"decision_id": decision_id, "decision": d["decision"], "principal_id": d["principal_id"],
                      "channel": d["channel"], "decided_at": now.isoformat(),
                      **({"comment": d["comment"][:8000]} if d.get("comment") else {})}
        if d["decision"] == "answer":
            g.decision["answer_ref"] = f"{source}:{decision_id}"  # answer text stays in the Update / script
        if source == "script":
            g.decision["source"] = "script"  # R1-019: approval record source; not part of WinningDecision
        if d.get("override_release_block"):  # V12 override, audited through the approval record (CCR-06-13)
            g.decision["override_release_block"] = dict(d["override_release_block"])
        g.state = GATE_STATE_FOR.get(d["decision"], "cancelled")  # stop_case (evaluation) cancels the gate
        if d["decision"] == "request_changes" and d.get("comment"):
            # R2-019: the comment is staged for the next producing node as an untrusted approver_feedback block
            self.s.approver_feedback = (self.s.approver_feedback + [{
                "gate_id": g.gate_id, "gate_kind": g.kind, "decision_id": decision_id, "cycle": g.cycle,
                "text": d["comment"][:8000]}])[-3:]
        if d["decision"] == "answer" and d.get("answer_text"):
            self.s.clarification_answer = {"decision_id": decision_id, "text": d["answer_text"][:16000]}  # R1-017
        if d["decision"] == "rerun_qa":  # CCR-05-04: re-run QA on the same diff, no fix iteration consumed
            self.s.qa_reruns += 1
            self.s.qa_rerun_pending = True
        if source == "update":
            self._log_decision(g, {**d, "presented_digest": g.presented_digest}, "accepted")
        self.s.remember(decision_id, now)
        result = {"schema": "replicant.msg.gate-decide-result/v1", "outcome": "accepted", "gate_id": g.gate_id,
                  "decision_id": decision_id, **_winning(g)}
        self.s.applied_results[decision_id] = result
        self._gate_closed(g)
        if source == "script":
            self.emit("gate.auto_resolved", f"{g.kind} {d['decision']} by gate script rule {rule_index}",
                      gate_id=g.gate_id, decision_id=decision_id)
        else:
            self.emit("gate.decided", f"{g.kind} {d['decision']} via {d['channel']}",
                      gate_id=g.gate_id, decision_id=decision_id)
        # C-ART 1.9.0: the activity writes the free text (comment, answer_text) as `approval_text` in artifacts
        # and a reference-only `approval_record` (text_key, text_sha256) in the audit bucket. Both handles are
        # awaited before Continue-As-New or close (R2-013).
        rec_h = workflow.start_activity(
            "gates.write_approval_record", {"job_id": self.s.job_id, "tenant_id": self.s.tenant_id, "gate_id": g.gate_id,
            "gate_kind": g.kind, "decision": g.decision, "answer_text": d.get("answer_text"), "packet_uri": g.packet_uri,
            "source": source, "presented_digest": g.presented_digest, "subject": g.subject},
            activity_id=ids.logical_activity_id(g.node_key, g.cycle, g.ordinal, f"record_{g.kind[:8]}"),
            **_opts("gates.write_approval_record"))
        self._audit.append(rec_h)
        if source == "update" and (d.get("comment") or d.get("answer_text")):
            # R1V-046: the activity that writes approval_text returns text_sha256; it goes into decision_log
            self._sha_pending += 1
            asyncio.ensure_future(self._fill_text_sha(g, decision_id, rec_h))
        if self.notifications_enabled():
            self._audit.append(workflow.start_activity(
                "notify.gate_resolved", {"job_id": self.s.job_id, "tenant_id": self.s.tenant_id, "gate_id": g.gate_id,
                "decision": g.decision, "channels": g.channels},
                activity_id=ids.logical_activity_id(g.node_key, g.cycle, g.ordinal, f"notify_res_{g.kind[:8]}"),
                **_opts("notify.gate_resolved")))
        if g.kind == "bundle_migration":
            self._on_migration_decided(g)
        self._wake += 1
        return result

    def validate_gate_decide(self, req: dict[str, Any]) -> None:
        self._common_validate(req, "decision_id", "replicant.msg.gate-decide/v1")  # V1
        if not _ID["gate_id"].match(str(req.get("gate_id"))) or not _ID["digest"].match(str(req.get("presented_digest"))):
            raise _reject("InvalidPayload", "gate_id or presented_digest invalid")
        if req.get("decision") == "answer" and not req.get("answer_text"):
            raise _reject("InvalidPayload", "answer_text required")
        g = self.s.gates.get(req["gate_id"])
        if g is None:
            raise _reject("UnknownGate", "gate not opened by this workflow")  # V2
        if self.s.seen(req["decision_id"]):
            raise _reject("DuplicateDecision", "already applied", self.s.applied_results.get(req["decision_id"]))  # V3
        if g.state in ("expired", "cancelled"):
            raise _reject("GateClosed", "gate closed")  # V4
        if g.state != "open":
            raise _reject("GateAlreadyDecided", "gate already decided", g.decision)  # V4
        if req.get("decision") not in DECISIONS_BY_KIND[g.kind]:
            raise _reject("DecisionNotAllowed", f"{req.get('decision')} not allowed for {g.kind}")  # V5
        if req["decision"] == "rerun_qa" and (self.s.last_qa_verdict != "inconclusive" or self.s.qa_reruns >= 2):
            raise _reject("DecisionNotOffered", "rerun_qa needs an inconclusive QA result; at most 2 per gate")  # V5
        if req["decision"] == "approve" and g.release_block and not req.get("override_release_block"):
            raise _reject("GateBlockedByRelease", "bundle rolled back or revoked; approve needs override_release_block",
                          g.release_block)  # V12 (CCR-06-13)
        if req["presented_digest"] != g.presented_digest:
            raise _reject("StaleApprovalView", "packet changed", g.presented_digest)  # V6
        if req["channel"] not in g.channels:
            raise _reject("ChannelNotAllowed", f"{req['channel']} not allowed")  # V7
        if not req["actor"]["principal_id"].startswith("usr_"):
            raise _reject("ActorNotHuman", "services never decide gates")  # V8
        if not g.eligible.get("requester_may_decide", False) and req["actor"]["principal_id"] == self.s.requester:
            raise _reject("SelfApprovalForbidden", "requester may not decide this gate")  # V9
        # V10 (R2-014, CCR-01-07): decided_at is control-plane-api's receipt time of the click. A decision
        # stamped before expires_at is accepted while the gate is still open (an outage may delay the
        # Update past expiry), within MAX_DECISION_DELAY and the post-outage grace window.
        now = workflow.now()
        try:
            decided_at = datetime.fromisoformat(str(req.get("decided_at")).replace("Z", "+00:00"))
        except ValueError:
            raise _reject("InvalidPayload", "decided_at") from None
        if decided_at > now + DECISION_SKEW or decided_at < datetime.fromisoformat(g.opened_at) - DECISION_SKEW:
            raise _reject("InvalidPayload", "decided_at outside the gate's lifetime")
        if g.expires_at and not g.held and now >= datetime.fromisoformat(g.expires_at):
            in_time = decided_at < datetime.fromisoformat(g.expires_at) and now - decided_at <= MAX_DECISION_DELAY
            if not in_time or (g.grace_until and now >= datetime.fromisoformat(g.grace_until)):
                raise _reject("GateClosed", "gate expired")  # V10

    async def job_control(self, req: dict[str, Any]) -> dict[str, Any]:
        s, action = self.s, req["action"]
        now = workflow.now()
        if s.status in TERMINAL_STATUSES:
            s.remember(req["action_id"], now)
            return _ctl(req, "no_op", s.status)
        check = await self._check_action(action, req)
        s.remember(req["action_id"], now)
        if not check["allow"]:
            return _ctl(req, "denied", s.status, denial_reason=check["reason"])
        if action == "cancel":
            self.cancel_requested = True
            self._cancel_actor = req["actor"]["principal_id"]
            if self._main and not self._main.done():
                self._main.cancel()
            return _ctl(req, "accepted", "cancelled")
        if action == "pause":
            if s.paused:
                return _ctl(req, "no_op", s.status)
            s.paused, s.pause_mode = True, req["pause_mode"]
            s.paused_by = req["actor"]["principal_id"]
            self.emit("job.paused", f"pause requested ({req['pause_mode']})")
            return _ctl(req, "accepted", s.status)
        if action == "resume":
            if s.hold and s.hold.get("kind") in ("dependency", "platform_dependency") and not s.paused:  # "retry now"
                self._wake += 1
                self.emit("job.resumed", "operator retry of the blocked dependency")
                return _ctl(req, "accepted", s.status)
            if not s.paused:
                return _ctl(req, "no_op", s.status)
            s.paused, s.pause_mode = False, None
            self._wake += 1
            self.emit("job.resumed", "resume requested")
            return _ctl(req, "accepted", "running" if s.status == "paused" else s.status)
        if action == "takeover":
            s.takeover_ordinal += 1
            tko = ids.takeover_id_for(s.job_id, s.takeover_ordinal)
            self._takeover = {"takeover_id": tko, "include": req.get("takeover_include") or
                              ["branch", "workspace_archive", "transcripts", "plan", "handoff"]}
            self.cancel_requested = True
            if self._main and not self._main.done():
                self._main.cancel()
            return {**_ctl(req, "accepted", "taken_over"), "takeover_id": tko}
        return _ctl(req, "no_op", s.status)

    def validate_job_control(self, req: dict[str, Any]) -> None:
        self._common_validate(req, "action_id", "replicant.msg.job-control/v1")
        if req.get("action") not in ("cancel", "pause", "resume", "takeover"):
            raise _reject("InvalidPayload", "action not valid for ReplicantJob")
        if req["action"] == "pause" and req.get("pause_mode") not in ("at_next_node", "at_next_gate"):
            raise _reject("InvalidPayload", "pause_mode required")
        if self.s.seen(req["action_id"]):
            raise _reject("DuplicateAction", "already applied")
        if not req["actor"]["principal_id"].startswith("usr_"):
            raise _reject("ActorNotHuman", "job control requires a human actor")

    async def job_steer(self, req: dict[str, Any]) -> dict[str, Any]:
        s = self.s
        check = await self._check_action("steer", req)
        now = workflow.now()
        s.remember(req["message_id"], now)
        res = {"schema": "replicant.msg.job-steer-result/v1", "message_id": req["message_id"]}
        if not check["allow"]:
            return {**res, "outcome": "denied", "denial_reason": check["reason"][:200]}
        live = [r for r in s.runs.values() if r.active and r.steering_enabled and r.phase == "Running"]
        if req["delivery"] in ("auto", "live_only") and live:
            r = live[0]
            s.steer_seq += 1
            try:
                await workflow.execute_activity("runner.post_message", {
                    "run_id": r.run_id, "message_id": req["message_id"], "text": req["text"],
                    "author": req["actor"]["principal_id"], "sent_at": req["sent_at"]},
                    activity_id=ids.logical_activity_id(r.node_key, r.cycle, r.ordinal, f"steer_{s.steer_seq}"),
                    **_opts("runner.post_message"))
            except ActivityError:  # the run ended or the Runner is down: fall back to the next node
                if req["delivery"] == "live_only":
                    return {**res, "outcome": "not_deliverable"}
                s.steering_queue.append({"message_id": req["message_id"], "text": req["text"],
                                         "author": req["actor"]["principal_id"], "sent_at": req["sent_at"]})
                s.steering_log.append({"message_id": req["message_id"], "outcome": "queued_next_node", "at": now.isoformat(),
                               "actor": req["actor"]["principal_id"]})
                self.emit("steer.received", "steering queued for the next node", message_id=req["message_id"])
                return {**res, "outcome": "queued_next_node"}
            s.steering_log.append({"message_id": req["message_id"], "outcome": "delivered_live", "at": now.isoformat(),
                                   "actor": req["actor"]["principal_id"]})
            self.emit("steer.delivered", "steering delivered live", run_id=r.run_id, message_id=req["message_id"])
            return {**res, "outcome": "delivered_live", "run_id": r.run_id}
        if req["delivery"] == "live_only":
            return {**res, "outcome": "not_deliverable"}
        s.steering_queue.append({"message_id": req["message_id"], "text": req["text"],
                                 "author": req["actor"]["principal_id"], "sent_at": req["sent_at"]})
        s.steering_log.append({"message_id": req["message_id"], "outcome": "queued_next_node", "at": now.isoformat(),
                               "actor": req["actor"]["principal_id"]})
        self.emit("steer.received", "steering queued for the next node", message_id=req["message_id"])
        return {**res, "outcome": "queued_next_node"}

    def validate_job_steer(self, req: dict[str, Any]) -> None:
        self._common_validate(req, "message_id", "replicant.msg.job-steer/v1")
        if not isinstance(req.get("text"), str) or not 1 <= len(req["text"]) <= 16000:
            raise _reject("InvalidPayload", "text length")
        if req.get("delivery") not in ("auto", "next_node", "live_only"):
            raise _reject("InvalidPayload", "delivery")
        if self.s.seen(req["message_id"]):
            raise _reject("DuplicateMessage", "already applied")
        if self.s.status in TERMINAL_STATUSES:
            raise _reject("JobTerminal", "job is terminal")
        if not req["actor"]["principal_id"].startswith("usr_"):
            raise _reject("ActorNotHuman", "steering requires a human actor")
        if len(self.s.steering_queue) >= 8 and req.get("delivery") == "next_node":
            raise _reject("SteeringBacklogFull", "8 queued messages; wait for the next node")

    async def offer_bundle_migration(self, req: dict[str, Any]) -> dict[str, Any]:
        s = self.s
        check = await self._check_action("offer_bundle_migration", req)
        now = workflow.now()
        s.remember(req["action_id"], now)
        base = {"schema": "replicant.msg.offer-bundle-migration-result/v1"}
        if not check["allow"]:
            return {**base, "outcome": "denied"}
        try:
            el = await workflow.execute_activity("release.check_migration_eligibility", {
                "job_id": s.job_id, "current_release_bundle_id": s.release_bundle_id,
                "target_release_bundle_id": req["target_release_bundle_id"], "workflow_type": s.workflow_type,
                "resume_at": s.resume_at, "carry_schema": "replicant.wf-carry/v1", "tenant_id": s.tenant_id,
                "lineage": "any", "open_gate_kinds": sorted({g.kind for g in s.gates.values() if g.state == "open"})},
                **_opts("release.check_migration_eligibility"))
        except ActivityError as e:  # the handler never fails after validation; the operator retries the offer
            return {**base, "outcome": "ineligible", "ineligible_reason": f"eligibility check unavailable ({_etype(e)})"}
        if not el["eligible"]:
            return {**base, "outcome": "ineligible", "ineligible_reason": el["reason"][:500]}
        if req.get("consent") == "operator":  # CCR-06-13: release:revoke, no gate, migrate at the next safe point
            op = await self._check_action("migrate_operator_consent", req)
            if not op["allow"]:
                return {**base, "outcome": "denied"}
            s.migration = {"target_release_bundle_id": req["target_release_bundle_id"], "target_build_id": el["target_build_id"],
                           "state": "approved", "required": bool(req.get("required")),
                           "consent": f"operator:{req['actor']['principal_id']}"}
            self.emit("bundle.migration_forced", f"operator scheduled migration to {req['target_release_bundle_id'][:12]}")
            await self._notify("notify.job_event", {"job_id": s.job_id, "kind": "migration_scheduled_by_operator",
                               "to": s.requester, "target_release_bundle_id": req["target_release_bundle_id"],
                               "reason": (req.get("reason") or "")[:500]},
                               node_key="release", effect="notify_operator_migrate", ordinal=min(s.next_child("op_migrate"), 999))
            self._wake += 1
            return {**base, "outcome": "migration_scheduled"}
        kind = "bundle_migration"
        gord = s.next_gate_ordinal(kind)
        gate_id = ids.gate_id_for(s.job_id, kind, s.cycle, gord)
        routing = await self._aux_act("gates.resolve_routing", {"gate_kind": kind, "facts": self.gate_facts(kind, {
                                      "target_release_bundle_id": req["target_release_bundle_id"],
                                      "required": bool(req.get("required"))})},
                                      node_key="release", effect="route_migrate", ordinal=gord)
        rules = {**routing["timing"], "channels": routing["channels"]}
        built = await self._aux_act("gates.build_packet", {"job_id": s.job_id, "tenant_id": s.tenant_id,
                                    "gate_id": gate_id, "kind": kind, "revision": 1, "refs": {},
                                    "risk": s.risk, "extra": {"target": req["target_release_bundle_id"],
                                                              "diff": el.get("change_summary", ""),
                                                              "required": bool(req.get("required"))}},
                                    node_key="release", effect="packet_migrate_r1", ordinal=gord)
        g = GateRecord(gate_id=gate_id, kind=kind, node_key="release", cycle=s.cycle, ordinal=gord, state="open",
                       opened_at=now.isoformat(), presented_digest=built["digest"], packet_uri=built["packet_uri"],
                       channels=list(rules["channels"]),
                       eligible={"principals": [s.requester], "requester_may_decide": True},
                       expires_at=(now + timedelta(seconds=rules["expiry_seconds"])).isoformat(),
                       schedule=dict(routing["timing"]))
        s.gates[gate_id] = g
        s.migration = {"target_release_bundle_id": req["target_release_bundle_id"],
                       "target_build_id": el["target_build_id"], "state": "offered", "gate_id": gate_id,
                       "required": bool(req.get("required"))}
        self._upsert_gate_sas()
        self.emit("bundle.migration_offered", f"migration to {req['target_release_bundle_id'][:12]} offered", gate_id=gate_id)
        await self._notify("notify.gate_opened", {"job_id": s.job_id, "gate_id": gate_id, "kind": kind,
                           "channels": g.channels, "packet_uri": g.packet_uri, "eligible": g.eligible},
                           node_key="release", effect="notify_open_migrate", ordinal=gord)
        return {**base, "outcome": "gate_opened", "gate_id": gate_id}

    def validate_offer_bundle_migration(self, req: dict[str, Any]) -> None:
        self._common_validate(req, "action_id", "replicant.msg.offer-bundle-migration/v1")
        if not _ID["bundle"].match(str(req.get("target_release_bundle_id", ""))):
            raise _reject("InvalidPayload", "target bundle id")
        if self.s.seen(req["action_id"]):
            raise _reject("DuplicateAction", "already applied")
        if self.s.status not in ("waiting_human", "waiting_external"):
            raise _reject("NotAtSafePoint", "job is not waiting")
        if req["target_release_bundle_id"] == self.s.release_bundle_id:
            raise _reject("SameBundle", "target equals current bundle")
        if self.s.migration and self.s.migration.get("state") in ("offered", "approved"):
            raise _reject("MigrationPending", "a migration offer is already open")
        if req.get("consent", "requester") not in ("requester", "operator"):
            raise _reject("InvalidPayload", "consent")

    async def retry_node(self, req: dict[str, Any]) -> dict[str, Any]:
        """Update ops.retry_node (C-MSG 2.0.0, CCR-07-12). Re-runs the named node's stuck Runner run with
        ordinal + 1, so activity IDs never collide with earlier results. Every check is in the handler and a
        failed check returns accepted=false with the reason, so operators always get an answer."""
        s = self.s
        s.remember(req["operation_id"], workflow.now())
        res = {"schema": "replicant.msg.retry-node-result/v1"}
        key = req["node_key"]
        if s.status in TERMINAL_STATUSES:
            return {**res, "accepted": False, "denial_reason": "job_terminal"}
        if self._spec is None or key not in self._spec.nodes:
            return {**res, "accepted": False, "denial_reason": "node_unknown"}
        if self._spec.nodes[key].type in ("human_gate", "pr_feedback_wait", "publisher"):
            return {**res, "accepted": False, "denial_reason": "not_allowed"}  # gates and pushes are never re-run
        active = [r for r in s.runs.values() if r.active and r.node_key == key and r.cycle == s.cycle]
        if not active or key in s.retry_nodes:
            return {**res, "accepted": False, "denial_reason": "not_failed"}
        s.retry_nodes[key] = req["operation_id"]
        self._wake += 1
        self.emit("node.retry_requested", f"operator retry of {key} requested", node_key=key)
        return {**res, "accepted": True, "new_ordinal": max(2, s.ordinals.get(f"{key}@c{s.cycle}", 1) + 1)}

    def validate_retry_node(self, req: dict[str, Any]) -> None:
        if not isinstance(req, dict) or req.get("schema") != "replicant.msg.retry-node/v1" \
                or not _ID["operation_id"].match(str(req.get("operation_id", ""))) \
                or not re.match(r"^[a-z][a-z0-9_]{0,40}$", str(req.get("node_key", ""))) \
                or not 3 <= len(str(req.get("reason", ""))) <= 2000 \
                or not _ID["principal"].match(str((req.get("actor") or {}).get("principal_id", ""))):
            raise _reject("InvalidPayload", "payload does not match replicant.msg.retry-node/v1")
        if self.s.seen(req["operation_id"]):
            raise _reject("DuplicateOperation", "already applied")

    def _on_migration_decided(self, g: GateRecord) -> None:
        m = self.s.migration or {}
        if g.state == "approved":
            self.s.migration = {**m, "state": "approved"}
        else:
            required = m.get("required")
            self.s.migration = None
            if required:  # declining (or letting expire) a required migration ends the job (R1-004)
                self.s.forced_outcome = next((DEADLINE_OUTCOME[k] for k in ("bundle_revoked", "model_retiring",
                                              "worker_version_retire_scheduled") if k in self.s.deadlines),
                                             "bundle_revoked_not_migrated")
                self.cancel_requested = True
                if self._main and not self._main.done():
                    self._main.cancel()
        self._wake += 1

    # ============================================================ Signals (C-MSG 2.2)
    def on_run_status_changed(self, sig: dict[str, Any]) -> None:
        run_id = sig.get("run_id")
        r = self.s.runs.get(run_id) if isinstance(run_id, str) else None
        if r is not None and r.active:
            self.hints[run_id] = True
        else:  # C-MSG 1.1: counted (replay-safe metric meter) and logged, never acted on (R1-030)
            self._unknown_signals.add(1)
            workflow.logger.info("unknown run.status_changed signal", extra={"run_id": str(run_id)[:40]})

    def on_pr_event(self, sig: dict[str, Any]) -> None:
        d = sig.get("delivery_id")
        if not isinstance(d, str) or d in self.s.deliveries or sig.get("job_id") != self.s.job_id:
            return
        now = workflow.now()
        self.s.deliveries[d] = now.timestamp()
        self.s.pr_buffer.append({k: sig.get(k) for k in ("delivery_id", "event", "actor", "payload_uri",
                                                          "head_sha", "review_state", "received_at")})
        self.s.pr_last_activity_at = now.isoformat()
        self.s.pr_reminders_sent = 0
        self.emit("pr.event", f"PR event {sig.get('event')}", delivery_id=d)

    def on_release_notice(self, sig: dict[str, Any]) -> None:
        n = sig.get("notice_id")
        if not isinstance(n, str) or n in self.s.notices:
            return
        self.s.notices[n] = workflow.now().timestamp()
        kind = sig["kind"]
        self.s.pending_notices = (self.s.pending_notices + [kind])[-20:]
        if sig.get("current_release_bundle_id") not in (None, self.s.release_bundle_id) and kind != "worker_version_retire_scheduled":
            self.emit("notice.received", f"release notice {kind} for another bundle ignored")
            return
        if kind in DEADLINE_OUTCOME:
            # R1-004: revoked bundle -> hold with status waiting_human at the next safe point for the required
            # offer; worker retirement and model retirement -> must migrate before the deadline.
            default = timedelta(days=14)
            at = sig.get("deadline") or (workflow.now() + default).isoformat()
            self.s.deadlines[kind] = at.replace("Z", "+00:00")
            if kind == "bundle_revoked":
                self.s.revoked_deadline = self.s.deadlines[kind]
        if kind in ("bundle_rolled_back", "bundle_revoked"):  # CCR-06-13 / XD-FIX-R2-021: block approvals (V12)
            blk = {"release_bundle_id": self.s.release_bundle_id, "rolled_back_at": sig.get("issued_at") or workflow.now().isoformat(),
                   "actor": sig.get("issued_by") or "svc_release-controller", "reason": (sig.get("reason") or kind)[:2000]}
            self.s.release_blocked = blk
            for g in self.s.gates.values():
                if g.state == "open" and g.kind in ("plan_approval", "publish_approval"):
                    g.release_block = dict(blk)
                    self.emit("gate.release_blocked", f"{g.kind} gate blocked: bundle {kind.split('_', 1)[1].replace('_', ' ')}",
                              gate_id=g.gate_id)
        if kind in ("bundle_patched", "model_retiring"):
            self.s.notice_queue.append({k: sig.get(k) for k in ("kind", "notice_id", "recommended_release_bundle_id",
                                                                "deadline")})
        self._wake += 1
        self.emit("notice.received", f"release notice {kind}")

    def on_principal_changed(self, sig: dict[str, Any]) -> None:
        n = sig.get("notice_id")
        if not isinstance(n, str) or n in self.s.notices:
            return
        self.s.notices[n] = workflow.now().timestamp()
        p, change = sig.get("principal_id"), sig.get("change")
        if p == self.s.requester and change in ("deprovisioned", "suspended"):
            self.s.paused, self.s.pause_mode = True, "at_next_node"  # tenant policy default: pause, notify admins
            self.emit("notice.received", "requester deprovisioned; job paused for tenant admin review")
        if change == "decision_identity_contested" and sig.get("decision_id"):  # CCR-07-06, XD-FIX-R1-173
            self.s.notice_queue.append({"kind": "contested", "decision_id": sig["decision_id"], "principal_id": p})
            self._wake += 1
            return
        sole = any(g.state == "open" and g.eligible.get("principals") == [p] for g in self.s.gates.values())
        if sole or (p == self.s.requester and change in ("deprovisioned", "suspended")):
            self.s.notice_queue.append({"kind": "principal_changed", "principal_id": p, "change": change})
            self._wake += 1

    def on_kill_switch(self, sig: dict[str, Any]) -> None:
        n = sig.get("notice_id")
        if not isinstance(n, str) or n in self.s.notices:
            return
        self.s.notices[n] = workflow.now().timestamp()
        self._apply_kill_switch(sig)
        self.emit("notice.kill_switch", f"operators {sig['state']} {'/'.join(sig['switches'])} ({sig['scope']})")

    async def _check_action(self, action: str, req: dict[str, Any]) -> dict[str, Any]:
        try:  # handlers never fail after validation (R2-011)
            return await workflow.execute_activity("identity.check_action", {
                "job_id": self.s.job_id, "tenant_id": self.s.tenant_id, "action": action,
                "principal_id": req["actor"]["principal_id"], "requester": self.s.requester,
                "repos": [t["repo"] for t in self.s.targets]}, **_opts("identity.check_action"))
        except ActivityError:
            return {"allow": False, "reason": "identity_unverifiable"}

    # ============================================================ Queries (C-MSG 2.3)
    def q_open_gates(self) -> dict[str, Any]:
        return {"schema": "replicant.msg.open-gates/v1",
                "gates": [_gate_view(g) for g in self.s.gates.values() if g.state == "open"]}

    def q_working_memory_refs(self) -> dict[str, Any]:
        r = self.s.refs
        out = {"schema": "replicant.msg.working-memory-refs/v1"}
        for k in ("plan_uri", "latest_handoff_uri", "latest_diff_bundle_uri", "latest_session_state_uri",
                  "latest_qa_report_uri", "briefing_uri"):
            if r.get(k):
                out[k] = r[k]
        if self.s.memory_snapshot_id:
            out["memory_snapshot_id"] = self.s.memory_snapshot_id
        return out


# ---------------------------------------------------------------- helpers
def steering_backlog_input(s: JobState) -> dict[str, Any]:
    """steering.stage_backlog input (WS-04 steering-stage-backlog.schema.json, XD-W2-04-08): messages[] in the
    same order as message_ids[], each exactly {message_id, text, author, sent_at}."""
    msgs = [{k: m[k] for k in ("message_id", "text", "author", "sent_at")} for m in s.steering_queue][:50]
    return {"job_id": s.job_id, "node_key": s.resume_at or "steering", "cycle": s.cycle,
            "ordinal": min(max(s.segment, 1), 999), "message_ids": [m["message_id"] for m in msgs], "messages": msgs}


def _etype(e: ActivityError) -> str:
    c = e.cause
    if isinstance(c, ApplicationError) and c.type:
        return c.type
    return type(c).__name__ if c is not None else "ActivityError"


def _opts(name: str) -> dict[str, Any]:
    d = CATALOG[name].klass
    return {"task_queue": d.task_queue, "start_to_close_timeout": d.start_to_close,
            "schedule_to_close_timeout": d.schedule_to_close, "heartbeat_timeout": d.heartbeat,
            "retry_policy": d.retry}


def _target(t: dict[str, Any]) -> dict[str, Any]:
    r = t["repo"]
    return {"repo": f"{r['host']}/{r['owner']}/{r['name']}", "repo_ref": dict(r),
            "base_ref": t.get("base_ref"), "base_sha": t.get("base_sha"),
            "pull_request": t.get("pull_request")}


def _summary(jr: dict[str, Any]) -> dict[str, Any]:
    c, o = jr.get("constraints") or {}, jr.get("options") or {}
    return {"workflow_type": jr.get("workflow_type"), "require_gates": list(c.get("require_gates", [])),
            "budget_usd_max": c.get("budget_usd_max"), "dry_run": bool(o.get("dry_run")),
            "draft_pr": bool(o.get("draft_pr")), "channel": jr["source"]["channel"],
            "labels": list(c.get("labels", [])), "paths": [p for t in jr.get("targets", []) for p in t.get("paths", [])][:50],
            "received_at": jr.get("received_at")}  # build_run_plan input (XD-W2-11-01)


def _evaluation(jr: dict[str, Any]) -> dict[str, Any] | None:
    ev = (jr.get("options") or {}).get("evaluation")
    if not ev:
        return None
    case = (jr.get("related") or {}).get("eval_case") or {}
    return {**ev, **{k: case[k] for k in ("eval_run_id", "golden_task_id", "arm", "rep") if case.get(k) is not None}}


def _winning(g: GateRecord) -> dict[str, Any]:
    if not g.decision:
        return {}
    return {"winning_decision": {k: g.decision[k] for k in ("decision_id", "decision", "principal_id", "channel",
                                                             "decided_at", "comment") if k in g.decision}}


def _gate_view(g: GateRecord) -> dict[str, Any]:
    v = {"gate_id": g.gate_id, "kind": g.kind, "state": g.state, "opened_at": g.opened_at,
         "presented_digest": g.presented_digest, "packet_uri": g.packet_uri, "channels": g.channels,
         "escalation_level": g.escalation_level,
         "eligible": {k: v for k, v in g.eligible.items()
                      if k in ("okta_groups", "principals", "min_repo_permission", "requester_may_decide")}}
    if g.expires_at:
        v["expires_at"] = g.expires_at
    if g.release_block:
        v["release_block"] = dict(g.release_block)
    if g.decision_log:
        v["decision_log"] = [dict(e) for e in g.decision_log]
    if g.next_reminder_at:
        v["next_reminder_at"] = g.next_reminder_at
    w = _winning(g)
    if w:
        v["decision"] = w["winning_decision"]
    return v


def _ctl(req: dict[str, Any], outcome: str, status: str, **extra: Any) -> dict[str, Any]:
    return {"schema": "replicant.msg.job-control-result/v1", "outcome": outcome, "action_id": req["action_id"],
            "status": status, **extra}


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
