"""ReplicantJob: activity calls and holds, kill switches, deadlines and notices (R2-010, R2-012).

A mixin of ReplicantJobBase (workflows/replicant_job.py); it runs in workflow code under the sandbox."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, RetryState

from replicant_worker import ids
from replicant_worker.activities.options import CATALOG
from replicant_worker.registry import NodeFailed
from replicant_worker.workflows.job.common import DEADLINE_OUTCOME, DEPENDENCY_BLOCK_DEFAULTS, SA_BUNDLE, _etype, _opts


class ActivitiesMixin:
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
