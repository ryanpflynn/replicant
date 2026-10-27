"""Core node types (graph roles from shared context section 4). Shipped in every replicant-worker build.

Each class is registered in the build's NodeCatalog. Config schemas here are JSON Schema and are
validated at registry publish time (graph compile against the catalog) and in graph.compose.
Activities and their logical-ID effects follow graphs/README.md section 2.
"""

from __future__ import annotations

from typing import Any

from temporalio import workflow

from replicant_worker import ids
from replicant_worker.workflows.context import NodeContext
from replicant_worker.graphs.dsl import RISK_ORDER
from replicant_worker.registry import NodeFailed, NodeResult, NodeType, node_type
from replicant_worker.nodes.qa_routing import QaRoutingInput, route_after_qa

AGENT_CONFIG = {
    "type": "object", "additionalProperties": False, "required": ["agent"],
    "properties": {
        "agent": {"type": "string", "pattern": "^[a-z][a-z0-9]*(-[a-z0-9]+)*$", "x-replicant-ref": "agent"},
        "environment": {"type": "string", "x-replicant-ref": "environment",
                        "description": "Default: from the repo's codebase binding"},
        "workloadClass": {"enum": ["coding", "qa_build", "review", "utility"]},
        "resourceProfile": {"type": "string", "default": "default"},
        "networkProfile": {"type": "string", "default": "coding-default"},
        "timeouts": {"type": "object", "properties": {
            "startup_seconds": {"type": "integer", "minimum": 60, "maximum": 1800},
            "active_deadline_seconds": {"type": "integer", "minimum": 60, "maximum": 28800},
            "finalize_seconds": {"type": "integer", "minimum": 30, "maximum": 1800}}},
        "steering": {"type": "boolean", "default": False},
        "mode": {"type": "string", "description": "role-specific mode, e.g. rebase"},
    },
}
RUN_EFFECTS = ("plan", "alloc", "resnap", "stage", "stage_agent", "run", "cost", "cancel", "cancel_op", "cancel_retry",
               "route_budget_e", "packet_budget_e_r1", "notify_open_budget_e", "extend")


def _max_risk(*levels: str) -> str:
    return max(levels, key=lambda lv: RISK_ORDER.get(lv, 0))


class _AgentRun(NodeType):
    run_role: str = "implementer"
    default_workload = "coding"
    output_refs: dict[str, str] = {}  # run_result output name -> JobState.refs name
    effects = RUN_EFFECTS
    config_schema = AGENT_CONFIG

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        out = await self._run_agent(ctx, config)
        for out_name, ref_name in self.output_refs.items():
            ctx.set_ref(ref_name, out.outputs.get(out_name))
        return self.result(out)

    async def _run_agent(self, ctx: NodeContext, config: dict[str, Any], extra_inputs: dict[str, Any] | None = None,
                         **kw: Any) -> Any:
        env = config.get("environment") or ctx.host.binding_environment()
        inputs = {**({"mode": config["mode"]} if "mode" in config else {}), **(extra_inputs or {})}
        return await ctx.run_agent(
            role=self.run_role, agent=config["agent"], environment=env,
            workload_class=config.get("workloadClass", self.default_workload),
            resource_profile=config.get("resourceProfile", "default"),
            timeouts=config.get("timeouts"), network_profile=config.get("networkProfile", "coding-default"),
            steering=bool(config.get("steering", False)),
            inputs=inputs or None, **kw)

    def result(self, out: Any) -> NodeResult:
        ex = {k: v for k, v in (out.role_exports or {}).items() if k in ("files_changed", "conflicts", "paths")}
        return NodeResult(outcome=out.outcome or "failed", exports={"run_id": out.run_id, **ex})


@node_type
class PlanRun(_AgentRun):
    node_type = "plan_run"
    role = "planner"
    run_role = "planner"
    default_workload = "review"
    output_refs = {"plan": "plan_uri", "handoff": "latest_handoff_uri"}
    description = "Read-only planning run; produces a plan artifact (acceptance criteria per XD-05-05)."


