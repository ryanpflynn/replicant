"""ReplicantJob: constants, private types and pure helpers shared by the ReplicantJobBase mixins."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio.common import SearchAttributeKey
from temporalio.exceptions import ActivityError, ApplicationError

from replicant_worker.activities.options import CATALOG
from replicant_worker.workflows.state import GateRecord, JobState

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
class _Can:
    kind: str
    resume_at: str | None


class _PrologueInterrupt(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("prologue interrupt")
        self.payload = payload


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
            "received_at": jr.get("received_at"),  # build_run_plan input (XD-W2-11-01)
            # design §8c: the repo profile pinned at intake (onboarding-interfaces.md §4); absent on older jobs
            **({"repo_profile": dict(jr["repo_profile"])} if isinstance(jr.get("repo_profile"), dict) else {})}


READINESS_LABEL = "replicant-readiness"  # local_bundle.READINESS_LABEL (design §8c readiness probe)


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
