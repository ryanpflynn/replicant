"""ReplicantJob: node execution (the engine callback), node-boundary safe points and the helpers nodes call on the host.

A mixin of ReplicantJobBase (workflows/replicant_job.py); it runs in workflow code under the sandbox."""

from __future__ import annotations

import asyncio
from typing import Any

from temporalio import workflow
from temporalio.common import Priority

from replicant_worker import ids
from replicant_worker.graphs.dsl import Guard
from replicant_worker.graphs.engines.base import BoundaryReached
from replicant_worker.registry import NodeFailed
from replicant_worker.registry import get as get_node_type
from replicant_worker.workflows.context import NodeContext
from replicant_worker.workflows.job.common import (
    PAUSE_EXPIRY, SA_HUMAN, SA_OPEN_GATES, SA_PHASE, SA_RISK, SA_STATUS, SA_WAITING,
)
from replicant_worker.workflows.state import GateRecord


class NodesMixin:
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
        # The projection follows a wait on an external event (PR review) at once: GET /v1/jobs, the lists and the
        # Simple view's "PR #N is ready for review" read `waiting_external` (MEM-PR-D10). Leaving the wait is covered
        # by the node or terminal event that follows.
        if sas and sas[0].key.name == "ReplicantStatus" and status == "waiting_external" and \
                workflow.patched("status-waiting-external-v1"):
            pr = (self.s.pr or {}).get("number")
            self.emit("job.waiting", f"Waiting for review on PR #{pr}" if pr and phase == "pr_review"
                      else f"Waiting on an external event ({phase})")

    async def _signal_parent(self, status: str, phase: str) -> None:
        """SignalExternalWorkflowExecution to the MaintenanceBatch parent (ADR-01-007, CCR-01-04).
        Fire-and-forget: a missing or closed parent never affects the child."""
        try:
            await workflow.get_external_workflow_handle(self.s.progress_workflow_id or self.s.parent_job_id).signal(  # type: ignore[arg-type]
                "batch.child_progress", {"schema": "replicant.msg.batch-child-progress/v1", "child_job_id": self.s.job_id,
                                         "status": status, "phase": phase, "seq": self.s.projection_seq + 1})
        except Exception as e:
            workflow.logger.info("parent progress signal failed", extra={"error": str(e)[:200]})