@node_type
class ImplementRun(_AgentRun):
    node_type = "implement_run"
    role = "implementer"
    effect_class = "workspace_write"
    output_refs = {"diff_bundle": "latest_diff_bundle_uri", "handoff": "latest_handoff_uri",
                   "session_state": "latest_session_state_uri", "memory_candidates": "memory_candidates_uri"}
    description = "Implementer run (native CLI, D-10). Produces a diff bundle."


@node_type
class RepairRun(_AgentRun):
    node_type = "repair_run"
    role = "implementer"  # diff-producing for invariant I-3
    run_role = "repair"
    effect_class = "workspace_write"
    output_refs = ImplementRun.output_refs
    description = "Repair or rebase run on the job's branch (D-15). Session restore or handoff (WS-04)."


@node_type
class QaRun(_AgentRun):
    node_type = "qa_run"
    role = "qa"
    run_role = "qa"
    default_workload = "qa_build"
    effect_class = "check_only"
    effects = RUN_EFFECTS + ("stage_stubs", "classify", "alert_env", "index_results")
    output_refs = {"qa_report": "latest_qa_report_uri"}
    description = "QA/QE stage in a fresh sandbox (D-16); routing by WS-05 route_after_qa (XD-05-03)."

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        s, h = ctx.s, ctx.host
        step_id = f"{ctx.node_key}.c{s.cycle}.n{ctx.ordinal}"
        pre_run_id = ids.run_id_for(s.tenant_id, ids.idempotency_key(workflow.info().workflow_id, ctx.lid("run")))
        # WS-05 qa-activities-io.schema.json StageContractStubsInput (R1V-012)
        await ctx.act("qa.stage_contract_stubs", {"job_id": s.job_id, "tenant_id": s.tenant_id, "step_id": step_id,
                                                  "run_id": pre_run_id,
                                                  "environment_definition_digest": ctx.host.environment_digest(),
                                                  "targets": [t["repo"] for t in s.targets][:5],
                                                  **({"briefing_uri": s.refs["briefing_uri"]} if s.refs.get("briefing_uri") else {})},
                      effect="stage_stubs")
        trigger = "env_retry" if s.qa_rerun_pending else None  # gate decision rerun_qa (CCR-05-04)
        s.qa_rerun_pending = False
        try:
            out = await self._run_agent(ctx, config, extra_inputs={"contract_compat": "report_only",
                                                                    **({"trigger": trigger} if trigger else {})},
                                        raise_on_failure=False)
            run_phase = out.phase
        except NodeFailed as f:
            if f.reason != "admission_denied":  # XD-FIX-R1-122: 422 at createRun is classified, not fatal
                raise
            from replicant_worker.workflows.context import RunOutcome
            out = RunOutcome(run_id="", phase="NotCreated", outcome=None, outputs={}, reason="AdmissionDenied")
            run_phase = "NotCreated"
        ctx.set_ref("latest_qa_report_uri", out.outputs.get("qa_report"))
        prev = s.exports.get(ctx.node_key, {})
        cls = await ctx.act("qa.classify", {k: v for k, v in {  # WS-05 ClassifyInput (R1V-012)
            "job_id": s.job_id, "tenant_id": s.tenant_id, "step_id": step_id, "run_id": out.run_id or pre_run_id,
            "workflow_type": s.workflow_type, "run_phase": run_phase,
            "failure_reason": out.reason, "result_uri": out.result_uri, "qa_report_uri": out.outputs.get("qa_report"),
            "fix_iteration": s.qa_fix_used, "previous_blocking": list(prev.get("blocking_test_ids", []))[:40]}.items()
            if v is not None}, effect="classify")
        s.last_qa_verdict = cls["verdict"]
        q = h.policy("repair_limits")["qa"]
        wt = s.workflow_type
        decision, floor = route_after_qa(QaRoutingInput(
            verdict=cls["verdict"], reason_code=cls.get("reason_code", "unknown_failure"),
            run_failure_reason=out.reason, run_failure_retryable=out.retryable,
            fix_iterations_used=s.qa_fix_used,
            fix_iterations_max=int(q["fix_iterations"].get("per_workflow_type", {}).get(wt, q["fix_iterations"]["default"])),
            env_retries_used=s.qa_env_retries,
            on_fail_after_fixes=q.get("on_fail_after_fixes_per_workflow_type", {}).get(wt, q["on_fail_after_fixes"]),
            workflow_type=wt, oscillation=bool(cls.get("oscillation")),
            env_signature_transient=bool(cls.get("env_signature_transient"))))
        if decision == "fix_loop":
            s.qa_fix_used += 1
        elif decision == "retry_qa":
            s.qa_env_retries += 1
        elif decision == "gate_with_failure":
            s.qa_gate_with_failure = True  # OQ-05-01 option: approval publishes a draft PR only
        env_cause = out.reason in ("EnvironmentSetupFailed", "AdmissionDenied") or cls.get("reason_code") == "environment_failure"
        if decision == "operator_and_gate" or (decision == "fail_job" and wt == "qa_only" and env_cause):
            sig = cls.get("env_failure") or {}  # WS-05 ClassifyOutput.env_failure object (R1V-013)
            # XD-W2-05-05 / WS-05 failure-feedback.md §5.2: the operator alert, payload exactly as specified
            await ctx.act("notify.operator", {
                "dedupe_key": f"qa-env:{out.run_id or pre_run_id}", "event_key": "qa.environment_failure",
                "tenant_id": s.tenant_id, "job_id": s.job_id,
                "recipients": {"roles": ["operators", "environment_owners"]},
                "vars": {"run_id": out.run_id or pre_run_id, "stage": sig.get("stage"), "check_id": sig.get("check_id"),
                         "evidence_key": sig.get("evidence_key"), "verdict": cls["verdict"]},
                "operator": {k: v for k, v in {"definition_ref": sig.get("definition_ref"), "signature": sig.get("signature"),
                                               "proposed_patch_uri": sig.get("proposed_patch_uri")}.items() if v is not None}},
                effect="alert_env")
        s.risk_floor = _max_risk(s.risk_floor, floor)  # D-19: QA never lowers risk
        if cls.get("classified_report_uri") or out.outputs.get("qa_report"):  # XD-FIX-R1-118, fire-and-forget
            h.start_side_effect("qa.index_results", {"job_id": s.job_id, "tenant_id": s.tenant_id, "run_id": out.run_id or pre_run_id,
                                "classified_report_uri": cls.get("classified_report_uri") or out.outputs.get("qa_report"),
                                "decision": decision}, activity_id=ctx.lid("index_results"))
        passed = cls["verdict"] in ("pass", "pass_with_warnings", "not_applicable")
        return NodeResult(outcome=decision, exports={"passed": passed, "decision": decision, "risk_floor": floor,
                                                     "verdict": cls["verdict"], "run_id": out.run_id,
                                                     "blocking": int(cls.get("blocking", 0))})


