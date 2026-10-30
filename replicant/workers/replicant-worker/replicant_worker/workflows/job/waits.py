"""ReplicantJob: interrupt waits: human gates (expiry, reminders, escalation) and the PR feedback wait.

A mixin of ReplicantJobBase (workflows/replicant_job.py); it runs in workflow code under the sandbox."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow

from replicant_worker.workflows.job.common import EXPIRY_GRACE, LATE_FIRE, _Can
from replicant_worker.workflows.state import GateRecord


class WaitsMixin:
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
                    return {"event": "merged" if ev["event"] == "pull_request_merged" else "closed",
                            **{k: ev[k] for k in ("payload_uri", "head_sha") if ev.get(k)}}
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
