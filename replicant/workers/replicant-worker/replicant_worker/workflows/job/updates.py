"""ReplicantJob: update handlers and validators (C-MSG 2.1). Registration stays on ReplicantJob.

A mixin of ReplicantJobBase (workflows/replicant_job.py); it runs in workflow code under the sandbox."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError

from replicant_worker import ids
from replicant_worker.workflows.job.common import (
    _ID, DEADLINE_OUTCOME, DECISION_SKEW, DECISIONS_BY_KIND, GATE_STATE_FOR, LOG_CHANNELS, MAX_DECISION_DELAY,
    TERMINAL_STATUSES, _ctl, _etype, _opts, _reject, _winning,
)
from replicant_worker.workflows.state import GateRecord


class UpdatesMixin:
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
        if d["decision"] in ("request_changes", "reject") and d.get("comment") and not self.s.evaluation \
                and source != "script" and workflow.patched("memory-gate-feedback-v1"):
            # MEM (DEVIATIONS MEM-D3): an approver's comment becomes memory feedback -> extraction -> candidates
            self._audit.append(workflow.start_activity(
                "memory.ingest_gate_feedback", {"job_id": self.s.job_id, "tenant_id": self.s.tenant_id,
                "gate_id": g.gate_id, "gate_kind": g.kind, "decision_id": decision_id, "decision": d["decision"],
                "comment": d["comment"][:8000], "principal_id": d["principal_id"], "cycle": g.cycle,
                "repo": self.s.targets[0]["repo"] if self.s.targets else None},
                activity_id=ids.logical_activity_id(g.node_key, g.cycle, g.ordinal, f"memory_fb_{g.kind[:8]}"),
                **_opts("memory.ingest_gate_feedback")))
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