@node_type
class ReviewRun(_AgentRun):
    node_type = "review_run"
    role = "reviewer"
    run_role = "reviewer"
    default_workload = "review"
    effect_class = "check_only"
    output_refs = {"review_report": "review_report_uri"}
    description = "Reviewer run (for example Codex, read-only intent)."

    def result(self, out: Any) -> NodeResult:
        ok = out.outcome == "completed"
        return NodeResult(outcome="approve" if ok else "changes", exports={"verdict": "approve" if ok else "changes"})


@node_type
class SkepticRun(_AgentRun):
    node_type = "skeptic_run"
    role = "skeptic"
    run_role = "reviewer"
    default_workload = "review"
    effect_class = "check_only"
    output_refs = {"review_report": "skeptic_report_uri"}
    description = "Skeptic/challenger run against the plan: argues the opposite, lists risks."

    def result(self, out: Any) -> NodeResult:
        blocking = out.outcome in ("blocked", "failed")
        return NodeResult(outcome="blocking" if blocking else "ok", exports={"blocking": blocking})


@node_type
class ContextAssembler(NodeType):
    node_type = "context_assembler"
    role = "context_assembler"
    protected = True
    effects = ("snapshot", "briefing")
    config_schema = {"type": "object", "additionalProperties": False, "properties": {
        "briefing": {"enum": ["standard", "consumer_impact", "minimal"], "default": "standard"}}}
    description = "Memory snapshot (pinned per job, D-14) and briefing/task packet (WS-04)."

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        s = ctx.s
        ev = s.evaluation or {}
        if s.memory_snapshot_id is None:
            snap = await ctx.act("memory.create_snapshot", {k: v for k, v in {  # WS-04 CreateSnapshotRequest
                "job_id": s.job_id, "tenant_id": s.tenant_id, "release_bundle_id": s.release_bundle_id,
                "evaluation_mode": ev.get("mode"),
                "repos": [t["repo"] for t in s.targets], "workflow_type": s.workflow_type,
                "mode": "eval_pinned" if ev.get("memory_snapshot_id") else "create",
                "use_snapshot_id": ev.get("memory_snapshot_id"), "scope_context": ev.get("memory_scope_context"),
                "replay_job_id": ev.get("memory_replay_job_id"),  # XD-FIX-R1-101
                "requester_principal_id": s.acting_for or s.requester,  # XD-FIX-R1-100
                "policy": ctx.host.memory_policy_ref(),
            }.items() if v is not None}, effect="snapshot")
            s.memory_snapshot_id = snap["memory_snapshot_id"]
        res = await ctx.act("context.build_briefing", {
            "job_id": s.job_id, "tenant_id": s.tenant_id, "release_bundle_id": s.release_bundle_id,
            "memory_snapshot_id": s.memory_snapshot_id, "request": s.request, "refs": dict(s.refs),
            "briefing": config.get("briefing", "standard"), "cycle": s.cycle, "working_memory": s.working_memory,
        }, effect="briefing")
        ctx.set_ref("briefing_uri", res.get("briefing_uri"))
        ctx.set_ref("task_packet_uri", res.get("task_packet_uri"))
        if res.get("working_memory") is not None:
            s.working_memory = res["working_memory"]
        s.request = None  # the task packet now carries the request; keep the carry small
        return NodeResult(outcome="completed")


