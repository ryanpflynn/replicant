"""ReplicantJob: signal handlers (C-MSG 2.2) and the identity.check_action helper. Registration stays on ReplicantJob.

A mixin of ReplicantJobBase (workflows/replicant_job.py); it runs in workflow code under the sandbox."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError

from replicant_worker.workflows.job.common import DEADLINE_OUTCOME, _opts


class SignalsMixin:
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
