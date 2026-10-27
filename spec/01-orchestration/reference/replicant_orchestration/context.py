"""NodeContext: the only API node code uses. Deterministic; runs in workflow code.

Rules enforced here (temporal-langgraph.md section 5, determinism rules):
* every unit of real work is an activity; side-effecting activities get an explicit logical
  activity ID `<node_key>.c<cycle>.n<ordinal>.<effect>` (C-IDS 2.1);
* time comes from workflow.now(); there is no randomness; configuration is read only from the
  pinned bundle (loaded by activity and recorded in history);
* node re-entry (after an interrupt resume, or after Continue-As-New at this node) is safe: every
  completed side-effecting step is memoized by logical activity ID in JobState.memo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable

from temporalio import workflow
from temporalio.common import Priority


from . import build_config, ids
from .activity_options import CATALOG
from .engines.base import BoundaryReached
from .registry import NodeFailed
from .state import GateRecord, RunRecord

if TYPE_CHECKING:
    from .workflow import ReplicantJobBase


RUN_TERMINAL = {"Succeeded", "Failed", "Cancelled"}
PRIORITY_KEY = {"urgent": 1, "high": 2, "normal": 3, "low": 4}
# R2-012: run failures caused by a platform dependency. They never consume repair_limits.run_retries; the node
# waits for the dependency's circuit breaker to close (platform.dependency_health), then restarts with jitter.
# NodeLost counts as platform only while the exec node-pool breaker is open (a node-pool event).
PLATFORM_DEPENDENCY = {"GatewayUnavailable": "ai-gateway", "RepoFetchFailed": "repo-mirror", "NodeLost": "exec-node-pool"}


@dataclass
class RunOutcome:
    run_id: str
    phase: str
    outcome: str | None  # run_result.outcome when a result exists
    outputs: dict[str, str]  # output name -> s3 URI
    reason: str | None = None  # AgentRun failure reason when phase != Succeeded
    retryable: bool = False
    usage: dict[str, Any] | None = None
    memory_entries_used: list[str] | None = None
    role_exports: dict[str, Any] | None = None  # small typed values from role_result (WS-03/WS-05)
    result_uri: str | None = None  # run_result object (qa.classify ClassifyInput.result_uri)


class NodeContext:
    def __init__(self, host: "ReplicantJobBase", node_key: str, ordinal: int,
                 interrupt_fn: Callable[[dict[str, Any]], Any]) -> None:
        self.host = host
        self.s = host.s
        self.node_key = node_key
        self.ordinal = ordinal
        self._interrupt_fn = interrupt_fn

    # ----------------------------------------------------------------- identities
    @property
    def cycle(self) -> int:
        return self.s.cycle

    def lid(self, effect: str, ordinal: int | None = None) -> str:
        return ids.logical_activity_id(self.node_key, self.s.cycle, ordinal or self.ordinal, effect)

    # ----------------------------------------------------------------- activities
    async def act(self, name: str, arg: Any, *, effect: str | None = None, ordinal: int | None = None,
                  summary: str | None = None, block: bool = True) -> Any:
        """Execute a catalogued activity. Side-effecting activities require `effect` and are
        memoized by logical activity ID (re-entry safe). Read-only activities may pass an effect to
        get a stable ID and memoization (used where re-entry must not re-ask, e.g. LLM calls).
        Failures follow ReplicantJobBase.call: non-retryable -> NodeFailed(<type>); retries exhausted ->
        dependency hold (block=True) or NodeFailed('dependency_unavailable') (block=False). R2-010."""
        d = CATALOG[name]
        if d.side_effect and not effect:
            raise ValueError(f"activity {name} has side effects and needs an explicit effect/ID")
        activity_id = self.lid(effect, ordinal) if effect else None
        if activity_id and activity_id in self.s.memo:
            return self.s.memo[activity_id]
        result = await self.host.call(name, arg, activity_id=activity_id, summary=summary, block=block)
        if activity_id:
            self.s.memo[activity_id] = result
        return result

    # ----------------------------------------------------------------- interrupts
    def interrupt(self, payload: dict[str, Any]) -> Any:
        """Pause the graph (LangGraph interrupt in the Pregel engine; the interpreter's equivalent
        in the fallback engine). Returns the resume value when the host resumes this node."""
        return self._interrupt_fn({**payload, "node_key": self.node_key})

    # ----------------------------------------------------------------- Runner (D-04, ADR-002)
    async def run_agent(self, *, role: str, agent: str, environment: str, workload_class: str,
                        resource_profile: str = "default", timeouts: dict[str, int] | None = None,
                        inputs: dict[str, Any] | None = None, network_profile: str = "coding-default",
                        steering: bool = False, effect_prefix: str = "",
                        raise_on_failure: bool = True) -> RunOutcome:
        """The Runner-call sequence (graphs/README.md section 2). Per attempt:
        agents.build_run_plan -> budget.allocate_run -> memory.create_snapshot(revocation_filter)
        -> context.stage_inputs -> agents.stage_inputs -> runner.start_run (signs the run grant)
        -> wait (Signal hint + timer + runner.get_run) -> runner.read_result -> cost.record_run.

        RunSpec decision fields (C-RUNNER 2.4.1) are built here, in workflow code, from activity results
        recorded in history (graphs/README.md section 2.1). runner.start_run adds only the per-attempt fields
        `run_grant` and `trace_context`, and computes `spec_digest` from the decision fields it was given."""
        h, s = self.host, self.s
        pol = h.policy("runner_polling")
        limits = h.policy("repair_limits")
        ordinal = self.ordinal
        p = effect_prefix
        max_attempts = 1 + int(limits["run_retries"]["max_per_node"])
        attempt = 0
        platform_restarts = 0
        resume_from: str | None = None  # QA resume after a lost run (XD-05-18)
        while True:
            attempt += 1
            lid = self.lid(f"{p}run", ordinal)
            key = ids.idempotency_key(workflow.info().workflow_id, lid)
            run_id = ids.run_id_for(s.tenant_id, key)
            base = {"job_id": s.job_id, "tenant_id": s.tenant_id, "run_id": run_id, "role": role,
                    "node_key": self.node_key, "cycle": s.cycle, "release_bundle_id": s.release_bundle_id,
                    "parent_job_id": s.parent_job_id}
            if lid not in s.memo and (h.kill_engaged("starts") or h.kill_engaged("runs")):  # C-MSG 1.5.0
                await h.hold_while(lambda: not (h.kill_engaged("starts") or h.kill_engaged("runs")),
                                   kind="kill_switch", node_key=self.node_key, detail="new runs held by operators")
            ev_case = (s.evaluation or {}).get("golden_task_id")
            plan = await self.act("agents.build_run_plan", {  # WS-03 agent-definition.md §3 shape (R1V-016)
                **base, "ordinal": ordinal, "workflow_type": s.workflow_type, "agent_ref": agent,
                "environment_ref": environment, "workload_class": workload_class,
                "resource_profile": resource_profile, "timeouts": timeouts, "network_profile": network_profile,
                "steering_requested": steering,
                # XD-W2-11-01: deterministic when the build says so (local/CI only) or a cassette stub is selected
                "deterministic": bool(build_config.DETERMINISTIC_RUNS or ev_case),
                "received_at": s.request_summary.get("received_at")}, effect=f"{p}plan", ordinal=ordinal)
            budget = await self.act("budget.allocate_run", {**base, "plan_budget_usd": plan.get("max_cost_usd"),
                                                            "job_cap_usd": h.remaining_budget_usd()},
                                    effect=f"{p}alloc", ordinal=ordinal)  # IDs per WS-03 (XD-FIX-R1-071)
            if plan.get("compute_usd_per_job") is not None and lid not in s.memo:  # XD-FIX-R2-064 compute dollars
                use = await self.act("cost.run_resource_usage", {"job_id": s.job_id, "tenant_id": s.tenant_id})
                if float(use.get("compute_usd", 0)) >= float(plan["compute_usd_per_job"]):
                    raise NodeFailed("budget_exceeded", f"compute ${use.get('compute_usd')} reached the environment's "
                                     f"computeUsdPerJob ${plan['compute_usd_per_job']}")
            if s.memory_snapshot_id and not s.evaluation:
                # WS-04 CreateSnapshotRequest (XD-W3-R1V-026): job_id and base_snapshot_id; run context for logs only
                snap = await self.act("memory.create_snapshot", {
                    "tenant_id": s.tenant_id, "job_id": s.job_id, "mode": "revocation_filter",
                    "base_snapshot_id": s.memory_snapshot_id, "run_id": run_id, "role": role, "node_key": self.node_key,
                    "cycle": s.cycle, "parent_job_id": s.parent_job_id}, effect=f"{p}resnap", ordinal=ordinal)
                s.memory_snapshot_id = snap["memory_snapshot_id"]
            staged = await self.act("context.stage_inputs", {
                **base, "refs": dict(s.refs), "memory_snapshot_id": s.memory_snapshot_id,
                "working_memory": s.working_memory, "steering": list(s.steering_queue),
                "steering_refs": list(s.steering_refs),
                # R2-019: request_changes comments, staged as <untrusted_input source="approver_feedback">
                "approver_feedback": list(s.approver_feedback),
                # WS-04 shape (XD-FIX-R1-100): the latest request_changes decision of the cycle
                "latest_gate_decision": ({"gate_id": s.approver_feedback[-1]["gate_id"],
                                          "gate_kind": s.approver_feedback[-1]["gate_kind"], "decision": "request_changes",
                                          "comment": s.approver_feedback[-1]["text"],
                                          "decided_by": s.gates[s.approver_feedback[-1]["gate_id"]].decision["principal_id"]
                                          if s.approver_feedback[-1]["gate_id"] in s.gates
                                          and s.gates[s.approver_feedback[-1]["gate_id"]].decision else None}
                                         if s.approver_feedback else None),
                "run_plan": plan.get("run_plan"),  # XD-FIX-R1-067: listed in the manifest as place run_plan
                # XD-W2-05-04 / R1V-008 (WS-04's agreed form): WS-04 stages the previous run's per-stage checkpoints
                # as `place: qa_file`, `path: checkpoints/<stage>.json` (S3 inputs/qa-checkpoints/<stage>.json)
                "qa_resume": {"previous_run_id": resume_from} if resume_from and role == "qa" else None,
                # R1V-011 (XD-W3-R1V-011): the node removes imageBuild from the staged qa/config.json before createRun
                # when the runtime for qa_build is not Kata
                "qa_config_remove": ["imageBuild"] if role == "qa" and not str(plan.get("isolation_hint")
                                                                                 or "kata").startswith("kata") else None,
                "extra": inputs or {}}, effect=f"{p}stage", ordinal=ordinal)
            h.consume_steering(staged.get("steering_consumed", []))
            done = set(staged.get("approver_feedback_consumed", []))
            s.approver_feedback = [f for f in s.approver_feedback if f["decision_id"] not in done]
            if staged.get("working_memory") is not None:
                s.working_memory = staged["working_memory"]
            # limits.max_cost_usd is a decision field computed here (C-RUNNER 2.4.1): the smallest of the
            # allocation, the agent plan's limit and the job's remaining budget, all recorded in history.
            max_cost = round(min(float(budget["budget_usd"]), float(plan.get("max_cost_usd") or budget["budget_usd"]),
                                 float(h.remaining_budget_usd())), 2)
            ev = s.evaluation or {}
            await self.act("agents.stage_inputs", {**base, "plan": plan, "budget_usd": max_cost,
                                                   "inputs": staged["inputs"],
                                                   # XD-W2-06-02: local-mode cassette selection for eval cases; the
                                                   # activity sends it only when REPLICANT_ENV=local (prod shims reject it)
                                                   "stub_selection": {"root": "evals", "set": ev["golden_task_id"],
                                                                      "key": agent} if ev.get("golden_task_id") else None},
                           effect=f"{p}stage_agent", ordinal=ordinal)
            t = plan["timeouts"]
            self._run_policy = {"tenant_id": s.tenant_id, "job_id": s.job_id, "role": role,
                                "release_bundle_id": s.release_bundle_id, "models": plan.get("models"),
                                "budget_usd": max_cost, "max_concurrency": plan.get("max_concurrency"),
                                "expires_after_seconds": t["startup_seconds"] + t["active_deadline_seconds"]
                                + t["finalize_seconds"] + 300}
            run_spec = self._run_spec(lid=lid, role=role, workload_class=workload_class, plan=plan,
                                      staged=staged, max_cost=max_cost, steering=steering,
                                      resource_profile=resource_profile, network_profile=network_profile)
            cap_since, cap_attempt = None, 0
            while True:  # XD-FIX-R2-020: capacity waits are a workflow loop with durable timers (JobSnapshot.waiting)
                try:
                    status = await self.act("runner.start_run", {
                    "idempotency_key": key, "logical_activity_id": lid, "role": role, "run_id": run_id,
                    "workflow_id": workflow.info().workflow_id,
                    "tenant_id": s.tenant_id, "job_id": s.job_id, "run_spec": run_spec,
                    # RunGrantClaims inputs (CCR-09-02); iat, exp = iat + timeouts + 300 and jti are set at signing
                    "grant": {"role": role, "repo_ids": list(s.repo_ids), "audiences": list(plan.get("audiences", [])),
                              "scopes": list(plan.get("scopes", [])), "budget_usd": max_cost, "acting_for": s.acting_for,
                              "lifetime_seconds": t["startup_seconds"] + t["active_deadline_seconds"]
                              + t["finalize_seconds"] + 300}},
                    effect=f"{p}run", ordinal=ordinal)
                    break
                except NodeFailed as e:
                    if e.reason != "CapacityUnavailable":
                        raise
                    now = workflow.now()
                    cap_since = cap_since or now
                    if now - cap_since > timedelta(hours=6):
                        raise NodeFailed("capacity_unavailable", "no sandbox capacity for 6 hours") from None
                    cap_attempt += 1
                    d = e.details
                    h.set_waiting("tenant_run_quota",  # 429 only; cluster capacity comes from the run wait (R1V-093)
                                  since=cap_since.isoformat(), attempt=cap_attempt, quota_used=d.get("quota_used"),
                                  quota_limit=d.get("quota_limit"))  # no queue position (XD-W2-02-04, R1V-093)
                    await workflow.sleep(timedelta(seconds=int(d.get("retry_after_seconds") or 30)))
            if cap_attempt:
                h.clear_waiting()
            if status["run_id"] != run_id:  # C-IDS: workflow-computed run_id must equal the Runner's
                raise NodeFailed("RunIdMismatch", f"{status['run_id']} != {run_id}")
            rec = s.runs.get(run_id) or RunRecord(
                run_id=run_id, node_key=self.node_key, cycle=s.cycle, ordinal=ordinal, role=role,
                started_at=workflow.now().isoformat(), steering_enabled=bool(plan.get("steering_live")) and steering,
                finalize_seconds=int(t["finalize_seconds"]))
            s.runs[run_id] = rec
            if rec.phase == "Pending" and rec.active:
                h.emit("run.status_changed", f"{role} run {run_id} created", run_id=run_id)
            final = await self._wait_run(rec, status, t, pol)
            outcome = RunOutcome(run_id=run_id, phase=final["phase"], outcome=None, outputs={},
                                 result_uri=final.get("result_uri"))
            if final.get("result_uri"):
                res = await self.act("runner.read_result", {"run_id": run_id, "result_uri": final["result_uri"],
                                                            "manifest_uri": final.get("artifact_manifest_uri")})
                outcome.outcome, outcome.outputs = res["outcome"], res.get("outputs", {})
                outcome.usage, outcome.memory_entries_used = res.get("usage"), res.get("memory_entries_used")
                outcome.role_exports = res.get("role_exports", {})
                await self.act("cost.record_run", {**base, "usage": res.get("usage"), "plan": plan},
                               effect=f"{p}cost", ordinal=ordinal)
            if final["phase"] == "Succeeded":
                return outcome
            failure = final.get("failure") or {}
            reason = failure.get("reason", "CancelledByRequest" if final["phase"] == "Cancelled" else "ControllerError")
            outcome.reason, outcome.retryable = reason, bool(failure.get("retryable"))
            if final["phase"] == "Cancelled" and h.cancel_requested:
                raise NodeFailed("CancelledByRequest")
            if final.get("retry_node"):  # ops.retry_node: restart with ordinal + 1 (not charged to run_retries)
                s.retry_nodes.pop(self.node_key, None)
                h.emit("node.retried", f"{self.node_key} re-run by operator", node_key=self.node_key)
                ordinal = s.next_ordinal(self.node_key)
                attempt -= 1
                continue
            if final.get("cancelled_by_operator"):  # kill switch `runs`: hold, then restart (no retry consumed)
                await h.hold_while(lambda: not h.kill_engaged("runs"), kind="kill_switch", node_key=self.node_key,
                                   detail="active runs cancelled by operators")
                ordinal = s.next_ordinal(self.node_key)
                attempt -= 1
                continue
            if s.evaluation and "cassette_miss" in (failure.get("message") or ""):  # XD-W2-06-02: 599 cassette_miss
                raise NodeFailed("harness_error", "gateway cassette miss (the case is a harness error, not a failure)")
            if reason == "SecurityViolation":  # CCR-09-04: never retried, never routed around
                h.security_alert(run_id, failure.get("message", ""))
                raise NodeFailed("security_violation", failure.get("message", "")[:500])
            if reason == "GatewayBudgetExceeded":
                if await self._budget_exceeded(run_id, failure, budget):
                    ordinal = s.next_ordinal(self.node_key)
                    attempt = 0
                    continue
                raise NodeFailed("budget_exhausted", failure.get("message", "")[:500])
            dep = PLATFORM_DEPENDENCY.get(reason)
            if dep and platform_restarts < int(limits.get("platform_dependency", {}).get("max_restarts_per_node", 20)):
                health = await self.act("platform.dependency_health", {"dependency": dep, "tenant_id": s.tenant_id})
                if reason != "NodeLost" or health["state"] != "closed":
                    await h.wait_for_dependency(dep, health, node_key=self.node_key)  # R2-012 circuit breaker
                    platform_restarts += 1
                    ordinal = s.next_ordinal(self.node_key)
                    attempt -= 1  # a platform outage is not charged to the node's retry budget
                    continue
            if not raise_on_failure:  # QA classifies environment failures itself (WS-05)
                return outcome
            if outcome.retryable and attempt < max_attempts:
                if reason in ("NodeLost", "PodEvicted", "Preempted", "ArtifactUploadFailed"):
                    resume_from = run_id  # the next QA attempt resumes from this run's checkpoints (XD-05-18)
                await workflow.sleep(timedelta(seconds=int(limits["run_retries"]["backoff_seconds"]) * attempt))
                ordinal = s.next_ordinal(self.node_key)  # deliberate re-invocation => new key and run_id
                continue
            raise NodeFailed(reason, failure.get("message", "")[:2000], retryable=outcome.retryable)

    def _run_spec(self, *, lid: str, role: str, workload_class: str, plan: dict[str, Any], staged: dict[str, Any],
                  max_cost: float, steering: bool, resource_profile: str, network_profile: str) -> dict[str, Any]:
        """C-RUNNER 3.0.0 RunSpec without the per-attempt fields (run_grant, trace_context). Every value comes
        from workflow state or an activity result already recorded in history (graphs/README.md 2.1)."""
        s, h = self.s, self.host
        r = h.primary_repo()
        repo = {k: v for k, v in {"provider": r.get("provider", "github"), "host": r["host"], "owner": r["owner"],
                                  "name": r["name"], "repo_id": r["repo_id"], "base_ref": r.get("base_ref"),
                                  "base_sha": r.get("base_sha")}.items() if v is not None}
        spec: dict[str, Any] = {
            "schema": "replicant.run-spec/v1", "tenant_id": s.tenant_id, "job_id": s.job_id,
            "workflow_id": workflow.info().workflow_id, "logical_activity_id": lid, "role": role,
            "workload_class": workload_class, "workflow_type": s.workflow_type,
            "release_bundle_id": s.release_bundle_id,
            "agent_definition": {"kind": "agent", **plan["agent_ref"]},
            "environment_definition": {"kind": "environment", **plan["environment_ref"]},
            "resource_profile": plan.get("resource_profile", resource_profile), "repo": repo,
            "inputs": {"manifest_uri": staged["inputs"]["manifest_uri"],
                       "manifest_sha256": staged["inputs"]["manifest_sha256"]},
            "artifact_prefixes": dict(staged["artifact_prefixes"]),
            "network_profile": plan.get("network_profile", network_profile),
            "timeouts": {k: int(plan["timeouts"][k]) for k in ("startup_seconds", "active_deadline_seconds",
                                                                  "finalize_seconds")},
            "limits": {"max_cost_usd": max_cost},
            "steering": {"enabled": bool(steering)},
            "priority": s.priority,
            "callback": {"kind": "temporal_signal", "namespace": workflow.info().namespace,
                         "workflow_id": workflow.info().workflow_id, "signal_name": "run.status_changed"},
        }
        if plan.get("isolation_hint"):
            spec["isolation_hint"] = plan["isolation_hint"]
        if plan.get("max_output_bytes"):
            spec["limits"]["max_output_bytes"] = int(plan["max_output_bytes"])
        return spec

    async def _budget_exceeded(self, run_id: str, failure: dict[str, Any], budget: dict[str, Any]) -> bool:
        """WS-03 gateway.md 6.6: run/job budget -> budget_extension gate (requester may self-approve only
        up to requesterSelfApproveMaxUsd, G-07); tenant period quota -> wait for the quota to reset."""
        h, s = self.host, self.s
        lim = h.policy("repair_limits")["budget"]
        scope = (failure.get("message") or "").split(":", 1)[0] or "run"
        if scope == "tenant_period":
            # R1V-009 / R1V-015 (XD-FIX-R2-020): a budget wait, not a human wait. Status stays as it is, phase
            # budget_gate, JobSnapshot.waiting = tenant_budget_exhausted (resets_at). The status check asks with
            # raise_if_exhausted=false (WS-03 agent-definition.md §3), so an exhausted quota is an answer, not an error.
            since = workflow.now()
            h.set_status(s.status, "budget_gate")
            deadline = since + timedelta(days=int(lim.get("tenant_quota_wait_days", 7)))
            n = 0
            while workflow.now() < deadline:
                q = await self.act("budget.tenant_quota_status", {"tenant_id": s.tenant_id, "job_id": s.job_id,
                                                                  "raise_if_exhausted": False})
                if q["available"]:
                    h.clear_waiting()
                    return True
                n += 1
                h.set_waiting("tenant_budget_exhausted", since=since.isoformat(), attempt=n, resets_at=q.get("resets_at"),
                              contact_group=q.get("contact_group"))
                await workflow.sleep(timedelta(seconds=int(lim.get("tenant_quota_check_seconds", 3600))))
            h.clear_waiting()
            raise NodeFailed("tenant_quota_exhausted", "tenant model budget did not reset in time")
        if not lim.get("extension_gate", True):
            return False
        amount = float(budget.get("extension_usd", 0) or 0)
        ext_key = f"{self.node_key}.c{s.cycle}.n{s.runs[run_id].ordinal}.extension_count"
        if ext_key not in s.memo:  # count once per failed run, even across re-entry
            s.budget_extensions += 1
            s.memo[ext_key] = s.budget_extensions
        run_ordinal = s.runs[run_id].ordinal
        d = await self.gate("budget_extension", packet={"run_id": run_id, "scope": scope, "amount_usd": amount},
                            facts={"amount_usd": amount, "extensions_so_far": s.budget_extensions - 1},
                            ordinal=run_ordinal)
        if d.get("decision") != "approve":
            return False
        await self.act("budget.apply_extension", {"job_id": s.job_id, "tenant_id": s.tenant_id, "amount_usd": amount,
                                                  "decision_id": d.get("decision_id")},
                       effect="extend", ordinal=run_ordinal)
        return True

    async def _wait_run(self, rec: RunRecord, status: dict[str, Any], timeouts: dict[str, int],
                        pol: dict[str, Any]) -> dict[str, Any]:
        h = self.host
        self._run_policy = getattr(self, "_run_policy", None)
        final_key = f"{rec.node_key}.c{rec.cycle}.n{rec.ordinal}.final"  # re-entry: never re-poll a finished run
        if final_key in self.s.memo:
            return self.s.memo[final_key]
        # R1V-020: the controller may extend finalize once by finalizeSeconds (throttled uploads, WS-02 controller.md §9),
        # so the workflow's run timer covers 2 x finalize
        budget = (timeouts["startup_seconds"] + timeouts["active_deadline_seconds"]
                  + 2 * timeouts["finalize_seconds"] + int(pol.get("deadline_slack_seconds", 300)))
        deadline = workflow.now() + timedelta(seconds=budget)
        killed = False
        retried = False
        unscheduled_since = None  # XD-W2-02-04: Pending with Scheduled=False for > 60 s is a cluster-capacity wait
        while status["phase"] not in RUN_TERMINAL:
            if status["phase"] == "Pending" and any(c.get("type") == "Scheduled" and c.get("status") == "False"
                                                    for c in status.get("conditions") or []):
                unscheduled_since = unscheduled_since or workflow.now()
                waited = workflow.now() - unscheduled_since
                if waited > timedelta(seconds=60):
                    h.set_waiting("cluster_capacity", since=unscheduled_since.isoformat(), attempt=1)  # no queue position
            elif unscheduled_since is not None:
                unscheduled_since = None
                if (self.s.waiting or {}).get("reason") == "cluster_capacity":
                    h.clear_waiting()
            if h.history_heavy() and not h.in_parallel_branch(self.node_key):
                # History safe point inside a long run wait (ADR-01-004): continue-as-new now; the next run
                # re-enters this node, finds start_run memoized, and keeps polling the same run_id. Never in
                # a parallel branch: the join would wait for a sibling that the next run does not re-enter.
                raise BoundaryReached(self.node_key, "history")
            await h.process_notices()
            if self.s.retry_nodes.get(rec.node_key) and not retried:  # ops.retry_node (CCR-07-12)
                await self.act("runner.cancel_run", {"run_id": rec.run_id, "reason": "operator",
                                                     "detail": f"ops.retry_node {self.s.retry_nodes[rec.node_key]}"},
                               effect="cancel_retry", ordinal=rec.ordinal)
                retried = True
            if h.kill_engaged("runs") and not killed:  # C-MSG 1.5.0 ops.kill_switch `runs`
                await self.act("runner.cancel_run", {"run_id": rec.run_id, "reason": "operator",
                                                     "detail": "kill switch"}, effect="cancel_op", ordinal=rec.ordinal)
                killed = True
            interval = pol["pending_interval_seconds"] if status["phase"] in ("Pending", "Provisioning") \
                else pol["running_interval_seconds"]
            remaining = (deadline - workflow.now()).total_seconds()
            if remaining <= 0:
                await self.act("runner.cancel_run", {"run_id": rec.run_id, "reason": "orchestrator_deadline"},
                               effect="cancel", ordinal=rec.ordinal)
                status = {**status, "phase": "Failed", "failure": {"reason": "OrchestratorDeadlineExceeded",
                                                                    "retryable": True}}
                break
            wake = h._wake
            try:
                await workflow.wait_condition(lambda: h.hints.get(rec.run_id, False) or h.cancel_requested
                                              or h._wake != wake,
                                              timeout=timedelta(seconds=min(interval, remaining)),
                                              timeout_summary=f"poll {rec.run_id}")
            except TimeoutError:
                pass
            h.hints.pop(rec.run_id, None)
            try:
                # XD-FIX-R1-286: the activity also checks the shim's run policy and re-PUTs it on 404 from these
                # values (idempotent), so no extra activity or history is needed per poll.
                new = await self.act("runner.get_run", {"run_id": rec.run_id, "ensure_run_policy": self._run_policy}, block=False)
            except NodeFailed as e:  # Runner down: keep polling until the orchestrator deadline
                if e.reason in ("run_not_found", "run_expired"):
                    raise NodeFailed("RunLost", e.message) from None
                continue
            if new["phase"] != status["phase"]:
                rec.phase = new["phase"]
                rec.isolation = new.get("isolation")
                if new["phase"] not in RUN_TERMINAL:  # the terminal transition is emitted once, below
                    h.emit("run.status_changed", f"run {rec.run_id} {new['phase']}", run_id=rec.run_id)
            status = new
        if (self.s.waiting or {}).get("reason") == "cluster_capacity":
            h.clear_waiting()
        rec.phase = status["phase"]
        rec.active = False
        rec.reason = (status.get("failure") or {}).get("reason")
        rec.finished_at = workflow.now().isoformat()
        rec.result_uri = status.get("result_uri")
        rec.artifact_manifest_uri = status.get("artifact_manifest_uri")
        h.emit("run.status_changed", f"run {rec.run_id} {rec.phase}", run_id=rec.run_id)
        if killed and status["phase"] == "Cancelled":
            status = {**status, "cancelled_by_operator": True}
        if retried and status["phase"] == "Cancelled":
            status = {**status, "retry_node": True}
        self.s.memo[final_key] = status
        return status

    # ----------------------------------------------------------------- gates (gates.md)
    async def routing(self, kind: str, facts: dict[str, Any] | None = None, ordinal: int | None = None) -> dict[str, Any]:
        """GateRouting for this gate opening (WS-07 logic via gates.resolve_routing; ADR-01-009)."""
        return await self.act("gates.resolve_routing", {"gate_kind": kind, "facts": self.host.gate_facts(kind, facts)},
                              effect=f"route_{kind[:8]}", ordinal=ordinal)

    async def gate(self, kind: str, *, packet: dict[str, Any], routing: dict[str, Any] | None = None,
                   facts: dict[str, Any] | None = None, ordinal: int | None = None) -> dict[str, Any]:
        """Open (once) and wait for a human gate. Returns {state, decision...}. In evaluation mode the
        gate opens identically and is resolved from the gate script (XD-06-02)."""
        h, s = self.host, self.s
        o = ordinal or self.ordinal  # a node may open the same gate kind once per run attempt (budget)
        memo_key = f"gate:{self.node_key}:{kind}:c{s.cycle}:n{o}"
        gate_id = s.memo.get(memo_key)
        if gate_id is None:
            gord = s.next_gate_ordinal(kind)
            gate_id = ids.gate_id_for(s.job_id, kind, s.cycle, gord)
            s.memo[memo_key] = gate_id
        g = s.gates.get(gate_id)
        if g is None:
            routing = routing or await self.routing(kind, facts, ordinal=o)
            built = await self.act("gates.build_packet", {
                "job_id": s.job_id, "tenant_id": s.tenant_id, "gate_id": gate_id, "kind": kind,
                "revision": 1, "refs": dict(s.refs), "risk": s.risk, "extra": packet,
            }, effect=f"packet_{kind[:8]}_r1", ordinal=o)
            now = workflow.now()
            timing = routing["timing"]
            g = GateRecord(
                gate_id=gate_id, kind=kind, node_key=self.node_key, cycle=s.cycle, ordinal=o,
                state="open", opened_at=now.isoformat(), presented_digest=built["digest"],
                packet_uri=built["packet_uri"], channels=list(routing["channels"]), eligible=dict(routing["eligible"]),
                expires_at=(now + timedelta(seconds=timing["expiry_seconds"])).isoformat(),
                next_reminder_at=(now + timedelta(seconds=timing["reminder_seconds"][0])).isoformat()
                if timing.get("reminder_seconds") else None,
                held=s.paused and s.pause_mode == "at_next_gate", schedule=dict(timing),
                release_block=dict(s.release_blocked) if s.release_blocked and kind in ("plan_approval", "publish_approval") else None,
                subject={k: built[k] for k in ("diff_sha256", "plan_sha256") if built.get(k)},
                routing={k: routing.get(k) for k in ("rule_id", "reapprove_after_rebase", "gate_kind", "on_contested_decision")},
            )
            if g.held:
                g.held_since = now.isoformat()
            s.gates[gate_id] = g
            h.on_gate_opened(g)
            if g.held and s.paused_by and not s.evaluation:  # R2-023: tell the person who paused
                await self.act("notify.job_event", {"job_id": s.job_id, "kind": "gate_held", "gate_id": gate_id,
                                                    "gate_kind": kind, "to": s.paused_by,
                                                    "note": "expiry clock suspended until the job is resumed"},
                               effect=f"notify_held_{kind[:8]}", ordinal=o)
            if s.evaluation:
                await h.resolve_gate_from_script(g)
            elif not g.held:
                await self.act("notify.gate_opened", {"job_id": s.job_id, "gate_id": gate_id,
                                                      "kind": kind, "channels": g.channels,
                                                      "packet_uri": g.packet_uri, "eligible": g.eligible},
                               effect=f"notify_open_{kind[:8]}", ordinal=o)
        if g.state == "open":
            resumed = self.interrupt({"wait": "gate", "gate_id": gate_id})
            if isinstance(resumed, dict) and resumed.get("gate_id") == gate_id:
                g = s.gates[gate_id]
        if g.state == "open":
            raise NodeFailed("GateResumedWhileOpen", gate_id)
        if g.state == "cancelled" and s.evaluation and not g.decision:  # stop_case (XD-FIX-R1-143)
            raise NodeFailed("eval_stopped", "eval_stop_case")
        return {"state": g.state, **(g.decision or {})}

    async def reopen_gate(self, prev: GateRecord) -> dict[str, Any]:
        """XD-FIX-R1-173: a new instance of a gate whose approval was withdrawn: ordinal + 1, the same packet
        revision and digest, banner reason approval_withdrawn_identity. Opened and awaited like any gate."""
        h, s = self.host, self.s
        memo_key = f"reopen:{prev.gate_id}"
        gate_id = s.memo.get(memo_key)
        if gate_id is None:
            gord = s.next_gate_ordinal(prev.kind)
            gate_id = ids.gate_id_for(s.job_id, prev.kind, s.cycle, gord)
            s.memo[memo_key] = gate_id
        g = s.gates.get(gate_id)
        if g is None:
            now = workflow.now()
            expiry = int(prev.schedule.get("expiry_seconds", 604800))
            g = GateRecord(gate_id=gate_id, kind=prev.kind, node_key=self.node_key, cycle=s.cycle, ordinal=self.ordinal,
                           state="open", opened_at=now.isoformat(), presented_digest=prev.presented_digest,
                           packet_uri=prev.packet_uri, channels=list(prev.channels), eligible=dict(prev.eligible),
                           expires_at=(now + timedelta(seconds=expiry)).isoformat(), schedule=dict(prev.schedule),
                           subject=dict(prev.subject), routing={**prev.routing, "banner": "approval_withdrawn_identity"},
                           reopened_from=prev.gate_id)
            s.gates[gate_id] = g
            h.on_gate_opened(g)
            h.emit("gate.reopened", f"{prev.kind} gate reopened: approval withdrawn (identity contested)", gate_id=gate_id,
                   decision_id=(prev.decision or {}).get("decision_id"))
            await self.act("notify.gate_opened", {"job_id": s.job_id, "gate_id": gate_id, "kind": prev.kind,
                                                  "channels": g.channels, "packet_uri": g.packet_uri, "eligible": g.eligible,
                                                  "banner": "approval_withdrawn_identity"},
                           effect=f"notify_reopen_{prev.kind[:8]}")
        if g.state == "open":
            self.interrupt({"wait": "gate", "gate_id": gate_id})
            g = s.gates[gate_id]
        return {"state": g.state, **(g.decision or {})}

    # ----------------------------------------------------------------- misc
    def set_ref(self, name: str, uri: str | None) -> None:
        if uri:
            self.s.refs[name] = uri


__all__ = ["NodeContext", "RunOutcome", "Priority", "PRIORITY_KEY"]