@node_type
class RiskClassify(NodeType):
    node_type = "risk_classify"
    role = "verifier"
    protected = True
    effect_class = "check_only"
    config_schema = {"type": "object", "additionalProperties": False, "required": ["stage"],
                     "properties": {"stage": {"enum": ["plan", "diff"]}, "llmReview": {"type": "boolean", "default": True}}}
    description = "Deterministic risk rules (D-19); an LLM review and the QA risk floor may only raise the level."

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        s = ctx.s
        det = await ctx.act("risk.classify", {"job_id": s.job_id, "tenant_id": s.tenant_id,
                                              "stage": config["stage"], "refs": dict(s.refs),
                                              "targets": s.targets, "workflow_type": s.workflow_type})
        level, rules, raised = det["level"], list(det["rules_fired"]), False
        if config.get("llmReview", True) and ctx.host.policy("router_rules").get("llm_risk_review", True):
            llm = await ctx.act("risk.llm_review", {"job_id": s.job_id, "stage": config["stage"],
                                                    "refs": dict(s.refs), "deterministic_level": level})
            if RISK_ORDER[llm["level"]] > RISK_ORDER[level]:  # may raise, never lower
                level, raised = llm["level"], True
                rules.append("llm_raised")
        if config["stage"] == "diff" and RISK_ORDER[s.risk_floor] > RISK_ORDER[level]:
            level = s.risk_floor  # WS-05: QA inconclusive or failing raises the floor
            rules.append(f"qa_floor_{s.risk_floor}")
        level = _max_risk(level, s.risk["level"])  # risk never decreases within a job
        s.risk = {"level": level, "rules_fired": sorted(set(rules))[:100],
                  "raised_by_llm": raised or s.risk.get("raised_by_llm", False)}
        ctx.host.upsert_risk(level)
        return NodeResult(outcome=level, exports={"level": level})


