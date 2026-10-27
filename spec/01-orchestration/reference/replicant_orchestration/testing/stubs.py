"""In-process test doubles for every activity in activity_options.CATALOG (local tests, WS-11).

`FakeWorld` holds scripted behavior and records calls. `world.activities(client)` returns the
activity callables grouped by task queue. The fake Runner advances an AgentRun one phase per
`runner.get_run` call and sends the `run.status_changed` hint Signal (like run-notifier) after each
start, so both the hint path and the polling path are exercised. The fake projector validates
every ProjectionEvent against the frozen C-MSG JSON Schema.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

import jsonschema
import yaml
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .. import ids, registry
from ..activity_options import CATALOG
from ..graphspec import compose, parse_definition, validate_composed

ROOT = pathlib.Path(__file__).resolve().parents[3]  # spec/01-orchestration
CONTRACTS = ROOT.parent / "00-contracts"
GRAPH_FILES = {"bugfix": "bugfix", "feature": "feature", "api_change": "api-change", "docs": "docs-low-risk",
               "hotfix": "hotfix", "maintenance": "maintenance-repo", "pr_repair": "pr-repair", "qa_only": "qa-only"}
BUCKET = "local-replicant-local-artifacts"


def _digest(*parts: str) -> str:
    return "sha256:" + hashlib.sha256("|".join(parts).encode()).hexdigest()


def _policy(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / "policies" / name).read_text())["spec"]


def _fixture(name: str) -> dict[str, Any]:  # non-envelope test fixture (R1-026)
    return yaml.safe_load((ROOT / "policies" / name).read_text())


@dataclass
class FakeRun:
    run_id: str
    role: str
    key: str
    polls: int = 0
    phase: str = "Pending"
    outcome: str = "completed"
    failure: str | None = None
    running_polls: int = 1
    role_exports: dict[str, Any] = field(default_factory=dict)
    cancel_reason: str | None = None
    unscheduled_polls: int = 0  # get_run calls that stay Pending with Scheduled=False (XD-W2-02-04)
    failure_message: str | None = None


@dataclass
class FakeWorld:
    risk: dict[str, str] = field(default_factory=lambda: {"plan": "low", "diff": "low"})
    outcomes: dict[str, list[str]] = field(default_factory=dict)  # role -> outcomes popped per run
    failures: dict[str, list[str]] = field(default_factory=dict)  # role -> failure reasons popped per run
    running_polls: int = 1  # get_run calls that return Running before the terminal phase
    running_polls_by_role: dict[str, int] = field(default_factory=dict)  # per-role override
    base_moves: list[dict[str, Any]] = field(default_factory=list)
    triage: list[str] = field(default_factory=list)
    deny: set[str] = field(default_factory=set)
    router: dict[str, Any] | None = None
    intents: list[dict[str, Any]] = field(default_factory=list)
    bindings: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # repo -> graph modifiers
    target_build_id: str | None = None
    extra_policies: dict[str, Any] = field(default_factory=dict)
    send_hints: bool = True
    runs: dict[str, FakeRun] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    projections: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    invalid_projections: list[str] = field(default_factory=list)
    catalog_override: dict[str, dict] | None = None
    gate_script: dict[str, Any] | None = None  # evaluation mode GateScript (WS-06 schema)
    review_rounds: list[dict[str, Any]] = field(default_factory=list)  # evaluation mode scripted rounds
    diff_sha: str = "6c8e0a2b4d5f7c9e1a3b5d7f9c2e4a6b8d0f1c3e5a7b9d2f4c6e8a0b1d3f5c7e"
    publish_digest_mismatches: list[str] = field(default_factory=list)
    qa_verdicts: list[str] = field(default_factory=list)  # optional explicit QA verdict sequence
    batch_targets: list[dict[str, Any]] = field(default_factory=list)
    license_denied: list[str] = field(default_factory=list)
    # fix-round scenarios
    outages: dict[str, float] = field(default_factory=dict)  # activity -> monotonic time until which it fails
    fatal: dict[str, str] = field(default_factory=dict)  # activity -> non-retryable error type it raises
    health: dict[str, list[str]] = field(default_factory=dict)  # dependency -> breaker states popped per probe
    kill_switches: list[dict[str, Any]] = field(default_factory=list)  # ops.read_kill_switches result
    batch_spend: list[float] = field(default_factory=list)  # cost.batch_spend results popped per call
    eligible_lineage: set[str] = field(default_factory=lambda: {"any", "patch", "model_successor"})
    run_spec_errors: list[str] = field(default_factory=list)
    # wave 2
    capacity: list[dict[str, Any]] = field(default_factory=list)  # CapacityUnavailable details popped per start
    compute_usd: float = 0.0
    compute_limit: float | None = None
    superseded_plan: dict[str, Any] | None = None
    harness_steps: list[dict[str, Any]] = field(default_factory=list)
    takeover_push_reject: bool = False
    on_contested_decision: str = "reopen_if_unpublished"
    unscheduled_polls_by_role: dict[str, int] = field(default_factory=dict)
    quota_available: list[bool] = field(default_factory=list)  # budget.tenant_quota_status answers
    failure_messages: dict[str, str] = field(default_factory=dict)  # role -> AgentRun failure message
    contract_errors: list[str] = field(default_factory=list)  # activity inputs that violated a peer schema

    def __post_init__(self) -> None:
        schema = json.loads((CONTRACTS / "schemas" / "workflow-messages.schema.json").read_text())
        self._proj_validator = jsonschema.Draft202012Validator(
            {"$ref": "#/$defs/ProjectionEvent", "$defs": schema["$defs"]})
        self._backlog_validator = jsonschema.Draft202012Validator(json.loads(
            (ROOT.parent / "04-memory-and-context" / "schemas" / "steering-stage-backlog.schema.json").read_text()))
        self.tenant = "payments"
        qa = json.loads((ROOT.parent / "05-qa" / "schemas" / "qa-activities-io.schema.json").read_text())
        self._qa_validators = {k: jsonschema.Draft202012Validator({"$ref": f"#/$defs/{k}", "$defs": qa["$defs"]})
                               for k in ("ClassifyInput", "ClassifyOutput", "StageContractStubsInput", "IndexResultsInput")}
        mem = yaml.safe_load((ROOT.parent / "04-memory-and-context" / "api" / "memory-service.openapi.yaml").read_text())
        self._mem_validator = jsonschema.Draft202012Validator(
            {"$ref": "#/components/schemas/CreateSnapshotRequest", "components": mem["components"]})
        runner = yaml.safe_load((CONTRACTS / "runner-api.openapi.yaml").read_text())
        self._run_spec_validator = jsonschema.Draft202012Validator(
            {"$ref": "#/components/schemas/RunSpec", "components": runner["components"]})

    # ------------------------------------------------------------------ helpers
    def _check(self, what: str, validator: Any, doc: Any) -> None:
        errs = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
        if errs:
            self.contract_errors.append(f"{what}: {errs[0].message[:200]} at {list(errs[0].path)}")
            raise ApplicationError(f"{what} violates the peer schema", type="InvalidInput", non_retryable=True)

    def _run_outcome(self, run_id: str) -> str | None:
        r = self.runs.get(run_id)
        return r.outcome if r and r.phase == "Succeeded" else ("failed" if r and r.phase == "Failed" else None)

    def called(self, name: str) -> list[dict[str, Any]]:
        return [a for n, a in self.calls if n == name]

    def _rec(self, name: str, arg: Any) -> None:
        self.calls.append((name, arg if isinstance(arg, dict) else {"arg": arg}))

    def bundle_core(self) -> dict[str, Any]:
        policies = {
            "router_rules": _policy("router-rules.yaml"), "repair_limits": _policy("repair-limits.yaml"),
            "runner_polling": _policy("runner-polling.yaml"), "gate_rules": _fixture("gate-routing.fixture.yaml"),
        }
        policies["runner_polling"] = {**policies["runner_polling"], "pending_interval_seconds": 2,
                                      "running_interval_seconds": 3}  # test speed
        for k, v in self.extra_policies.items():
            policies[k] = {**policies[k], **v}
        return {"release_bundle_id": "rb_ds5p3wmcj4dag2e9a8ncp4py6k", "policies": policies,
                "policy_refs": {"memory": {"ref": "policy/memory-default@3", "digest": "sha256:" + "3" * 64}}}

    # ------------------------------------------------------------------ activities
    def activities(self, client: Any) -> dict[str, list[Callable]]:
        w = self
        acts: dict[str, Callable] = {}

        def act(name: str):
            def deco(fn: Callable) -> Callable:
                async def wrapper(arg: Any) -> Any:
                    w._rec(name, arg)
                    if name in w.fatal:
                        raise ApplicationError(f"{name} failed", type=w.fatal[name], non_retryable=True)
                    until = w.outages.get(name)
                    if until is not None and asyncio.get_running_loop().time() < until:  # simulated outage
                        raise ApplicationError(f"{name} unavailable", type="ServiceUnavailable")
                    return await fn(arg)
                wrapper.__name__ = name.replace(".", "_")
                acts[name] = activity.defn(name=name)(wrapper)
                return fn
            return deco

        @act("registry.load_bundle_core")
        async def _(a): return w.bundle_core()

        @act("graph.compose")
        async def _(a):
            doc = yaml.safe_load((ROOT / "graphs" / "definitions" / f"{GRAPH_FILES[a['workflow_type']]}.yaml").read_text())
            base = parse_definition(doc, version=1, digest=_digest("graph", a["workflow_type"]))
            cat = w.catalog_override or registry.catalog_entries()
            mods = w.bindings.get(a.get("repo") or "", [])
            g = compose(base, mods, catalog=cat)
            validate_composed(base, g, catalog=cat)
            return {"spec": g.to_json(), "composed_digest": g.composed_digest(), "environment": "go-service",
                    "environment_digest": "sha256:" + "1" * 64}

        @act("router.classify")
        async def _(a):
            req = a["request"]
            if req.get("workflow_type") not in (None, "auto"):
                return {"workflow_type": req["workflow_type"], "method": "requested", "rules_fired": ["requested-type-honored"]}
            return w.router or {"needs_intent": True, "candidates": ["bugfix", "feature", "api_change", "docs"],
                                "fallback": "bugfix", "rules_fired": ["otherwise-ask-the-model"]}

        @act("router.parse_intent")
        async def _(a): return w.intents.pop(0) if w.intents else {"workflow_type": "bugfix", "confidence": 0.9}

        @act("scm.resolve_base")
        async def _(a): return {"base_ref": a.get("base_ref") or "main", "base_sha": "8c1e4f2a9b7d3c6e5f0a1b2c3d4e5f6a7b8c9d0e"}

        @act("memory.create_snapshot")
        async def _(a):
            w._check("CreateSnapshotRequest", w._mem_validator, a)  # WS-04 schema (R1V-026)
            return {"memory_snapshot_id": ids.derived("msnap", a["job_id"])}

        @act("context.build_briefing")
        async def _(a):
            p = f"s3://{BUCKET}/tenants/{a['tenant_id']}/jobs/{a['job_id']}/steps/context.c{a['cycle']}.n1/"
            return {"briefing_uri": p + "briefing/briefing.json", "task_packet_uri": p + "task_packet/task.json",
                    "working_memory": {"schema": "replicant.working-memory/v1", "decisions": [], "cycle": a["cycle"]}}

        @act("memory.extract_candidates")
        async def _(a): return {"candidates_uri": f"s3://{BUCKET}/m/extract-c{a['cycle']}.json", "sha256": "0" * 64,
                                "proposals": 1, "non_candidates": 0}

        for n in ("memory.feedback_signals", "memory.record_outcome", "memory.write_episode", "cost.record_run",
                  "budget.apply_extension"):
            @act(n)
            async def _(a): return {"ok": True}

        @act("agents.build_run_plan")
        async def _(a):
            missing = [k for k in ("deterministic", "received_at", "ordinal", "workflow_type", "agent_ref",
                                   "environment_ref") if k not in a]
            if missing:  # WS-03 agent-definition.md §3 input (R1V-016)
                w.contract_errors.append(f"build_run_plan missing {missing}")
                raise ApplicationError("build_run_plan input", type="InvalidInput", non_retryable=True)
            return {"agent_ref": {"name": a["agent_ref"], "version": 7, "digest": "sha256:" + "9" * 64},
                    "environment_ref": {"name": a["environment_ref"], "version": 12, "digest": "sha256:" + "1" * 64},
                    "timeouts": a.get("timeouts") or {"startup_seconds": 300, "active_deadline_seconds": 3600,
                                                       "finalize_seconds": 300},
                    "max_cost_usd": 15, "steering_live": a["role"] in ("implementer", "repair"),
                    "network_profile": a.get("network_profile") or "coding-default",
                    "audiences": ["ai-gateway", "memory-mcp", "context-mcp", "repo-mirror", "runner-sandbox"],
                    "scopes": ["gateway:invoke", "memory:read", "memory:search", "context:read", "code_search:read",
                               "repo:fetch", "inbox:read"],
                    "run_plan": {"name": "run_plan", "key": f"tenants/x/jobs/{a['job_id']}/runs/{a['run_id']}/inputs/run-plan.json",
                                 "sha256": "1" * 64, "size_bytes": 512},
                    "models": ["claude-sonnet-4-6"], "max_concurrency": 4,
                    **({"compute_usd_per_job": w.compute_limit} if w.compute_limit is not None else {})}

        @act("budget.allocate_run")
        async def _(a): return {"budget_usd": 15.0, "extension_usd": 10.0}

        @act("budget.tenant_quota_status")
        async def _(a):  # WS-03 shape; with raise_if_exhausted=false an exhausted quota is a normal answer (R1V-009)
            avail = w.quota_available.pop(0) if w.quota_available else True
            if not avail and a.get("raise_if_exhausted", True):
                raise ApplicationError("exhausted", type="TenantBudgetExhausted", non_retryable=True)
            return {"available": avail, "period": "2026-10", "spent_usd": 1000.0, "limit_usd": 1000.0,
                    "resets_at": "2026-11-01T00:00:00Z"}

        @act("agents.stage_inputs")
        async def _(a): return {"run_policy_registered": True}

        @act("qa.stage_contract_stubs")
        async def _(a):
            w._check("StageContractStubsInput", w._qa_validators["StageContractStubsInput"], a)  # R1V-012
            return {"staged": [], "missing": []}

        @act("qa.classify")
        async def _(a):
            w._check("ClassifyInput", w._qa_validators["ClassifyInput"], a)  # R1V-012
            if w.qa_verdicts:
                v = w.qa_verdicts.pop(0)
            elif a.get("failure_reason") == "EnvironmentSetupFailed":
                v = "inconclusive"
            else:
                v = {"completed": "pass", "failed": "fail", "blocked": "inconclusive",
                     "no_changes": "not_applicable"}.get(w._run_outcome(a["run_id"]), "inconclusive")
            rc = {"pass": "all_passed", "fail": "regression", "inconclusive": "environment_failure"}.get(v, "unknown_failure")
            envf = {"stage": "services", "check_id": "svc-kafka-health", "evidence_key": "qa_report/logs/services/kafka.log",
                    "definition_ref": "go-service@12.0.0", "signature": "kafka_not_ready"} if v == "inconclusive" else None
            out = {"verdict": v, "reason_code": rc, "oscillation": False, "env_signature_transient": True,
                   "agentrun_exit_class": {"pass": "pass", "pass_with_warnings": "pass_with_warnings", "fail": "fail",
                                           "not_applicable": "not_applicable"}.get(v, "environment_setup"
                                           if a.get("failure_reason") == "EnvironmentSetupFailed" else "inconclusive_other"),
                   "risk_floor_hint": "low", "blocking": 0,
                   "classified_report_uri": f"s3://{BUCKET}/tenants/payments/jobs/{a['job_id']}/steps/{a['step_id']}/qa_report/classified.json",
                   "test_inventory_delta": {"tests_removed": 0, "tests_skipped_added": 0, "deleted_or_skipped": False},
                   **({"first_env_signature": "kafka_not_ready", "env_failure": envf} if envf else {})}
            w._check("ClassifyOutput", w._qa_validators["ClassifyOutput"], out)
            return out

        @act("gates.resolve_routing")
        async def _(a):
            from ..dsl import RISK_ORDER
            kind, f = a["gate_kind"], a["facts"]
            r = w.bundle_core()["policies"]["gate_rules"]["gates"][kind]
            required = (f["workflow_type"] in r.get("always_for", []) or kind in f.get("require_gates", [])
                        or RISK_ORDER[f["risk_level"]] >= RISK_ORDER[r.get("risk_at_least", "critical")])
            if kind in ("clarification", "budget_extension", "bundle_migration"):
                required = True
            if r.get("approvers", {}).get("requester_only"):
                elig = {"principals": [f["requester"]], "requester_may_decide": True}
            else:
                may = bool(r.get("requester_may_decide", False))
                if kind == "budget_extension":  # WS-09 G-07: self-approval only up to the cap (default 0)
                    may = may and float(f.get("amount_usd", 0)) <= float(r.get("requester_self_approve_max_usd", 0))
                elig = {"okta_groups": ["payments-approvers"], "min_repo_permission": "write", "requester_may_decide": may}
            return {"schema": "replicant.gate-routing/v1", "gate_kind": kind, "required": required,
                    "rule_id": f"example.{kind}", "channels": r["channels"], "eligible": elig,
                    "timing": {"reminder_seconds": r.get("reminder_seconds", []),
                               "reminder_repeat_seconds": r.get("reminder_repeat_seconds", 86400),
                               "escalation_seconds": r.get("escalation_seconds", []),
                               "expiry_seconds": r["expiry_seconds"], "on_expiry": r.get("on_expiry", "expire")},
                    "reapprove_after_rebase": r.get("reapprove_after_rebase", "on_conflict_or_risk_change"),
                    "on_contested_decision": w.on_contested_decision}

        @act("eval.read_gate_script")
        async def _(a):
            sc = w.gate_script or {"default": "approve", "rules": []}
            for i, rule in enumerate(sc.get("rules", [])):
                if rule["gate_kind"] == a["gate_kind"] and rule.get("ordinal", a["ordinal"]) == a["ordinal"]:
                    return {**rule, "rule_index": i}
            return {"decision": sc["default"], "rule_index": -1}

        @act("eval.read_review_round")
        async def _(a):
            k = a["round"]
            return w.review_rounds[k - 1] if k <= len(w.review_rounds) else {"event": "merged"}

        @act("context.stage_inputs")
        async def _(a):
            pre = f"tenants/{a['tenant_id']}/jobs/{a['job_id']}/runs/{a['run_id']}/"
            return {"inputs": {"manifest_uri": f"s3://{BUCKET}/{pre}inputs/manifest.json",
                               "manifest_sha256": hashlib.sha256(pre.encode()).hexdigest()},
                    "artifact_prefixes": {c: f"s3://local-replicant-local-{c}/{pre}" for c in ("artifacts", "transcripts", "session")},
                    "steering_consumed": [m["message_id"] for m in a.get("steering", [])],
                    "approver_feedback_consumed": [f["decision_id"] for f in a.get("approver_feedback", [])]}

        @act("steering.stage_backlog")
        async def _(a):  # WS-04 schema + order rule (XD-W2-04-08)
            errs = list(w._backlog_validator.iter_errors(a))
            if errs or [m["message_id"] for m in a["messages"]] != a["message_ids"]:
                raise ApplicationError(errs[0].message if errs else "messages[] order differs from message_ids[]",
                                       type="steering_backlog_mismatch", non_retryable=True)
            return {"keys": [f"tenants/{w.tenant}/jobs/{a['job_id']}/steps/{a['node_key']}.c{a['cycle']}.n{a['ordinal']}"
                             f"/steering_message/{m}.json" for m in a["message_ids"]]}

        @act("runner.start_run")
        async def _(a):
            key = a["idempotency_key"]
            spec = json.loads(json.dumps(a["run_spec"]))
            spec["run_grant"] = "eyJ0.eyJ0.c2ln"  # the real activity signs RunGrantClaims here (CCR-09-02)
            if spec["callback"]["namespace"] == "default":  # the dev server's namespace
                spec["callback"]["namespace"] = "replicant-local"
            if w.capacity:  # XD-FIX-R2-020 typed capacity error (non-retryable for the activity)
                raise ApplicationError("capacity", w.capacity.pop(0), type="CapacityUnavailable", non_retryable=True)
            errs = list(w._run_spec_validator.iter_errors(spec))
            if errs:
                w.run_spec_errors.append(f"{errs[0].message} at {list(errs[0].path)}")
                raise ApplicationError("invalid RunSpec", type="invalid_request", non_retryable=True)
            run_id = ids.run_id_for(a["tenant_id"], key)
            if run_id not in w.runs:
                role = a["role"]
                out = w.outcomes.get(role, [])
                fail = w.failures.get(role, [])
                r = FakeRun(run_id=run_id, role=role, key=key, outcome=out.pop(0) if out else "completed",
                            failure=fail.pop(0) if fail else None,
                            running_polls=w.running_polls_by_role.get(role, w.running_polls),
                            unscheduled_polls=w.unscheduled_polls_by_role.get(role, 0),
                            failure_message=w.failure_messages.get(role),
                            role_exports={"files_changed": 3, "conflicts": False, "paths": ["internal/ledger/refund.go"]})
                w.runs[run_id] = r
                if w.send_hints:
                    wid = a["workflow_id"]

                    async def hint() -> None:
                        await asyncio.sleep(0.3)
                        await client.get_workflow_handle(wid).signal("run.status_changed", {
                            "schema": "replicant.msg.run-status-changed/v1", "notification_id": f"{run_id}:Provisioning:1",
                            "run_id": run_id, "job_id": a["job_id"], "tenant_id": a["tenant_id"],
                            "idempotency_key": key, "phase": "Provisioning", "terminal": False,
                            "observed_generation": 1, "observed_at": "2026-10-24T08:00:00Z"})
                    asyncio.get_running_loop().create_task(hint())
            return w._status(w.runs[run_id])

        @act("runner.get_run")
        async def _(a):
            r = w.runs[a["run_id"]]
            if r.phase not in ("Succeeded", "Failed", "Cancelled"):
                r.polls += 1
                order = ["Pending", "Provisioning", "Running"]
                if r.phase == "Pending" and r.unscheduled_polls > 0:
                    r.unscheduled_polls -= 1
                elif r.phase in ("Pending", "Provisioning"):
                    r.phase = order[order.index(r.phase) + 1]
                elif r.phase == "Running":
                    r.running_polls -= 1
                    if r.running_polls < 0:
                        r.phase = "Failed" if r.failure else "Succeeded"
                elif r.phase == "Cancelling":
                    r.phase = "Cancelled"
            return w._status(r)

        @act("runner.cancel_run")
        async def _(a):
            r = w.runs.get(a["run_id"])
            if r and r.phase not in ("Succeeded", "Failed", "Cancelled"):
                # takeover: the run finalizes (uploads the workspace archive and diff) before Cancelled
                r.phase = "Cancelling" if a["reason"] == "takeover" else "Cancelled"
                r.cancel_reason = a["reason"]
            return w._status(r) if r else {}

        @act("runner.post_message")
        async def _(a): return {"message_id": a["message_id"], "run_id": a["run_id"], "state": "queued"}

        @act("runner.read_result")
        async def _(a):
            r = next(x for x in w.runs.values() if a["run_id"] == x.run_id)
            pre = f"s3://{BUCKET}/tenants/payments/jobs/j/runs/{r.run_id}/"
            outs = {"planner": {"plan": pre + "plan/plan.json"}, "qa": {"qa_report": pre + "qa_report/report.json"},
                    "reviewer": {"review_report": pre + "review_report/review.json"}}.get(
                r.role, {"diff_bundle": pre + "diff_bundle/changes.tar.zst", "handoff": pre + "handoff/handoff.json"})
            return {"outcome": r.outcome, "outputs": outs, "usage": {"wall_seconds": 60}, "role_exports": r.role_exports}

        @act("risk.classify")
        async def _(a): return {"level": w.risk.get(a["stage"], "low"), "rules_fired": [f"stub.{a['stage']}"]}

        @act("risk.llm_review")
        async def _(a): return {"level": a["deterministic_level"]}

        @act("verifier.check")
        async def _(a): return {"passed": True, "findings": 0, "report_uri": f"s3://{BUCKET}/v/report.json",
                                "paths": ["internal/ledger/refund.go"], "diff_sha256": w.diff_sha}

        @act("checks.run_step")
        async def _(a):
            return {"contract_compat": {"outcome": "compatible", "breaking": False},
                    "consumer_impact": {"outcome": "completed", "consumers": 3},
                    "docs_scope": {"outcome": "in_scope", "escalate": False}}[a["step"]] | {"report_uri": f"s3://{BUCKET}/c/{a['step']}.json"}

        @act("gates.build_packet")
        async def _(a):
            return {"packet_uri": f"s3://{BUCKET}/tenants/{a['tenant_id']}/jobs/{a['job_id']}/gates/{a['gate_id']}/packet-r{a['revision']}.json",
                    "digest": _digest(a["gate_id"], str(a["revision"])), "diff_sha256": w.diff_sha}

        @act("gates.write_approval_record")
        async def _(a):  # C-ART 2.0.0 key (XD-W2-08-03)
            text = (a["decision"].get("comment") or "") + (a.get("answer_text") or "")
            return {"key": f"tenants/{a['tenant_id']}/jobs/{a['job_id']}/gates/{a['gate_id']}/decision-{a['decision']['decision_id']}.json",
                    **({"text_sha256": hashlib.sha256(text.encode()).hexdigest()} if text else {})}

        @act("identity.check_decision")
        async def _(a):
            p = a["principal_id"]
            return {"allow": p not in w.deny, "reason": "not_in_approver_group" if p in w.deny else "ok"}

        @act("identity.check_action")
        async def _(a):
            p = a["principal_id"]
            return {"allow": p not in w.deny, "reason": "not_authorized" if p in w.deny else "ok"}

        @act("identity.check_repair_trigger")
        async def _(a): return {"allowed_user_ids": list(a["actors"])}

        @act("publisher.push_takeover_branch")
        async def _(a):
            if w.takeover_push_reject:
                raise ApplicationError("ci paths", type="PushRejected", non_retryable=True)
            return {"branch": a.get("branch")}

        @act("notify.operator")
        async def _(a): return {"ok": True}

        @act("cost.run_resource_usage")
        async def _(a): return {"compute_usd": w.compute_usd}

        @act("qa.index_results")
        async def _(a):
            w._check("IndexResultsInput", w._qa_validators["IndexResultsInput"], a)
            return {"indexed": True}

        @act("artifacts.resolve_superseded_plan")
        async def _(a): return w.superseded_plan or {"plan_uri": None, "plan_approved": False}

        @act("eval.read_harness_spec")
        async def _(a): return {"steps": list(w.harness_steps)}

        @act("eval.await_inputs")
        async def _(a): return {"manifest_present": True}

        for n in ("notify.gate_opened", "notify.gate_reminder", "notify.gate_escalated", "notify.gate_resolved",
                  "notify.job_event", "github.post_reply", "artifacts.expire_session_state",
                  "publisher.close_pr"):
            @act(n)
            async def _(a): return {"ok": True}

        @act("publisher.check_base")
        async def _(a):
            m = w.base_moves.pop(0) if w.base_moves else {"moved": False}
            return {"moved": m["moved"], "new_base_sha": m.get("new_base_sha", "9" * 40),
                    "applies_cleanly": m.get("clean", True)}

        @act("publisher.publish")
        async def _(a):
            if a.get("approved_diff_sha256") != w.diff_sha:  # the real Publisher refuses (G-03)
                w.publish_digest_mismatches.append(str(a.get("approved_diff_sha256")))
                raise ApplicationError("diff digest mismatch", type="PushRejected", non_retryable=True)
            return {"repo": "github.com/acme-payments/ledger-api", "number": 1431, "state": "draft" if a["draft"] else "open",
                    "url": "https://github.com/acme-payments/ledger-api/pull/1431", "head_sha": "a" * 40}

        @act("publisher.push_update")
        async def _(a):
            if a.get("approved_diff_sha256") != w.diff_sha:
                w.publish_digest_mismatches.append(str(a.get("approved_diff_sha256")))
                raise ApplicationError("diff digest mismatch", type="PushRejected", non_retryable=True)
            return {"head_sha": "b" * 40}

        @act("feedback.triage")
        async def _(a):
            return {"outcome": w.triage.pop(0) if w.triage else "actionable", "actionable": 1,
                    "triage_uri": f"s3://{BUCKET}/t/triage-c{a['cycle']}.json"}

        @act("memory.write_candidates")
        async def _(a): return {"written": 1}

        @act("takeover.assemble_package")
        async def _(a): return {"package_uri": f"s3://{BUCKET}/tenants/x/jobs/{a['job_id']}/takeover/{a['takeover_id']}/package.json"}

        @act("release.check_migration_eligibility")
        async def _(a):
            ok = w.target_build_id is not None and a.get("lineage", "any") in w.eligible_lineage
            return {"eligible": ok, "target_build_id": w.target_build_id,
                    "reason": "no target" if not ok else "", "change_summary": "worker build change"}

        @act("ops.read_kill_switches")
        async def _(a): return {"engaged": list(w.kill_switches)}

        @act("platform.dependency_health")
        async def _(a):
            seq = w.health.get(a["dependency"], [])
            return {"dependency": a["dependency"], "state": seq.pop(0) if seq else "closed"}

        @act("cost.batch_spend")
        async def _(a): return {"spent_usd": w.batch_spend.pop(0) if w.batch_spend else 0.0}

        @act("release.apply_migration_override")
        async def _(a):
            from google.protobuf.field_mask_pb2 import FieldMask
            from temporalio.api.common.v1 import WorkflowExecution
            from temporalio.api.deployment.v1 import WorkerDeploymentVersion as PVer
            from temporalio.api.workflow.v1 import VersioningOverride, WorkflowExecutionOptions
            from temporalio.api.workflowservice.v1 import UpdateWorkflowExecutionOptionsRequest
            await client.workflow_service.update_workflow_execution_options(UpdateWorkflowExecutionOptionsRequest(
                namespace=client.namespace,
                workflow_execution=WorkflowExecution(workflow_id=a["workflow_id"], run_id=a["run_id"]),
                workflow_execution_options=WorkflowExecutionOptions(versioning_override=VersioningOverride(
                    pinned=VersioningOverride.PinnedOverride(
                        behavior=VersioningOverride.PinnedOverrideBehavior.PINNED_OVERRIDE_BEHAVIOR_PINNED,
                        version=PVer(deployment_name="replicant-worker", build_id=a["target_build_id"])))),
                update_mask=FieldMask(paths=["versioning_override"])))
            return {"applied": True}

        if "policy.evaluate_licenses" in CATALOG:  # build N+1 only (worked example)
            @act("policy.evaluate_licenses")
            async def _(a): return {"denied": list(w.license_denied)}

        @act("catalog.resolve_targets")
        async def _(a): return {"children_index_uri": f"s3://{BUCKET}/tenants/{a['tenant_id']}/jobs/{a['job_id']}/batch/children.jsonl",
                                "count": len(w.batch_targets)}

        @act("catalog.read_targets")
        async def _(a): return {"repos": list(w.batch_targets[a["offset"]:a["offset"] + a["limit"]])}

        @act("intake.create_child_job")
        async def _(a): return {"job_id": ids.derived("job", a["parent_job_id"], str(a["index"]))}

        @act("projection.apply")
        async def _(a):
            errs = sorted(w._proj_validator.iter_errors(a), key=lambda e: list(e.path))
            if errs:
                w.invalid_projections.append(f"seq {a.get('seq')}: {errs[0].message} at {list(errs[0].path)}")
                raise ApplicationError("invalid projection", type="SchemaViolation", non_retryable=True)
            w.projections[a["job_id"]].append(a)
            return None

        missing = set(CATALOG) - set(acts)
        assert not missing, f"stub missing activities: {missing}"
        by_queue: dict[str, list[Callable]] = defaultdict(list)
        for name, fn in acts.items():
            by_queue[CATALOG[name].klass.task_queue].append(fn)
        return by_queue

    @staticmethod
    def _status(r: FakeRun) -> dict[str, Any]:
        s = {"run_id": r.run_id, "phase": r.phase, "terminal": r.phase in ("Succeeded", "Failed", "Cancelled"),
             "idempotency_key": r.key}
        if r.phase == "Pending" and r.unscheduled_polls > 0:
            s["conditions"] = [{"type": "Scheduled", "status": "False", "reason": "Unschedulable"}]
        if r.phase == "Succeeded" or (r.phase == "Cancelled" and r.cancel_reason == "takeover"):
            s["result_uri"] = f"s3://{BUCKET}/r/{r.run_id}/run_result/result.json"
            s["artifact_manifest_uri"] = f"s3://{BUCKET}/r/{r.run_id}/manifest.json"
        if r.phase == "Failed":
            retryable = r.failure in ("NodeLost", "PodEvicted", "AgentExitedNonZero")
            s["failure"] = {"reason": r.failure, "retryable": retryable, "message": r.failure_message or "stub"}
        return s
