"""Workflow-local job state (Temporal-owned, C-STATE section 1.1) and the Continue-As-New carry.

Everything here lives in workflow memory and is rebuilt by replay from history. Large bodies are
never stored: only S3 URIs (<= 1100 chars), digests, IDs, counters, and small structured values.
`JobState.carry()` is the Continue-As-New payload, budgeted at 256 KiB (C-STATE section 3.1).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

CARRY_SCHEMA = "replicant.wf-carry/v1"
CARRY_BUDGET_BYTES = 256 * 1024
DEDUPE_WINDOW = timedelta(days=7)  # C-IDS section 3 item 5
MAX_RECENT_RUNS = 30
MAX_CLOSED_GATES = 10

TERMINAL_STATUS_FOR_OUTCOME = {
    "pr_merged": "succeeded", "docs_published": "succeeded", "no_change_needed": "succeeded",
    "qa_completed": "succeeded", "batch_completed": "succeeded", "repair_pushed": "succeeded",
    "dry_run_completed": "succeeded", "ready_to_publish": "succeeded",
    "rejected_at_gate": "rejected", "pr_closed_unmerged": "rejected", "clarification_rejected": "rejected",
    "gate_expired": "expired", "pr_wait_expired": "expired", "pause_expired": "expired",
    "cancelled_by_user": "cancelled", "bundle_revoked_not_migrated": "cancelled",
    "requester_deprovisioned": "cancelled",
    "taken_over": "taken_over",
    "repair_budget_exhausted": "failed", "node_failed": "failed", "infrastructure_failure": "failed",
    "loop_limit_exceeded": "failed", "policy_violation": "failed", "qa_failed": "failed",
    "security_violation": "failed", "budget_exhausted": "failed", "tenant_quota_exhausted": "failed",
    "eval_stopped": "cancelled", "graph_digest_mismatch": "failed",
    # forced-migration deadlines (C-MSG 1.5.0 release.notice; R1-004, R2-044)
    "worker_retired_not_migrated": "cancelled", "model_retired_not_migrated": "cancelled",
    # hard job lifetime (repair_limits.job_max_age_days; R2-044)
    "job_max_age_exceeded": "expired",
    # fix round wave 2
    "qa_inconclusive": "failed",  # qa_only (XD-FIX-R1-131)
    "harness_completed": "succeeded",
    "harness_error": "failed",  # eval cassette miss (XD-W2-06-02)  # eval harness job (XD-FIX-R1-134)
    "capacity_unavailable": "failed",  # capacity wait beyond the node's 6 h limit (XD-FIX-R2-020)
    "budget_exceeded": "failed",  # compute dollars per job (XD-FIX-R2-064)
    # dependency outage that outlived repair_limits.dependency_block.max_hours (R2-010)
    "dependency_unavailable": "failed",
}


@dataclass
class RunRecord:
    run_id: str
    node_key: str
    cycle: int
    ordinal: int
    role: str
    phase: str = "Pending"
    reason: str | None = None
    isolation: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    artifact_manifest_uri: str | None = None
    result_uri: str | None = None
    steering_enabled: bool = False
    active: bool = True
    finalize_seconds: int = 300  # plan timeouts.finalize_seconds (takeover waits for finalize, R1-008)


@dataclass
class GateRecord:
    gate_id: str
    kind: str  # C-MSG gate_kind
    node_key: str
    cycle: int
    ordinal: int
    state: str  # open | approved | rejected | changes_requested | answered | expired | cancelled
    opened_at: str
    presented_digest: str
    packet_uri: str
    channels: list[str]
    eligible: dict[str, Any]
    expires_at: str | None = None
    next_reminder_at: str | None = None
    escalation_level: int = 0
    reminders_sent: int = 0
    held: bool = False  # pause_mode=at_next_gate: open, approvers not notified
    held_since: str | None = None  # expiry clock suspended while held (R2-023)
    grace_until: str | None = None  # late-fired expiry: decisions stamped before expires_at still count (R2-014)
    decision: dict[str, Any] | None = None
    schedule: dict[str, Any] = field(default_factory=dict)  # GateRouting.timing (seconds)
    subject: dict[str, Any] = field(default_factory=dict)  # digests the decider approved, e.g. diff_sha256 (G-03)
    routing: dict[str, Any] = field(default_factory=dict)  # GateRouting minus timing (rule_id, reapprove_after_rebase)
    release_block: dict[str, Any] | None = None  # C-MSG 1.6.0 GateView.release_block (CCR-06-13, V12)
    decision_log: list[dict[str, Any]] = field(default_factory=list)  # C-MSG 2.0.0 GateView.decision_log (CCR-07-07)
    reopened_from: str | None = None  # gate_id whose contested approval this instance replaces (XD-FIX-R1-173)


@dataclass
class JobState:
    job_id: str
    tenant_id: str
    release_bundle_id: str
    worker_build_id: str
    requester: str
    priority: str
    title: str
    targets: list[dict[str, Any]]
    request: dict[str, Any] | None  # full JobRequest until the task packet exists, then None
    request_summary: dict[str, Any]
    workflow_type: str = "unrouted"
    status: str = "queued"
    phase: str = "intake"
    parent_job_id: str | None = None
    cycle: int = 0
    ordinals: dict[str, int] = field(default_factory=dict)  # "<node_key>@c<cycle>" -> ordinal
    visits: dict[str, int] = field(default_factory=dict)  # node visits in the current cycle
    gate_ordinals: dict[str, int] = field(default_factory=dict)  # "<kind>@c<cycle>" -> ordinal
    child_counters: dict[str, int] = field(default_factory=dict)
    projection_seq: int = 0
    steer_seq: int = 0
    takeover_ordinal: int = 0
    risk: dict[str, Any] = field(default_factory=lambda: {"level": "low", "rules_fired": [], "raised_by_llm": False})
    refs: dict[str, str] = field(default_factory=dict)  # plan_uri, briefing_uri, latest_diff_bundle_uri, ...
    memory_snapshot_id: str | None = None
    exports: dict[str, dict[str, Any]] = field(default_factory=dict)  # guard context `nodes`
    gates: dict[str, GateRecord] = field(default_factory=dict)
    runs: dict[str, RunRecord] = field(default_factory=dict)
    applied_ids: dict[str, float] = field(default_factory=dict)  # Update IDs -> epoch seconds
    applied_results: dict[str, dict[str, Any]] = field(default_factory=dict)  # decision_id -> result
    deliveries: dict[str, float] = field(default_factory=dict)  # pr.event delivery IDs
    notices: dict[str, float] = field(default_factory=dict)
    pending_notices: list[str] = field(default_factory=list)
    pr: dict[str, Any] | None = None
    pr_buffer: list[dict[str, Any]] = field(default_factory=list)
    pr_last_activity_at: str | None = None
    pr_reminders_sent: int = 0  # since the last PR activity (repair_limits.pr_wait.reminder_days)
    pr_reminders_total: int = 0  # logical-ID counter for PR reminders
    steering_queue: list[dict[str, Any]] = field(default_factory=list)  # inline text, <= 8 entries
    steering_refs: list[str] = field(default_factory=list)  # staged backlog URIs
    steering_log: list[dict[str, Any]] = field(default_factory=list)  # {message_id, outcome, at}
    repair_used: int = 0
    repair_max: int = 5
    paused: bool = False
    pause_mode: str | None = None
    terminal: dict[str, Any] | None = None
    graph_spec_digest: str | None = None
    resume_at: str | None = None
    boundary_crossed: str | None = None
    migration: dict[str, Any] | None = None  # {target_release_bundle_id, target_build_id, state}
    revoked_deadline: str | None = None
    segment: int = 0  # number of Continue-As-New transitions so far (diagnostics)
    # Node re-entry support: nodes in progress (key -> {ordinal, started_at}) and the memo of
    # completed side-effecting steps of in-progress nodes (logical activity ID -> small result).
    active_nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    memo: dict[str, Any] = field(default_factory=dict)
    # Evaluation mode (CCR-06-01): {mode, gate_script_uri, memory_snapshot_id, memory_scope_context,
    # shadow_of_job_id, publish_to_eval_git, eval_run_id, review_rounds_key}
    evaluation: dict[str, Any] | None = None
    acting_for: str = ""  # requester, or on_behalf_of for service-triggered jobs (CCR-09-03)
    repo_ids: list[str] = field(default_factory=list)  # authorized targets (run grant repo_ids)
    # QA routing (WS-05 XD-05-03): per-cycle counters and the risk floor QA imposes (D-19: only raises)
    qa_fix_used: int = 0
    qa_env_retries: int = 0
    risk_floor: str = "low"
    qa_gate_with_failure: bool = False
    budget_extensions: int = 0
    verified_diff_sha256: str | None = None  # digest the latest verifier run checked (G-03 fallback)
    # WS-04 WorkingMemory (opaque to WS-01, <= 64 KiB, carried across Continue-As-New)
    working_memory: dict[str, Any] = field(default_factory=dict)
    # ---- fix round (Phase 3)
    created_at: str | None = None  # job start (workflow time); hard lifetime repair_limits.job_max_age_days (R2-044)
    paused_by: str | None = None  # principal who paused (gate.expiring notice goes to them, R2-023)
    hold: dict[str, Any] | None = None  # replicant-imposed hold: {kind: dependency|kill_switch|revoked, since, detail}
    deadlines: dict[str, str] = field(default_factory=dict)  # notice kind -> ISO deadline (R1-004, R2-044)
    kill_switches: dict[str, dict[str, Any]] = field(default_factory=dict)  # "<scope>:<value>" -> {switches, issued_at}
    approver_feedback: list[dict[str, Any]] = field(default_factory=list)  # request_changes comments to stage (R2-019)
    clarification_answer: dict[str, Any] | None = None  # {decision_id, text}: untrusted, cleared after routing (R1-017)
    notice_seq: int = 0  # logical-ID counter for notice-driven activities (bundle_patched, model_retiring)
    reroute_seq: int = 0  # logical-ID counter for gate re-routing on principal changes (R1-020)
    notice_queue: list[dict[str, Any]] = field(default_factory=list)  # notices awaiting activities (main flow)
    forced_outcome: str | None = None
    # ---- fix round wave 2
    waiting: dict[str, Any] | None = None  # C-MSG 2.0.0 JobSnapshot.waiting (CCR-07-10 + CCR-01-06)
    release_blocked: dict[str, Any] | None = None  # bundle rolled back or revoked: block for new and open gates
    qa_reruns: int = 0  # rerun_qa decisions used in this cycle (CCR-05-04, max 2)
    qa_rerun_pending: bool = False  # next qa node runs with trigger env_retry
    last_qa_verdict: str | None = None
    contested: list[dict[str, Any]] = field(default_factory=list)  # {gate_id, kind, decision_id}: re-approval needed
    publish_gate_ids: list[str] = field(default_factory=list)  # gates whose approval a publish activity consumed
    retry_nodes: dict[str, str] = field(default_factory=dict)  # node_key -> operation_id (ops.retry_node)
    reused_plan_uri: str | None = None  # XD-FIX-R2-050 restart from an approved plan
    progress_workflow_id: str | None = None  # batch or MaintenanceShard that receives batch.child_progress
  # outcome when a declined or missed forced migration ends the job

    # ------------------------------------------------------------------ counters
    def next_ordinal(self, node_key: str) -> int:
        k = f"{node_key}@c{self.cycle}"
        self.ordinals[k] = self.ordinals.get(k, 0) + 1
        return self.ordinals[k]

    def next_gate_ordinal(self, kind: str) -> int:
        k = f"{kind}@c{self.cycle}"
        self.gate_ordinals[k] = self.gate_ordinals.get(k, 0) + 1
        return self.gate_ordinals[k]

    def next_child(self, kind: str) -> int:
        self.child_counters[kind] = self.child_counters.get(kind, 0) + 1
        return self.child_counters[kind]

    # ------------------------------------------------------------------ guard context
    def guard_context(self, params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        return {
            "job": {"workflow_type": self.workflow_type, "priority": self.priority,
                    "tenant_id": self.tenant_id, "repos": [t["repo"] for t in self.targets]},
            "request": self.request_summary,
            "risk": self.risk,
            "nodes": self.exports,
            "gates": {g.node_key: (g.decision or {}) | {"state": g.state} for g in self.gates.values()},
            "cycle": self.cycle,
            "params": params,
            "policy": policy,
            "pr": self.pr or {},
            "repair": {"used": self.repair_used, "max": self.repair_max},
        }

    # ------------------------------------------------------------------ dedupe windows
    def seen(self, ident: str) -> bool:
        return ident in self.applied_ids

    def remember(self, ident: str, now: datetime) -> None:
        self.applied_ids[ident] = now.timestamp()

    def prune(self, now: datetime) -> None:
        cutoff = (now - DEDUPE_WINDOW).timestamp()
        for d in (self.applied_ids, self.deliveries, self.notices):
            for k in [k for k, t in d.items() if t < cutoff]:
                del d[k]
        for k in [k for k in self.applied_results if k not in self.applied_ids]:
            del self.applied_results[k]
        inactive = [r for r in self.runs.values() if not r.active]
        for r in inactive[:-MAX_RECENT_RUNS]:
            del self.runs[r.run_id]
        closed = [g for g in self.gates.values() if g.state != "open"]
        for g in closed[:-MAX_CLOSED_GATES]:
            del self.gates[g.gate_id]

    # ------------------------------------------------------------------ carry
    def carry(self) -> dict[str, Any]:
        d = asdict(self)
        d["schema"] = CARRY_SCHEMA
        size = len(json.dumps(d, separators=(",", ":")).encode())
        if size > CARRY_BUDGET_BYTES:
            raise CarryTooLarge(size)
        return d

    @staticmethod
    def from_carry(d: dict[str, Any]) -> "JobState":
        d = dict(d)
        schema = d.pop("schema", CARRY_SCHEMA)
        if schema != CARRY_SCHEMA:
            d = upcast_carry(schema, d)
        d["gates"] = {k: GateRecord(**v) for k, v in d.get("gates", {}).items()}
        d["runs"] = {k: RunRecord(**v) for k, v in d.get("runs", {}).items()}
        return JobState(**d)


class CarryTooLarge(RuntimeError):
    def __init__(self, size: int) -> None:
        super().__init__(f"Continue-As-New carry is {size} bytes, budget {CARRY_BUDGET_BYTES}")
        self.size = size


def upcast_carry(schema: str, d: dict[str, Any]) -> dict[str, Any]:
    """Upcasters from older carry schemas, one function per version step. A build declares the
    carry schemas it can read in its NodeCatalog (`carrySchemas`), which migration eligibility
    checks (versioning.md section 6). v1 is the first version, so there is nothing to upcast."""
    raise ValueError(f"unsupported carry schema {schema!r}")