@node_type
class HumanGate(NodeType):
    node_type = "human_gate"
    role = "human_gate"
    protected = True
    interrupts = True
    effect_class = "check_only"
    effects = ("route_plan_app", "route_publish_", "route_clarific", "packet_plan_app_r1", "packet_publish__r1",
               "notify_open_plan_app", "notify_open_publish_", "notify_unhold", "remind_1", "escalate_1",
               "notify_expired", "record_plan_app", "notify_res_plan_app")
    config_schema = {"type": "object", "additionalProperties": False, "required": ["gateKind"],
                     "properties": {"gateKind": {"enum": ["plan_approval", "publish_approval", "clarification"]}}}
    description = "Policy-driven human gate: graph interrupt backed by Update gate.decide (D-19)."

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        kind = config["gateKind"]
        level = ctx.s.risk["level"]
        routing = await ctx.routing(kind)
        node_when = ctx.host.node_when_true(ctx.node_key)  # extra conditions only ever add gates
        base = {"risk_level": level, "reapprove": routing.get("reapprove_after_rebase", "on_conflict_or_risk_change"),
                "rule_id": routing.get("rule_id", "none")}
        if not (routing["required"] or node_when):
            return NodeResult(outcome="not_required", exports={"decision": "approve", "required": False, **base})
        if kind == "publish_approval" and ctx.host.reapproval_waived(ctx.node_key):
            return NodeResult(outcome="approve", exports={"decision": "approve", "required": True, **base,
                                                          "waived": "clean_rebase_same_risk"})
        d = await ctx.gate(kind, packet={"node_key": ctx.node_key}, routing=routing)
        outcome = d.get("decision") or d["state"]  # approve | reject | request_changes | answer | expired | cancelled
        return NodeResult(outcome=outcome, exports={"decision": outcome, "required": True, **base,
                                                    "decision_id": d.get("decision_id")})


@node_type
class Verifier(NodeType):
    node_type = "verifier"
    role = "verifier"
    protected = True
    effect_class = "check_only"
    config_schema = {"type": "object", "additionalProperties": False, "properties": {
        "checks": {"type": "array", "items": {"enum": ["diff_scope", "secret_scan", "policy", "licenses"]},
                   "default": ["diff_scope", "secret_scan", "policy"]}}}
    description = "Mechanical verification of the diff: scope, secret scan (blocks publish), policy."

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        r = await ctx.act("verifier.check", {"job_id": ctx.s.job_id, "tenant_id": ctx.s.tenant_id,
                                             "refs": dict(ctx.s.refs), "checks": config.get("checks"),
                                             "paths_scope": ctx.s.request_summary.get("paths")})
        ctx.set_ref("verification_report_uri", r.get("report_uri"))
        ctx.set_ref("secret_scan_report_uri", r.get("secret_scan_report_uri"))  # CCR-09-05 artifact type
        ctx.s.verified_diff_sha256 = r.get("diff_sha256")
        return NodeResult(outcome="passed" if r["passed"] else "failed",
                          exports={"passed": r["passed"], "findings": r.get("findings", 0),
                                   "paths": r.get("paths", [])[:200], "diff_sha256": r.get("diff_sha256")})


@node_type
class CheckStep(NodeType):
    node_type = "check_step"
    role = "auxiliary"
    effect_class = "check_only"
    effects = ("step",)
    config_schema = {"type": "object", "additionalProperties": False, "required": ["step"],
                     "properties": {"step": {"enum": ["contract_compat", "consumer_impact", "docs_scope"]},
                                    "params": {"type": "object", "properties": {
                                        "mode": {"enum": ["warn_on_breaking", "block_on_breaking"]},
                                        "source": {"enum": ["qa_report"], "description": "contract_compat: read QA "
                                                   "stage K1's result instead of running the check again (R1-120)"}}}}}
    description = "Built-in auxiliary checks implemented by existing activities."

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        step = config["step"]
        # The activity writes its report as artifact type `node_report` (C-ART 1.8.0, CCR-01-03) with the
        # mandatory attributes.report_kind = <step> (R1-027); report_uri points at it.
        r = await ctx.act("checks.run_step", {"job_id": ctx.s.job_id, "tenant_id": ctx.s.tenant_id, "step": step,
                                              "params": config.get("params", {}), "refs": dict(ctx.s.refs),
                                              "targets": ctx.s.targets,
                                              "report": {"artifact_type": "node_report", "report_kind": step}},
                          effect="step")
        ctx.set_ref(f"{step}_report_uri", r.get("report_uri"))
        return NodeResult(outcome=r["outcome"], exports={k: v for k, v in r.items() if k in ("passed", "breaking", "consumers", "escalate")})


@node_type
class BaseCheck(NodeType):
    node_type = "base_check"
    role = "auxiliary"
    protected = True
    effect_class = "check_only"
    description = "Has the base branch moved since the diff was produced? (D-19 rebase-and-reverify)"

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        r = await ctx.act("publisher.check_base", {"job_id": ctx.s.job_id, "repo": ctx.host.primary_repo(),
                                                   "diff_bundle_uri": ctx.s.refs.get("latest_diff_bundle_uri")})
        if r["moved"]:
            ctx.host.set_base_sha(r["new_base_sha"])
        return NodeResult(outcome="moved" if r["moved"] else "current",
                          exports={"moved": r["moved"], "clean": r.get("applies_cleanly", True)})


@node_type
class Publisher(NodeType):
    node_type = "publisher"
    role = "publisher"
    protected = True
    effects = ("publish", "push", "notify_pr")
    config_schema = {"type": "object", "additionalProperties": False, "properties": {
        "mode": {"enum": ["open_pr", "push_update"], "default": "open_pr"}, "draft": {"type": "boolean"}}}
    description = "Publisher (outside the sandbox, D-21): pushes the APPROVED diff bundle (G-03); opens/updates the PR."

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        s, h = ctx.s, ctx.host
        ev = s.evaluation or {}
        if ev and not (ev.get("mode") == "eval" and ev.get("publish_to_eval_git")):
            return NodeResult(outcome="ready_to_publish")  # evaluation mode never publishes (CCR-06-01)
        if s.request_summary.get("dry_run"):
            return NodeResult(outcome="dry_run")
        while s.contested:  # XD-FIX-R1-173: withdrawn approvals are re-approved before anything is published
            c = s.contested[0]
            d = await ctx.reopen_gate(s.gates[c["gate_id"]])
            if d.get("state") != "approved":
                raise NodeFailed("gate_expired" if d.get("state") == "expired" else "rejected_at_gate",
                                 f"re-approval of {c['kind']} not given")
            s.contested.pop(0)
        if h.kill_engaged("publishing"):  # C-MSG 1.5.0: "Publishing paused by operators"
            await h.hold_while(lambda: not h.kill_engaged("publishing"), kind="kill_switch", node_key=ctx.node_key,
                               detail="Publishing paused by operators")
        expected, decision_id = h.approved_diff()
        s.publish_gate_ids = (s.publish_gate_ids + [g.gate_id for g in s.gates.values() if g.cycle == s.cycle
                              and g.state == "approved" and g.gate_id not in s.publish_gate_ids])[-20:]
        mode = config.get("mode", "open_pr")
        args = {"job_id": s.job_id, "tenant_id": s.tenant_id, "repo": h.primary_repo(),
                "diff_bundle_uri": s.refs.get("latest_diff_bundle_uri"), "pr": s.pr,
                "approved_diff_sha256": expected, "approval_decision_id": decision_id,  # Publisher refuses mismatch
                "draft": bool(config.get("draft", s.request_summary.get("draft_pr", False)) or s.qa_gate_with_failure),
                "evaluation_target": "eval_git" if ev else None,
                "plan_uri": s.refs.get("plan_uri"), "qa_report_uri": s.refs.get("latest_qa_report_uri")}
        if mode == "open_pr":
            pr = await ctx.act("publisher.publish", args, effect="publish")
            s.pr = pr
            h.emit("pr.opened", f"PR #{pr['number']} opened", node_key=ctx.node_key)
            # notifications.md §2 `job.pr_opened` (requester; originating channel, UI, Slack DM). Fire-and-forget:
            # a notification failure never holds the publish (C-MSG notify.* semantics). Patched for replay safety.
            if workflow.patched("notify-pr-opened-v1") and h.notifications_enabled():
                try:
                    await ctx.act("notify.job_event", {"job_id": s.job_id, "kind": "pr_opened", "pr": pr,
                                                       "title": s.request_summary.get("title")},
                                  effect="notify_pr", block=False)
                except NodeFailed as e:
                    workflow.logger.warning("pr_opened notification not delivered", extra={"error": str(e)[:200]})
        else:
            pr = await ctx.act("publisher.push_update", args, effect="push")
            s.pr = {**(s.pr or {}), **pr}
            h.emit("pr.updated", f"PR #{s.pr['number']} updated", node_key=ctx.node_key)
        return NodeResult(outcome="published")


@node_type
class PrFeedbackWait(NodeType):
    node_type = "pr_feedback_wait"
    role = "feedback_ingestor"
    protected = True
    bounded = True  # bounded by the repair budget (repair_limits) and the PR wait expiry
    interrupts = True
    effects = ("expire_session", "round_1")
    description = "Waits in pr_review for PR events (Signal pr.event), debounced; authorizes repair triggers (D-15)."

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        s = ctx.s
        ev = ctx.interrupt({"wait": "pr_feedback"})
        kind = ev["event"]
        if kind in ("merged", "closed", "expired"):
            if not s.evaluation:
                grace = ctx.host.session_state_grace_days()  # the bundle's retention policy (0-7, default 7)
                await ctx.act("artifacts.expire_session_state", {"job_id": s.job_id, "tenant_id": s.tenant_id,
                                                                 "after_days": grace}, effect="expire_session")
                # XD-FIX-R1-238 / R1V-010: WS-07's projector sets jobs.session_purge_due_at from this event type
                ctx.host.emit("artifacts.expire_session_state", f"session state purge in {grace} days after the end",
                              node_key=ctx.node_key)
            return NodeResult(outcome=kind)
        batch = ev["batch"]
        if s.evaluation:
            authorized = batch  # scripted rounds: no humans to authorize (ADR-06-006)
        else:
            allowed = await ctx.act("identity.check_repair_trigger", {
                "job_id": s.job_id, "tenant_id": s.tenant_id, "repo": ctx.host.primary_repo(),
                "actors": sorted({b["actor"]["github_user_id"] for b in batch})})
            authorized = [b for b in batch if b["actor"]["github_user_id"] in set(allowed["allowed_user_ids"])]
        if not authorized:
            return NodeResult(outcome="ignored")
        if s.repair_used >= s.repair_max:
            return NodeResult(outcome="budget_exhausted")
        s.repair_used += 1
        s.exports["__feedback__"] = {"batch": [{"delivery_id": b["delivery_id"], "payload_uri": b["payload_uri"],
                                                "event": b["event"]} for b in authorized][:100]}
        return NodeResult(outcome="feedback", exports={"items": len(authorized)})


@node_type
class FeedbackTriage(NodeType):
    node_type = "feedback_triage"
    role = "feedback_ingestor"
    effects = ("feedback_signals",)
    description = "LLM triage of the authorized feedback batch: tool-less, closed-schema selection (G-24)."

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        s = ctx.s
        batch = s.exports.get("__feedback__", {}).get("batch", [])
        r = await ctx.act("feedback.triage", {"job_id": s.job_id, "tenant_id": s.tenant_id, "cycle": s.cycle,
                                              "events": batch, "refs": dict(s.refs)})
        ctx.set_ref("feedback_triage_uri", r["triage_uri"])
        if not s.evaluation:
            await ctx.act("memory.feedback_signals", {"job_id": s.job_id, "tenant_id": s.tenant_id, "cycle": s.cycle,
                                                      "triage_uri": r["triage_uri"],
                                                      "memory_snapshot_id": s.memory_snapshot_id},
                          effect="feedback_signals")
        return NodeResult(outcome=r["outcome"], exports={"actionable": r.get("actionable", 0)})


@node_type
class PostReply(NodeType):
    node_type = "post_reply"
    role = "auxiliary"
    effects = ("post_reply",)
    config_schema = {"type": "object", "additionalProperties": False, "required": ["template"],
                     "properties": {"template": {"enum": ["ack", "fix_summary", "no_action", "budget_exhausted", "question"]}}}
    description = "Reply on the PR from a fixed template filled with structured data (never model text verbatim)."

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        s = ctx.s
        if s.evaluation:
            return NodeResult(outcome="suppressed")
        await ctx.act("github.post_reply", {"job_id": s.job_id, "repo": ctx.host.primary_repo(), "pr": s.pr,
                                            "template": config["template"], "cycle": s.cycle,
                                            "triage_uri": s.refs.get("feedback_triage_uri"),
                                            "in_reply_to": [b["delivery_id"] for b in s.exports.get("__feedback__", {}).get("batch", [])]},
                      effect="post_reply")
        return NodeResult(outcome="posted")


@node_type
class MemoryWriter(NodeType):
    node_type = "memory_writer"
    role = "memory_writer"
    protected = True
    effects = ("write_candidates", "extract")
    description = "Extracts and writes candidate memory entries with provenance (D-14, D-15; lifecycle per WS-04/D-25)."

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        s = ctx.s
        if s.evaluation:
            return NodeResult(outcome="skipped_evaluation")  # XD-04-01: no memory writes for eval jobs
        ex = await ctx.act("memory.extract_candidates", {
            "job_id": s.job_id, "tenant_id": s.tenant_id, "cycle": s.cycle,
            "candidates_uri": s.refs.get("memory_candidates_uri"), "triage_uri": s.refs.get("feedback_triage_uri"),
            "memory_snapshot_id": s.memory_snapshot_id}, effect="extract")
        r = await ctx.act("memory.write_candidates", {  # extract returns {candidates_uri, sha256, ...} (XD-FIX-R1-100)
            "job_id": s.job_id, "tenant_id": s.tenant_id, "cycle": s.cycle,
            "extracted_uri": ex.get("candidates_uri") or ex.get("uri"), "extracted_sha256": ex.get("sha256"),
            "memory_snapshot_id": s.memory_snapshot_id}, effect="write_candidates")
        ctx.host.emit("memory.candidates_written", f"{r.get('written', 0)} memory candidates", node_key=ctx.node_key)
        return NodeResult(outcome="written", exports={"written": r.get("written", 0)})


@node_type
class Reroute(NodeType):
    node_type = "reroute"
    role = "intake_router"
    protected = True
    config_schema = {"type": "object", "additionalProperties": False, "required": ["toWorkflowType", "startAt"],
                     "properties": {"toWorkflowType": {"enum": ["bugfix", "feature", "api_change"]},
                                    "startAt": {"type": "string"}}}
    description = "Escalates a job to a stricter graph (for example docs -> bugfix when code is touched)."

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        ctx.host.request_reroute(config["toWorkflowType"], config["startAt"])
        return NodeResult(outcome="rerouted")


@node_type
class Route(NodeType):
    node_type = "route"
    role = "auxiliary"
    effect_class = "check_only"
    description = "No-op node with no I/O; carries conditional edges (for example after a join)."

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        return NodeResult(outcome="ok")


__all__ = ["NodeFailed"]
