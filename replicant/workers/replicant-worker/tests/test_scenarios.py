"""End-to-end scenarios against a local Temporal dev server (temporalio 1.34.0, server 1.32.0).

Run: python tests/test_scenarios.py [scenario ...]
Each scenario prints PASS, FAIL or SKIP (a scenario that needs a different env setting).
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import (
    ALICE,
    BOB,
    decide_req,
    histories,
    new_job_id,
    open_gate,
    pr_event,
    replay_all,
    side_effect_activity_ids,
    snapshot,
    start_env,
    start_job,
    wait_for,
    workers,
)
from support.stubs import FakeWorld
from temporalio.client import WorkflowUpdateFailedError


class Skipped(Exception):
    """Raised by a scenario that needs an env setting this run doesn't have; reported as SKIP."""


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


async def common_checks(name: str, client, world: FakeWorld, job_id: str) -> dict:
    hists = await histories(client, job_id)
    fails = await replay_all(hists)
    check(not fails, f"replay failures: {fails}")
    check(not world.invalid_projections, f"invalid projections: {world.invalid_projections[:3]}")
    check(not world.contract_errors, f"peer-schema violations: {world.contract_errors[:3]}")
    check(not world.run_spec_errors, f"RunSpec violations: {world.run_spec_errors[:3]}")
    keys = [r.key for r in world.runs.values()]
    check(len(keys) == len(set(keys)), "duplicate Runner idempotency keys")
    seq = side_effect_activity_ids(hists)
    dup = [k for k, n in Counter(seq).items() if n > 1]
    check(not dup, f"duplicate logical activity IDs across runs: {dup}")
    seqs = [p["seq"] for p in world.projections[job_id]]
    check(len(seqs) == len(set(seqs)), "duplicate projection seq")
    info = {"runs": len(hists), "events_per_run": sorted((len(h.events) for h in hists), reverse=True),
            "total_events": sum(len(h.events) for h in hists), "activity_ids": seq,
            "projections": len(seqs), "runner_runs": len(world.runs)}
    return info


# --------------------------------------------------------------------------------------------
async def s_bugfix_full(env) -> str:
    """High risk: plan gate + publish gate (two channels race), PR, one repair cycle, merge."""
    world = FakeWorld(risk={"plan": "high", "diff": "high"})
    c = env.client
    job = new_job_id(1)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        g = await open_gate(c, job, "plan_approval")
        # validator rejections (C-MSG V1-V10): stale view, wrong channel, self-approval, unknown gate
        for bad, kind in [(dict(presented_digest="sha256:" + "f" * 64), "StaleApprovalView"),
                          (dict(channel="github"), "ChannelNotAllowed"),
                          (dict(actor={"principal_id": "usr_01knk8dfr0fvjc7v0b9vhvkzbp"}), "SelfApprovalForbidden"),
                          (dict(gate_id="gate_" + "0" * 26), "UnknownGate")]:
            req = {**decide_req(g, "approve", ALICE, "slack", 99), **bad}
            try:
                await h.execute_update("gate.decide", req, id=req["decision_id"] + kind)
                raise AssertionError(f"expected {kind}")
            except WorkflowUpdateFailedError as e:
                check(e.cause.type == kind, f"expected {kind}, got {e.cause.type}")
        r = await h.execute_update("gate.decide", decide_req(g, "approve", ALICE, "slack", 1), id=decide_req(g, "approve", ALICE, "slack", 1)["decision_id"])
        check(r["outcome"] == "accepted", f"plan approve: {r}")
        # duplicate click (same Update ID) is deduplicated by Temporal and returns the same result
        r2 = await h.execute_update("gate.decide", decide_req(g, "approve", ALICE, "slack", 1), id=decide_req(g, "approve", ALICE, "slack", 1)["decision_id"])
        check(r2 == r, "duplicate update must return the original result")
        pg = await open_gate(c, job, "publish_approval")
        a = decide_req(pg, "approve", ALICE, "slack", 2)
        b = {**decide_req(pg, "approve", BOB, "github", 3),  # local-profile Gitea review (C-MSG 1.1.0)
             "actor": {"principal_id": BOB, "channel_identity": {"kind": "gitea", "id": "17", "login": "bob"}}}
        ra, rb = await asyncio.gather(h.execute_update("gate.decide", a, id=a["decision_id"]),
                                      h.execute_update("gate.decide", b, id=b["decision_id"]), return_exceptions=True)
        outs = sorted(x["outcome"] if isinstance(x, dict) else x.cause.type for x in (ra, rb))
        # first valid decision wins; the loser either waited on the handler lock (superseded) or was
        # rejected by validator V4 (GateAlreadyDecided). Both report the same winner.
        check(outs in (["accepted", "superseded"], ["GateAlreadyDecided", "accepted"]), f"race outcomes {outs}")
        winners = {x["winning_decision"]["decision_id"] for x in (ra, rb) if isinstance(x, dict)}
        check(len(winners) == 1, f"one winner expected: {winners}")
        await wait_for(lambda: _status(c, job, "waiting_external"), 60, "pr_review")
        await h.signal("pr.event", pr_event(job, "review_submitted", 1))
        await h.signal("pr.event", pr_event(job, "review_submitted", 1))  # redelivery: deduped
        # risk is high, so the repair push needs publish approval again (D-19: presence by deterministic rules)
        pg2 = await open_gate(c, job, "publish_approval", 120)
        d = decide_req(pg2, "approve", ALICE, "ui", 4)
        await h.execute_update("gate.decide", d, id=d["decision_id"])
        await wait_for(lambda: _cycle_back_in_review(c, job, 1), 90, "repair cycle 1 finished")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 2))
        res = await h.result()
        check(res["status"] == "succeeded" and res["outcome"] == "pr_merged", f"result {res}")
        snap = max(world.projections[job], key=lambda p: p["seq"])["snapshot"]  # projector applies by seq
        check(snap["status"] == "succeeded" and snap["cycle"] == 1 and snap["repair_budget"]["used"] == 1, "final snapshot")
        info = await common_checks("bugfix_full", c, world, job)
        check(info["runs"] >= 4, f"expected >=4 runs (CAN at pr_review, cycle, pr_review), got {info['runs']}")
        check(len(world.called("gates.write_approval_record")) == 3, "approval records")
    return f"runs={info['runs']} events={info['events_per_run']} projections={info['projections']}"


async def _status(c, job, status):
    return (await snapshot(c, job))["status"] == status


async def _cycle_back_in_review(c, job, cycle):
    """Cycle `cycle` finished: back in the pr_wait node (not merely the post-CAN instant before triage)."""
    s = await snapshot(c, job)
    return (s["cycle"] == cycle and s["repair_budget"]["used"] == cycle and s["status"] == "waiting_external"
            and (s.get("current_node") or {}).get("node_key") == "pr_wait")


# --------------------------------------------------------------------------------------------
async def s_feature_parallel(env) -> str:
    """Feature: mandatory gates, skeptic and impact in parallel, contract check; PR closed unmerged."""
    world = FakeWorld()
    c = env.client
    job = new_job_id(2)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="feature")
        g = await open_gate(c, job, "plan_approval")  # required for feature even at low risk
        await h.execute_update("gate.decide", decide_req(g, "approve", ALICE, "ui", 1), id=decide_req(g, "approve", ALICE, "ui", 1)["decision_id"])
        pg = await open_gate(c, job, "publish_approval")
        await h.execute_update("gate.decide", decide_req(pg, "approve", ALICE, "ui", 2), id=decide_req(pg, "approve", ALICE, "ui", 2)["decision_id"])
        await wait_for(lambda: _status(c, job, "waiting_external"), 60, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_closed", 1))
        res = await h.result()
        check(res["outcome"] == "pr_closed_unmerged" and res["status"] == "rejected", f"result {res}")
        info = await common_checks("feature_parallel", c, world, job)
        roles = Counter(r.role for r in world.runs.values())
        check(roles["reviewer"] >= 2, f"skeptic + review runs expected: {roles}")
    return f"runs={info['runs']} roles={dict(roles)}"


async def s_low_risk_rebase(env) -> str:
    """Low-risk bugfix: no gates; base moved (clean) -> re-QA on the new base -> publish; QA fails once."""
    world = FakeWorld(outcomes={"qa": ["failed"]}, base_moves=[{"moved": True, "clean": True}, {"moved": False}])
    c = env.client
    job = new_job_id(3)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        await wait_for(lambda: _status(c, job, "waiting_external"), 90, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        info = await common_checks("low_risk_rebase", c, world, job)
        roles = Counter(r.role for r in world.runs.values())
        check(roles["qa"] == 3 and roles["implementer"] == 2, f"qa fail loop + re-QA after rebase: {roles}")
        check(not world.called("notify.gate_opened"), "no gates expected at low risk")
    return f"runs={info['runs']} roles={dict(roles)}"


async def s_cancel_during_run(env) -> str:
    """job.cancel while the implementer run is active: run cancelled, job cancelled."""
    world = FakeWorld(running_polls_by_role={"implementer": 1000})
    c = env.client
    job = new_job_id(4)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        await wait_for(lambda: _active_role(c, job, "implementer"), 60, "implementer running")
        req = {"schema": "replicant.msg.job-control/v1", "action_id": "act_" + "1" * 26, "action": "cancel",
               "actor": {"principal_id": ALICE}, "channel": "ui", "requested_at": "2026-10-24T10:00:00Z"}
        r = await h.execute_update("job.cancel", req, id=req["action_id"])
        check(r["outcome"] == "accepted", f"cancel {r}")
        res = await h.result()
        check(res["status"] == "cancelled", f"result {res}")
        cancels = world.called("runner.cancel_run")
        check(len(cancels) == 1 and cancels[0]["reason"] == "job_cancelled", f"cancel calls {cancels}")
        info = await common_checks("cancel_during_run", c, world, job)
    return f"runs={info['runs']}"


async def _active_role(c, job, role):
    s = await snapshot(c, job)
    return any(r["role"] == role and r["phase"] == "Running" for r in s["runs"])


async def s_gate_expiry(env) -> str:
    """Gate reminders and expiry run on durable timers (policy shortened for the test)."""
    world = FakeWorld(risk={"plan": "high", "diff": "high"}, extra_policies={"gate_rules": {}})
    rules = world.bundle_core()["policies"]["gate_rules"]
    rules["gates"]["plan_approval"].update(reminder_seconds=[2], reminder_repeat_seconds=2,
                                           escalation_seconds=[3], expiry_seconds=7)
    world.extra_policies = {"gate_rules": rules}
    c = env.client
    job = new_job_id(5)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        res = await h.result()
        check(res["outcome"] == "gate_expired" and res["status"] == "expired", f"result {res}")
        check(len(world.called("notify.gate_reminder")) >= 2, "reminders")
        check(len(world.called("notify.gate_escalated")) == 1, "escalation")
        await common_checks("gate_expiry", c, world, job)
    return f"reminders={len(world.called('notify.gate_reminder'))}"


async def s_router_clarify(env) -> str:
    """auto routing: low LLM confidence -> clarification gate -> answer -> docs graph -> escalates? no."""
    world = FakeWorld(intents=[{"workflow_type": "feature", "confidence": 0.4, "questions": ["Which service?"]},
                               {"workflow_type": "docs", "confidence": 0.92}])
    c = env.client
    job = new_job_id(6)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="auto")
        g = await open_gate(c, job, "clarification")
        req = decide_req(g, "answer", "usr_01knk8dfr0fvjc7v0b9vhvkzbp", "github", 1, answer_text="the ledger-api docs")
        r = await h.execute_update("gate.decide", req, id=req["decision_id"])
        check(r["outcome"] == "accepted", f"answer {r}")
        await wait_for(lambda: _status(c, job, "waiting_external"), 90, "pr_review")
        s = await snapshot(c, job)
        check(s["workflow_type"] == "docs", f"routed to {s['workflow_type']}")
        logs = [e for g in (await snapshot(c, job))["gates"] for e in g.get("decision_log", [])]
        check(any(e.get("text_sha256") and e.get("text_key") for e in logs), f"R1V-046 text_sha256 in decision_log: {logs}")
        ans = [a.get("clarification_answer") for a in world.called("router.parse_intent")]
        check(ans[0] is None and ans[1]["text"] == "the ledger-api docs" and ans[1]["trust"] == "untrusted",
              f"R1-017 answer text in the activity input: {ans}")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        info = await common_checks("router_clarify", c, world, job)
    return f"type={s['workflow_type']} runs={info['runs']}"


async def s_repair_budget(env) -> str:
    """Hotfix: repair budget 2 cycles; a third feedback batch exhausts it; publish gate required."""
    world = FakeWorld()
    c = env.client
    job = new_job_id(7)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="hotfix", priority="urgent")
        pg = await open_gate(c, job, "publish_approval")  # hotfix: always required, requester may not approve
        await h.execute_update("gate.decide", decide_req(pg, "approve", ALICE, "slack", 1), id=decide_req(pg, "approve", ALICE, "slack", 1)["decision_id"])
        await wait_for(lambda: _cycle_back_in_review(c, job, 0), 90, "pr_review")
        for cyc in (1, 2):
            await h.signal("pr.event", pr_event(job, "review_submitted", 10 + cyc))
            # hotfix: gate_rules publish_approval always_for hotfix, so every repair push is approved
            g = await open_gate(c, job, "publish_approval", 120)
            d = decide_req(g, "approve", ALICE, "slack", 10 + cyc)
            await h.execute_update("gate.decide", d, id=d["decision_id"])
            await wait_for(lambda: _cycle_back_in_review(c, job, cyc), 90, f"cycle {cyc}")
        await h.signal("pr.event", pr_event(job, "review_submitted", 20))
        res = await h.result()
        check(res["outcome"] == "repair_budget_exhausted" and res["status"] == "failed", f"result {res}")
        info = await common_checks("repair_budget", c, world, job)
    return f"runs={info['runs']} events={info['total_events']}"


async def s_takeover(env) -> str:
    world = FakeWorld(running_polls_by_role={"implementer": 1000})
    c = env.client
    job = new_job_id(8)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        await wait_for(lambda: _active_role(c, job, "implementer"), 60, "implementer running")
        req = {"schema": "replicant.msg.job-control/v1", "action_id": "act_" + "2" * 26, "action": "takeover",
               "actor": {"principal_id": ALICE}, "channel": "cli", "requested_at": "2026-10-24T10:00:00Z"}
        r = await h.execute_update("job.takeover", req, id=req["action_id"])
        check(r["outcome"] == "accepted" and r["takeover_id"].startswith("tko_"), f"takeover {r}")
        res = await h.result()
        check(res["status"] == "taken_over", f"result {res}")
        check(world.called("runner.cancel_run")[0]["reason"] == "takeover", "takeover cancel reason")
        check(len(world.called("takeover.assemble_package")) == 1, "package")
        impl = next(r for r in world.runs.values() if r.role == "implementer")
        pushed = world.called("publisher.push_takeover_branch")[0]["diff_bundle_uri"]
        check(impl.run_id in (pushed or ""), f"takeover pushes the cancelled run's finalized diff (R1-008): {pushed}")
        await common_checks("takeover", c, world, job)
    return "ok"


async def s_steer(env) -> str:
    """Steering: live delivery to a running implementer; queued delivery before the next node."""
    world = FakeWorld(running_polls_by_role={"implementer": 6})
    c = env.client
    job = new_job_id(9)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        await wait_for(lambda: _active_role(c, job, "implementer"), 60, "implementer running")
        mk = lambda n, d: {"schema": "replicant.msg.job-steer/v1", "message_id": f"msg_{str(n) * 26}", "actor": {"principal_id": ALICE},
                           "channel": "ui", "text": "Use decimal.Decimal for proration", "delivery": d, "sent_at": "2026-10-24T10:00:00Z"}
        r1 = await h.execute_update("job.steer", mk(1, "auto"), id=mk(1, "auto")["message_id"])
        r2 = await h.execute_update("job.steer", mk(2, "next_node"), id=mk(2, "next_node")["message_id"])
        check(r1["outcome"] == "delivered_live" and r2["outcome"] == "queued_next_node", f"steer {r1} {r2}")
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        staged = [a for a in world.called("context.stage_inputs") if a.get("steering")]
        check(staged and staged[0]["steering"][0]["message_id"] == mk(2, "x")["message_id"], "queued steering staged")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        await h.result()
        await common_checks("steer", c, world, job)
    return "ok"


# --------------------------------------------------------------------------------------------
REQUESTER = "usr_01knk8dfr0fvjc7v0b9vhvkzbp"  # requester in the C-JOBREQ example


async def s_eval_mode(env) -> str:
    """Evaluation mode (CCR-06-01, XD-06-02): scripted gates via the shared transition, no notifications,
    no identity checks, no memory writes, scripted review round, publish to eval git only."""
    world = FakeWorld(risk={"plan": "high", "diff": "high"},
                      gate_script={"schema": "replicant.gate-script/v1", "default": "approve",
                                   "rules": [{"gate_kind": "plan_approval", "ordinal": 1, "decision": "request_changes",
                                              "comment": "Keep the change inside internal/proration."}]},
                      review_rounds=[{"event": "feedback", "batch": [pr_event("x", "review_submitted", 1)]}])
    c = env.client
    job = new_job_id(10)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix",
                            options={"evaluation": {"mode": "eval", "gate_script_uri": "s3://local-replicant-local-eval/gs/1.json",
                                                    "publish_to_eval_git": True}},
                            related={"eval_case": {"eval_run_id": "eval_01m5p7x7r0bt69j0h0fbxepw91",
                                                   "golden_task_id": "gtask_01m5p7x7r0bt69j0h0fbxepw92"}})
        res = await h.result()
        check(res["outcome"] == "ready_to_publish", f"result {res}")  # scripted merge (WS-06 §3.3, R1V-014)
        evs = [p["event"]["type"] for p in world.projections[job]]
        check(evs.count("gate.auto_resolved") >= 4, f"auto-resolved gates: {evs.count('gate.auto_resolved')}")
        check(not [n for n, _ in world.calls if n.startswith("notify.") or n.startswith("identity.")], "no notify/identity in eval")
        check(not world.called("memory.write_candidates") and not world.called("memory.record_outcome"), "no memory writes")
        roles = Counter(r.role for r in world.runs.values())
        check(roles["planner"] == 2 and roles["repair"] == 1, f"plan revised once, one scripted repair cycle: {roles}")
        snap = max(world.projections[job], key=lambda p: p["seq"])["snapshot"]
        check(snap.get("evaluation", {}).get("mode") == "eval", "snapshot.evaluation")
        fb = [a["approver_feedback"] for a in world.called("context.stage_inputs") if a.get("approver_feedback")]
        check(fb and fb[0][0]["text"] == "Keep the change inside internal/proration.", f"R2-019 feedback staged: {fb[:1]}")
        await common_checks("eval_mode", c, world, job)
    return f"auto_resolved={evs.count('gate.auto_resolved')} roles={dict(roles)}"


async def s_budget_extension(env) -> str:
    """GatewayBudgetExceeded -> budget_extension gate (requester may not self-approve, G-07) -> approve ->
    budget.apply_extension -> new run (ordinal+1)."""
    world = FakeWorld(failures={"implementer": ["GatewayBudgetExceeded"]})
    c = env.client
    job = new_job_id(11)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        g = await open_gate(c, job, "budget_extension")
        bad = decide_req(g, "approve", REQUESTER, "ui", 1)
        try:
            await h.execute_update("gate.decide", bad, id=bad["decision_id"])
            raise AssertionError("requester self-approval of a budget extension must be rejected")
        except WorkflowUpdateFailedError as e:
            check(e.cause.type == "SelfApprovalForbidden", e.cause.type)
        d = decide_req(g, "approve", ALICE, "ui", 2)
        await h.execute_update("gate.decide", d, id=d["decision_id"])
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        check(len(world.called("budget.apply_extension")) == 1, "extension applied once")
        impl = sorted(a["logical_activity_id"] for a in world.called("runner.start_run") if a["role"] == "implementer")
        check(impl == ["implement.c0.n1.run", "implement.c0.n2.run"], f"implementer runs {impl}")
        await common_checks("budget_extension", c, world, job)
    return f"implementer runs={impl}"


async def s_qa_environment(env) -> str:
    """QA EnvironmentSetupFailed twice: retry_qa, then operator_and_gate (risk floor high) -> publish gate;
    the implementer is never re-run for an environment problem (XD-05-03)."""
    world = FakeWorld(failures={"qa": ["EnvironmentSetupFailed", "EnvironmentSetupFailed"]})
    c = env.client
    job = new_job_id(12)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        pg = await open_gate(c, job, "publish_approval", 120)
        d = decide_req(pg, "approve", ALICE, "slack", 1)
        await h.execute_update("gate.decide", d, id=d["decision_id"])
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        roles = Counter(r.role for r in world.runs.values())
        check(roles["implementer"] == 1 and roles["qa"] == 2, f"roles {roles}")
        ops = world.called("notify.operator")  # XD-W2-05-05 payload
        check(ops and ops[0]["event_key"] == "qa.environment_failure" and ops[0]["dedupe_key"].startswith("qa-env:")
              and ops[0]["recipients"] == {"roles": ["operators", "environment_owners"]}, f"operator alert {ops[:1]}")
        snap = max(world.projections[job], key=lambda p: p["seq"])["snapshot"]
        check(snap["risk"]["level"] == "high", f"risk floor applied: {snap['risk']}")
        await common_checks("qa_environment", c, world, job)
    return f"roles={dict(roles)}"


async def s_security_violation(env) -> str:
    """CCR-09-04: SecurityViolation is never retried; the job fails with outcome security_violation."""
    world = FakeWorld(failures={"implementer": ["SecurityViolation"]})
    c = env.client
    job = new_job_id(13)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        res = await h.result()
        check(res["outcome"] == "security_violation" and res["status"] == "failed", f"result {res}")
        check(sum(1 for r in world.runs.values() if r.role == "implementer") == 1, "no retry")
        await common_checks("security_violation", c, world, job)
    return "ok"


async def s_migration(env) -> str:
    """Consented migration of a job waiting at a gate from build A to build B (ADR-01-002)."""
    from temporalio.common import PinnedVersioningOverride, WorkerDeploymentVersion
    A, B = "b20261001-aaaaaaaaaaaa", "b20261024-bbbbbbbbbbbb"
    world = FakeWorld(risk={"plan": "high", "diff": "low"}, target_build_id=B)
    c = env.client
    job = new_job_id(14)
    async with workers(c, world, build_id=A), workers(c, world, build_id=B, activity_workers=False, set_current=False):
        h = await start_job(c, job, workflow_type="bugfix",
                            versioning_override=PinnedVersioningOverride(WorkerDeploymentVersion("replicant-worker", A)))
        g = await open_gate(c, job, "plan_approval")
        offer = {"schema": "replicant.msg.offer-bundle-migration/v1", "action_id": "act_" + "3" * 26,
                 "actor": {"principal_id": ALICE}, "target_release_bundle_id": "rb_" + "x" * 26,
                 "reason": "reviewer prompt fix"}
        r = await h.execute_update("job.offer_bundle_migration", offer, id=offer["action_id"])
        check(r["outcome"] == "gate_opened", f"offer {r}")
        mg = await open_gate(c, job, "bundle_migration")
        d = decide_req(mg, "approve", REQUESTER, "ui", 1)
        await h.execute_update("gate.decide", d, id=d["decision_id"])
        async def on_b():
            desc = await c.get_workflow_handle(job).describe()
            v = desc.raw_description.workflow_execution_info.versioning_info
            return v.deployment_version.build_id == B and (await snapshot(c, job))["release_bundle_id"] == offer["target_release_bundle_id"]
        await wait_for(on_b, 60, "job running on build B")
        g2 = await open_gate(c, job, "plan_approval")  # the same gate, re-entered on build B
        check(g2["gate_id"] == g["gate_id"], "same plan gate after migration")
        d2 = decide_req(g2, "approve", ALICE, "ui", 2)
        await h.execute_update("gate.decide", d2, id=d2["decision_id"])
        pg = await open_gate(c, job, "publish_approval", 120)  # risk never decreases: plan-stage high persists
        d3 = decide_req(pg, "approve", ALICE, "ui", 3)
        await h.execute_update("gate.decide", d3, id=d3["decision_id"])
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        info = await common_checks("migration", c, world, job)
    return f"runs={info['runs']}"


async def s_maintenance_batch(env) -> str:
    """MaintenanceBatch: 3 repos, window 2; children reach PR review (frees slots), merge, batch completes."""
    from replicant_worker.workflows.maintenance import BatchInput
    repos = [{"provider": "github", "host": "github.com", "owner": "acme-payments", "name": n, "repo_id": f"R_{i}"}
             for i, n in enumerate(["ledger-api", "billing-worker", "fx-rates"])]
    world = FakeWorld(batch_targets=repos, extra_policies={"repair_limits": {"maintenance": {"max_concurrency": 2}}})
    c = env.client
    parent = new_job_id(20)
    async with workers(c, world):
        jr = dict(__import__("harness").JOB_REQUEST)
        jr = {**jr, "workflow_type": "maintenance", "targets": [],
              "target_selector": {"catalog_query": {"languages": ["go"]}, "max_repos": 10}}
        h = await c.start_workflow("MaintenanceBatch", BatchInput(job_id=parent, tenant_id="payments",
                                   release_bundle_id="rb_ds5p3wmcj4dag2e9a8ncp4py6k", job_request=jr),
                                   id=parent, task_queue="replicant-jobs")
        async def all_in_review():
            snap = await h.query("batch.snapshot")
            return snap["children_by_status"].get("waiting_external", 0) == 3
        await wait_for(all_in_review, 240, "3 children in PR review")
        kids = [a for a in world.called("intake.create_child_job")]
        from replicant_worker import ids
        for i in range(3):
            cid = ids.derived("job", parent, str(i))
            await c.get_workflow_handle(cid).signal("pr.event", pr_event(cid, "pull_request_merged", 100 + i))
        res = await h.result()
        check(res["children_by_status"].get("succeeded") == 3, f"batch result {res}")
        fails = await replay_all(await histories(c, parent))
        check(not fails, f"replay {fails}")
    return f"children={len(kids)} result={res['children_by_status']}"


async def s_tenant_codec(env) -> str:
    """XD-09-06: every payload encrypted with the job's tenant key, routed by serialization context."""
    import dataclasses

    from harness import RUNNER
    from temporalio.client import Client
    from temporalio.converter import DataConverter
    from temporalio.worker import Replayer

    from replicant_worker.codec import ENCODING, LocalKeyProvider, TenantContextCodec, TenantDirectory
    from replicant_worker.workflows.maintenance import MaintenanceBatch
    from replicant_worker.workflows.replicant_job import ReplicantJob
    directory = TenantDirectory()
    conv = dataclasses.replace(DataConverter.default, payload_codec=TenantContextCodec(LocalKeyProvider(), directory))
    c = await Client.connect(env.client.service_client.config.target_host, namespace=env.client.namespace,
                             data_converter=conv)
    world = FakeWorld()
    job = new_job_id(30)
    directory.register(job, "payments")  # control-plane-api knows the tenant from the intake row
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
    hists = await histories(c, job)
    first = min(hists, key=lambda x: x.events[0].event_time.ToDatetime())
    start = first.events[0].workflow_execution_started_event_attributes
    meta = start.input.payloads[0].metadata
    check(meta.get("encoding") == ENCODING and meta.get("replicant-tenant") == b"payments", "start input encrypted for tenant")
    check(start.search_attributes.indexed_fields["ReplicantTenantId"].data == b'"payments"', "search attributes stay clear")
    acts = [e.activity_task_scheduled_event_attributes for h2 in hists for e in h2.events
            if e.HasField("activity_task_scheduled_event_attributes")]
    check(all(a.input.payloads[0].metadata.get("encoding") == ENCODING for a in acts), "every activity input encrypted")
    r = Replayer(workflows=[ReplicantJob, MaintenanceBatch], workflow_runner=RUNNER, data_converter=conv)
    fails = [x for x in [await r.replay_workflow(h2, raise_on_replay_failure=False) for h2 in hists] if x.replay_failure]
    check(not fails, f"replay with codec: {fails}")
    return f"encrypted activity inputs={len(acts)}"


from tests_support import LICENSE_MODIFIER  # noqa: E402


async def s_license_prestep(env) -> str:
    """Worked example, run with REPLICANT_EXTRA_NODE_MODULES=replicant_worker.nodes.license_scan
    (build N+1). Config-only insertion via a codebase-binding modifier for one repo; a violation ends the
    job before implementation; a clean scan proceeds."""
    from replicant_worker import registry
    check("license_policy_scan" in registry.registered(), "run with REPLICANT_EXTRA_NODE_MODULES=replicant_worker.nodes.license_scan")
    out = []
    for n, denied in ((40, ["AGPL-3.0-only"]), (41, [])):
        world = FakeWorld(bindings={"github.com/acme-payments/ledger-api": [LICENSE_MODIFIER]}, license_denied=denied)
        c = env.client
        job = new_job_id(n)
        async with workers(c, world):
            h = await start_job(c, job, workflow_type="bugfix")
            if not denied:
                await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
                await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
            res = await h.result()
            roles = [r.role for r in world.runs.values()]
            if denied:
                check(res["outcome"] == "policy_violation" and "implementer" not in roles, f"violation path {res} {roles}")
            else:
                check(res["outcome"] == "pr_merged" and roles.index("qa") < roles.index("implementer"), f"clean path {roles}")
            ids_ = [a["logical_activity_id"] for a in world.called("runner.start_run")]
            check("license_scan.c0.n1.run" in ids_, f"scan run id {ids_}")
            await common_checks(f"license_prestep_{n}", c, world, job)
            out.append(res["outcome"])
    return f"outcomes={out}"


async def s_history_budget(env) -> str:
    """Measurement for history-budget.md: high risk (both gates and push gates), 5 repair cycles, realistic
    poll counts per run (Running polls at the production 120 s cadence: planner 5, implementer 12, QA 10,
    reviewer 4, repair 8). Records per-run events, bytes, carry size, and an event-type histogram."""
    from collections import Counter as C
    world = FakeWorld(risk={"plan": "high", "diff": "high"},
                      running_polls_by_role={"planner": 5, "implementer": 12, "qa": 10, "reviewer": 4, "repair": 8})
    c = env.client
    job = new_job_id(50)
    approvals = 0

    async def approve(kind):
        nonlocal approvals
        approvals += 1
        g = await open_gate(c, job, kind, 240)
        d = decide_req(g, "approve", ALICE, "slack", 500 + approvals)
        await h.execute_update("gate.decide", d, id=d["decision_id"])

    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        await approve("plan_approval")
        await approve("publish_approval")
        await wait_for(lambda: _cycle_back_in_review(c, job, 0), 240, "pr_review")
        for cyc in range(1, 6):
            await h.signal("pr.event", pr_event(job, "review_submitted", 600 + cyc))
            await approve("publish_approval")  # push gate (risk high)
            await wait_for(lambda: _cycle_back_in_review(c, job, cyc), 240, f"cycle {cyc}")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 700))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        info = await common_checks("history_budget", c, world, job)
        hists = sorted(await histories(c, job), key=lambda x: x.events[0].event_time.ToDatetime())
        runs = []
        for hh in hists:
            st = hh.events[0].workflow_execution_started_event_attributes
            carry = sum(len(p.data) for p in st.input.payloads)
            hist_bytes = sum(e.ByteSize() for e in hh.events)
            runs.append({"events": len(hh.events), "bytes": hist_bytes, "input_bytes": carry,
                         "types": dict(C(type(e).__name__ if False else e.WhichOneof("attributes") for e in hh.events))})
        info["runs_detail"] = runs
    return f"runs={len(runs)} events={[r['events'] for r in runs]} total={sum(r['events'] for r in runs)}"


async def s_mid_run_can(env) -> str:
    """Run with REPLICANT_HISTORY_CAN_EVENTS=150: the implementer's long run wait crosses the history ceiling,
    the job continues-as-new mid-wait, re-enters `implement`, keeps polling the same run_id (start_run is
    memoized: one Runner run), and completes."""
    from replicant_worker import build_config
    if build_config.HISTORY_CAN_EVENTS > 300:
        raise Skipped("run separately with REPLICANT_HISTORY_CAN_EVENTS=150")
    world = FakeWorld(running_polls_by_role={"implementer": 40})
    c = env.client
    job = new_job_id(60)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        await wait_for(lambda: _status(c, job, "waiting_external"), 300, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        impl = [a for a in world.called("runner.start_run") if a["role"] == "implementer"]
        check(len({a["idempotency_key"] for a in impl}) == 1, "one implementer run across the mid-run CAN")
        info = await common_checks("mid_run_can", c, world, job)
        check(info["runs"] >= 4, f"expected several CANs, got {info['runs']}")
    return f"runs={info['runs']} events={info['events_per_run']}"


# -------------------------------------------------------------------------------------------- fix round (Phase 3)
FAST_BLOCK = {"dependency_block": {"probe_initial_seconds": 1, "probe_max_seconds": 2, "jitter_seconds": 2,
                                   "max_hours": 1}}


def _iso(delta_s: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _notice(kind: str, n: int, *, current: str, recommended: str | None = None, deadline: str | None = None) -> dict:
    sig = {"schema": "replicant.msg.release-notice/v1", "notice_id": f"ntc_{str(n) * 26}"[:30], "kind": kind,
           "current_release_bundle_id": current, "issued_by": ALICE, "issued_at": _iso(0)}
    if recommended:
        sig["recommended_release_bundle_id"] = recommended
    if deadline:
        sig["deadline"] = deadline
    return sig


def _kill(n: int, state: str, switches: list[str], scope: str = "tenant", value: str | None = "payments") -> dict:
    sig = {"schema": "replicant.msg.kill-switch/v1", "notice_id": f"ntc_{str(n) * 26}"[:30], "scope": scope,
           "state": state, "switches": switches, "issued_by": ALICE, "issued_at": _iso(n / 1000), "reason": "test hold"}
    if value and scope != "global":
        sig["scope_value"] = value
    return sig


def _events(world, job) -> list[str]:
    return [p["event"]["type"] for p in sorted(world.projections[job], key=lambda p: p["seq"])]


async def _hold(c, job, kind):
    s = await snapshot(c, job)
    return s["status"] in ("paused", "waiting_human") and s.get("paused") is not None and kind


BUNDLE0 = "rb_ds5p3wmcj4dag2e9a8ncp4py6k"


async def s_gateway_outage(env) -> str:
    """R2-012: GatewayUnavailable three times (more than run_retries.max_per_node = 2). The node holds on the
    ai-gateway breaker instead of failing, restarts with jitter when it closes, and the job completes."""
    world = FakeWorld(failures={"implementer": ["GatewayUnavailable"] * 3},
                      health={"ai-gateway": ["open", "open", "closed", "open", "closed", "closed"]},
                      extra_policies={"repair_limits": FAST_BLOCK})
    c = env.client
    job = new_job_id(70)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        await wait_for(lambda: _status(c, job, "waiting_external"), 240, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        impl = [r for r in world.runs.values() if r.role == "implementer"]
        check(len(impl) == 4, f"implementer runs {len(impl)} (3 platform failures not charged to run_retries)")
        ev = _events(world, job)
        check(ev.count("job.blocked_on_dependency") == 3 and ev.count("job.released") == 3, f"holds {ev.count('job.blocked_on_dependency')}")
        info = await common_checks("gateway_outage", c, world, job)
    return f"implementer_runs={len(impl)} runs={info['runs']}"


async def s_kill_switch(env) -> str:
    """C-MSG 1.5.0 ops.kill_switch: `publishing` engaged in the out-of-band mirror at start; `runs` engaged while
    the implementer runs (run cancelled with reason operator, job held), released (new run); the publisher
    holds until `publishing` is released."""
    world = FakeWorld(running_polls_by_role={"implementer": 30},
                      kill_switches=[_kill(1, "engaged", ["publishing"], scope="global", value=None)])
    c = env.client
    job = new_job_id(71)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        await wait_for(lambda: _active_role(c, job, "implementer"), 60, "implementer running")
        await h.signal("ops.kill_switch", _kill(2, "engaged", ["runs"]))
        async def held():
            s = await snapshot(c, job)
            return s["status"] == "paused" and any(r["role"] == "implementer" and r["phase"] == "Cancelled" for r in s["runs"])
        await wait_for(held, 60, "runs held")
        cancels = [a for a in world.called("runner.cancel_run") if a["reason"] == "operator"]
        check(len(cancels) == 1, f"operator cancel {cancels}")
        await h.signal("ops.kill_switch", _kill(3, "released", ["runs"]))
        world.running_polls_by_role = {}
        async def publish_held():
            return "job.held" in _events(world, job)[-12:] and (await snapshot(c, job))["phase"] == "publishing" \
                and (await snapshot(c, job))["status"] == "paused"
        await wait_for(publish_held, 180, "publishing held")
        check(not world.called("publisher.publish"), "no publish while held")
        await h.signal("ops.kill_switch", _kill(4, "released", ["publishing"], scope="global", value=None))
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        impl = [r for r in world.runs.values() if r.role == "implementer"]
        check(len(impl) == 2, f"implementer runs {len(impl)}")
        await common_checks("kill_switch", c, world, job)
    return f"implementer_runs={len(impl)}"


async def s_model_retiring(env) -> str:
    """R2-044 / C-MSG 1.5.0 model_retiring: forced migration onto the operator-approved successor bundle at the
    next safe point, with no bundle_migration gate (no requester consent)."""
    from temporalio.common import PinnedVersioningOverride, WorkerDeploymentVersion
    A, B = "b20261001-aaaaaaaaaaab", "b20261024-bbbbbbbbbbbc"
    succ = "rb_" + "m" * 26
    world = FakeWorld(risk={"plan": "high", "diff": "low"}, target_build_id=B)
    c = env.client
    job = new_job_id(72)
    async with workers(c, world, build_id=A), workers(c, world, build_id=B, activity_workers=False, set_current=False):
        h = await start_job(c, job, workflow_type="bugfix",
                            versioning_override=PinnedVersioningOverride(WorkerDeploymentVersion("replicant-worker", A)))
        g = await open_gate(c, job, "plan_approval")
        await h.signal("release.notice", _notice("model_retiring", 5, current=BUNDLE0, recommended=succ,
                                                 deadline=_iso(3600)))
        async def on_b():
            desc = await c.get_workflow_handle(job).describe()
            v = desc.raw_description.workflow_execution_info.versioning_info
            return v.deployment_version.build_id == B and (await snapshot(c, job))["release_bundle_id"] == succ
        await wait_for(on_b, 90, "forced migration to B")
        snap = await snapshot(c, job)
        check(not [x for x in snap["gates"] if x["kind"] == "bundle_migration"], "no consent gate")
        g2 = await open_gate(c, job, "plan_approval")
        check(g2["gate_id"] == g["gate_id"], "same plan gate after migration")
        d = decide_req(g2, "approve", ALICE, "ui", 1)
        await h.execute_update("gate.decide", d, id=d["decision_id"])
        await _approve_publish(c, h, job)
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        lin = [a["lineage"] for a in world.called("release.check_migration_eligibility")]
        check(lin == ["model_successor"], f"lineage checks {lin}")
        await common_checks("model_retiring", c, world, job)
    return "migrated without consent"


async def s_bundle_patched(env) -> str:
    """R1-005 / ADR-01-003: bundle_patched notice -> lineage verified by activity -> B' recorded in state,
    search attribute and projection (D-03), no consent."""
    from harness import DEFAULT_BUILD
    patched = "rb_" + "p" * 26
    world = FakeWorld(risk={"plan": "high", "diff": "low"}, target_build_id=DEFAULT_BUILD)
    c = env.client
    job = new_job_id(73)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        g = await open_gate(c, job, "plan_approval")
        await h.signal("release.notice", _notice("bundle_patched", 6, current=BUNDLE0, recommended=patched))
        async def recorded():
            return (await snapshot(c, job))["release_bundle_id"] == patched
        await wait_for(recorded, 60, "patch bundle recorded")
        desc = await h.describe()
        sa = desc.typed_search_attributes
        from temporalio.common import SearchAttributeKey
        check(sa.get(SearchAttributeKey.for_keyword("ReplicantReleaseBundleId")) == patched, "search attribute")
        d = decide_req(g, "approve", ALICE, "ui", 1)
        await h.execute_update("gate.decide", d, id=d["decision_id"])
        await _approve_publish(c, h, job)
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        check("bundle.patched" in _events(world, job), "bundle.patched projected")
        await common_checks("bundle_patched", c, world, job)
    return "B' recorded"


async def s_revoked_migrate(env) -> str:
    """R1-004: bundle_revoked during a run -> at the next safe point the job holds with status waiting_human,
    the required offer is accepted by the validator, the requester approves, and the job migrates."""
    from harness import DEFAULT_BUILD
    target = "rb_" + "r" * 26
    world = FakeWorld(running_polls_by_role={"implementer": 6}, target_build_id=DEFAULT_BUILD)
    c = env.client
    job = new_job_id(74)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        await wait_for(lambda: _active_role(c, job, "implementer"), 60, "implementer running")
        await h.signal("release.notice", _notice("bundle_revoked", 7, current=BUNDLE0, deadline=_iso(3600)))
        await wait_for(lambda: _status(c, job, "waiting_human"), 120, "revoked hold (waiting_human)")
        offer = {"schema": "replicant.msg.offer-bundle-migration/v1", "action_id": "act_" + "4" * 26,
                 "actor": {"principal_id": ALICE}, "target_release_bundle_id": target, "reason": "revoked",
                 "required": True}
        r = await h.execute_update("job.offer_bundle_migration", offer, id=offer["action_id"])
        check(r["outcome"] == "gate_opened", f"offer {r}")
        mg = await open_gate(c, job, "bundle_migration")
        d = decide_req(mg, "approve", REQUESTER, "ui", 1)
        await h.execute_update("gate.decide", d, id=d["decision_id"])
        await wait_for(lambda: _status(c, job, "waiting_external"), 180, "pr_review after migration")
        check((await snapshot(c, job))["release_bundle_id"] == target, "migrated bundle")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        await common_checks("revoked_migrate", c, world, job)
    return "held, offered, migrated"


async def s_revoked_deadline(env) -> str:
    """R1-004: bundle_revoked while waiting at a gate, no migration before the deadline ->
    bundle_revoked_not_migrated (cancelled), with compensation and a terminal projection."""
    world = FakeWorld(risk={"plan": "high", "diff": "low"})
    c = env.client
    job = new_job_id(75)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        await open_gate(c, job, "plan_approval")
        await h.signal("release.notice", _notice("bundle_revoked", 8, current=BUNDLE0, deadline=_iso(8)))
        res = await h.result()
        check(res["outcome"] == "bundle_revoked_not_migrated" and res["status"] == "cancelled", f"result {res}")
        check(_events(world, job)[-1] == "job.terminal", "terminal projection")
        await common_checks("revoked_deadline", c, world, job)
    return "stopped at deadline"


async def s_job_max_age(env) -> str:
    """R2-044: repair_limits.job_max_age_days is a hard lifetime from creation (shortened for the test)."""
    world = FakeWorld(risk={"plan": "high", "diff": "low"},
                      extra_policies={"repair_limits": {"job_max_age_days": 10 / 86400}})
    c = env.client
    job = new_job_id(76)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        res = await h.result()
        check(res["outcome"] == "job_max_age_exceeded" and res["status"] == "expired", f"result {res}")
        await common_checks("job_max_age", c, world, job)
    return "expired at max age"


async def s_held_gate(env) -> str:
    """R2-023: a gate held by pause(at_next_gate) does not expire while held; on resume its expiry moves by the
    held duration, and the pauser was told the gate is held."""
    world = FakeWorld(risk={"plan": "high", "diff": "low"}, extra_policies={"gate_rules": {}})
    rules = world.bundle_core()["policies"]["gate_rules"]
    rules["gates"]["plan_approval"].update(reminder_seconds=[], escalation_seconds=[], expiry_seconds=6)
    world.extra_policies = {"gate_rules": rules}
    c = env.client
    job = new_job_id(77)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        req = {"schema": "replicant.msg.job-control/v1", "action_id": "act_" + "5" * 26, "action": "pause",
               "pause_mode": "at_next_gate", "actor": {"principal_id": ALICE}, "channel": "ui", "requested_at": _iso(0)}
        await h.execute_update("job.pause", req, id=req["action_id"])
        g = await open_gate(c, job, "plan_approval")
        await asyncio.sleep(10)  # beyond expiry_seconds
        g_now = await open_gate(c, job, "plan_approval")
        check(g_now["state"] == "open", "held gate did not expire")
        held = [a for a in world.called("notify.job_event") if a.get("kind") == "gate_held"]
        check(held and held[0]["to"] == ALICE, "pauser notified")
        res_req = {**req, "action_id": "act_" + "6" * 26, "action": "resume"}
        res_req.pop("pause_mode")
        await h.execute_update("job.resume", res_req, id=res_req["action_id"])
        async def moved():
            gg = await open_gate(c, job, "plan_approval")
            return gg.get("expires_at", "") > g.get("expires_at", "")
        await wait_for(moved, 30, "expiry moved")
        d = decide_req(await open_gate(c, job, "plan_approval"), "approve", ALICE, "ui", 1)
        await h.execute_update("gate.decide", d, id=d["decision_id"])
        await _approve_publish(c, h, job)
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        await common_checks("held_gate", c, world, job)
    return "expiry suspended while held"


async def s_late_decision(env) -> str:
    """R2-014: workers are down across the gate's expiry. On recovery the late timer opens a grace window and a
    decision that control-plane-api stamped before expires_at is accepted; one stamped after is rejected."""
    world = FakeWorld(risk={"plan": "high", "diff": "low"}, extra_policies={"gate_rules": {}})
    rules = world.bundle_core()["policies"]["gate_rules"]
    rules["gates"]["plan_approval"].update(reminder_seconds=[], escalation_seconds=[], expiry_seconds=20)
    world.extra_policies = {"gate_rules": rules}
    c = env.client
    job = new_job_id(78)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        g = await open_gate(c, job, "plan_approval")
        stamped = _iso(0)  # the click reaches control-plane-api before expiry; Temporal workers are down
    await asyncio.sleep(20 + 65)  # expiry passes; the timer fires late (> LATE_FIRE) when workers return
    async with workers(c, world):
        await wait_for(lambda: _has_event(world, job, "gate.expiry_grace"), 60, "grace window")
        late = {**decide_req(g, "approve", ALICE, "ui", 2), "decided_at": _iso(0)}
        try:
            await h.execute_update("gate.decide", late, id=late["decision_id"])
            raise AssertionError("decision stamped after expiry must be rejected")
        except WorkflowUpdateFailedError as e:
            check(e.cause.type == "GateClosed", f"late: {e.cause.type}")
        d = {**decide_req(g, "approve", ALICE, "ui", 1), "decided_at": stamped}
        r = await h.execute_update("gate.decide", d, id=d["decision_id"])
        check(r["outcome"] == "accepted", f"in-time decision {r}")
        await _approve_publish(c, h, job)
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        await common_checks("late_decision", c, world, job)
    return "accepted within grace"


async def _approve_publish(c, h, job, n: int = 90) -> None:
    pg = await open_gate(c, job, "publish_approval", 180)  # risk never decreases: plan-stage high persists
    d = decide_req(pg, "approve", ALICE, "ui", n)
    await h.execute_update("gate.decide", d, id=d["decision_id"])


async def _has_event(world, job, ev):
    return ev in _events(world, job)


async def s_principal_reroute(env) -> str:
    """R1-020: the requester (sole approver of a bundle_migration gate) is deprovisioned -> the gate is re-routed
    by activity and re-notified, tenant admins are notified, and the job pauses at its next node."""
    world = FakeWorld(risk={"plan": "high", "diff": "low"}, target_build_id="b20261024-000000000001")
    c = env.client
    job = new_job_id(79)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        await open_gate(c, job, "plan_approval")
        offer = {"schema": "replicant.msg.offer-bundle-migration/v1", "action_id": "act_" + "7" * 26,
                 "actor": {"principal_id": ALICE}, "target_release_bundle_id": "rb_" + "q" * 26, "reason": "fix"}
        await h.execute_update("job.offer_bundle_migration", offer, id=offer["action_id"])
        await open_gate(c, job, "bundle_migration")
        await h.signal("identity.principal_changed", {"schema": "replicant.msg.principal-changed/v1",
                       "notice_id": "ntc_" + "9" * 26, "principal_id": REQUESTER, "change": "deprovisioned",
                       "effective_at": _iso(0)})
        async def rerouted():
            return any(a["facts"].get("exclude_principals") == [REQUESTER] for a in world.called("gates.resolve_routing"))
        await wait_for(rerouted, 60, "gate re-routed")
        await wait_for(lambda: _has_event(world, job, "gate.rerouted"), 30, "projection")
        admins = [a for a in world.called("notify.job_event") if a.get("kind") == "requester_deprovisioned"]
        check(len(admins) == 1, "tenant admins notified")
        req = {"schema": "replicant.msg.job-control/v1", "action_id": "act_" + "8" * 26, "action": "cancel",
               "actor": {"principal_id": ALICE}, "channel": "ui", "requested_at": _iso(0)}
        await h.execute_update("job.cancel", req, id=req["action_id"])
        res = await h.result()
        check(res["status"] == "cancelled", f"result {res}")
        await common_checks("principal_reroute", c, world, job)
    return "re-routed"


async def s_batch_spend_cap(env) -> str:
    """R2-062: the batch stops starting children once cost.batch_spend reaches max_batch_usd; children already
    running finish; the batch projects JobSnapshots (R1-010) and its child IDs follow R1-011."""
    from replicant_worker.workflows.maintenance import BatchInput
    repos = [{"provider": "github", "host": "github.com", "owner": "acme-payments", "name": n, "repo_id": f"R_{i}"}
             for i, n in enumerate(["ledger-api", "billing-worker", "fx-rates", "risk-engine"])]
    world = FakeWorld(batch_targets=repos, batch_spend=[0.0, 150.0],
                      extra_policies={"repair_limits": {"maintenance": {"max_concurrency": 2, "max_batch_usd": 100}}})
    c = env.client
    parent = new_job_id(80)
    async with workers(c, world):
        jr = dict(__import__("harness").JOB_REQUEST)
        jr = {**jr, "workflow_type": "maintenance", "targets": [],
              "target_selector": {"catalog_query": {"languages": ["go"]}, "max_repos": 10}}
        h = await c.start_workflow("MaintenanceBatch", BatchInput(job_id=parent, tenant_id="payments",
                                   release_bundle_id=BUNDLE0, job_request=jr), id=parent, task_queue="replicant-jobs")
        async def two_in_review():
            snap = await h.query("batch.snapshot")
            return snap["children_by_status"].get("waiting_external", 0) == 2
        await wait_for(two_in_review, 240, "2 children in PR review")
        from replicant_worker import ids
        for i in range(2):
            cid = ids.derived("job", parent, str(i))
            await c.get_workflow_handle(cid).signal("pr.event", pr_event(cid, "pull_request_merged", 200 + i))
        res = await h.result()
        check(res.get("stopped_reason") == "batch_spend_cap" and res["children_total"] == 2, f"batch {res}")
        check(not world.invalid_projections, f"invalid projections {world.invalid_projections[:2]}")
        evs = [p["snapshot"]["phase"] for p in sorted(world.projections[parent], key=lambda p: p["seq"])]
        check(evs[0] == "fan_out" and evs[-1] == "done", f"batch phases {evs}")
        fails = await replay_all(await histories(c, parent))
        check(not fails, f"replay {fails}")
    return f"children={res['children_total']} stopped={res.get('stopped_reason')}"


async def s_migration_failure(env) -> str:
    """R2-015 / R1-007: the handoff activity fails -> the run (still on the source build) takes the frozen
    failure path: best-effort re-pin to the source build, bundle.migration_failed, operator alert, and the job
    continues at its open gate on the old bundle."""
    world = FakeWorld(risk={"plan": "high", "diff": "low"}, target_build_id="b20261024-000000000001")
    world.fatal["release.apply_migration_override"] = "InvalidInput"
    c = env.client
    job = new_job_id(85)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        g = await open_gate(c, job, "plan_approval")
        offer = {"schema": "replicant.msg.offer-bundle-migration/v1", "action_id": "act_" + "a" * 26,
                 "actor": {"principal_id": ALICE}, "target_release_bundle_id": "rb_" + "f" * 26, "reason": "fix"}
        await h.execute_update("job.offer_bundle_migration", offer, id=offer["action_id"])
        mg = await open_gate(c, job, "bundle_migration")
        d = decide_req(mg, "approve", REQUESTER, "ui", 1)
        await h.execute_update("gate.decide", d, id=d["decision_id"])
        await wait_for(lambda: _has_event(world, job, "bundle.migration_failed"), 90, "migration failure path")
        snap = await snapshot(c, job)
        check(snap["release_bundle_id"] == BUNDLE0, "stays on the old bundle")
        alerts = [a for a in world.called("notify.job_event") if a.get("kind") == "migration_failed"]
        check(len(alerts) == 1, "operator alert")
        g2 = await open_gate(c, job, "plan_approval")
        check(g2["gate_id"] == g["gate_id"], "same plan gate")
        d2 = decide_req(g2, "approve", ALICE, "ui", 2)
        await h.execute_update("gate.decide", d2, id=d2["decision_id"])
        await _approve_publish(c, h, job)
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        await common_checks("migration_failure", c, world, job)
    return "continued on N"


# -------------------------------------------------------------------------------------------- fix round wave 2
def _opr(n: int) -> str:
    return "opr_" + str(n) * 26


async def s_retry_node(env) -> str:
    """ops.retry_node (C-MSG 1.7.0, CCR-07-12): a stuck implementer run is cancelled and re-run with ordinal + 1;
    every failed check answers accepted=false with a reason."""
    world = FakeWorld(running_polls_by_role={"implementer": 1000})
    c = env.client
    job = new_job_id(90)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        await wait_for(lambda: _active_role(c, job, "implementer"), 60, "implementer running")
        mk = lambda n, key: {"schema": "replicant.msg.retry-node/v1", "operation_id": _opr(n), "node_key": key,
                             "reason": "stuck run", "actor": {"principal_id": ALICE}}
        for n, key, why in ((1, "nope", "node_unknown"), (2, "plan_gate", "not_allowed"), (3, "review", "not_failed")):
            r = await h.execute_update("ops.retry_node", mk(n, key), id=_opr(n))
            check(r == {"schema": "replicant.msg.retry-node-result/v1", "accepted": False, "denial_reason": why}, f"{key}: {r}")
        world.running_polls_by_role = {}
        r = await h.execute_update("ops.retry_node", mk(4, "implement"), id=_opr(4))
        check(r["accepted"] and r["new_ordinal"] == 2, f"retry {r}")
        await wait_for(lambda: _status(c, job, "waiting_external"), 180, "pr_review")
        impl = sorted(a["logical_activity_id"] for a in world.called("runner.start_run") if a["role"] == "implementer")
        check(impl == ["implement.c0.n1.run", "implement.c0.n2.run"], f"implementer runs {impl}")
        check(any(a["reason"] == "operator" for a in world.called("runner.cancel_run")), "cancelled with reason operator")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        await common_checks("retry_node", c, world, job)
    return "re-run with ordinal 2"


async def s_contested_reopen(env) -> str:
    """CCR-07-06 / XD-FIX-R1-173: the plan approval is contested before publish -> withdrawn, a new plan gate instance
    (same packet and digest) must be approved before publishing; decision_log and superseded events projected."""
    world = FakeWorld(risk={"plan": "high", "diff": "low"}, running_polls_by_role={"implementer": 8})
    c = env.client
    job = new_job_id(91)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        g = await open_gate(c, job, "plan_approval")
        d = decide_req(g, "approve", ALICE, "slack", 1)
        await h.execute_update("gate.decide", d, id=d["decision_id"])
        await h.signal("identity.principal_changed", {"schema": "replicant.msg.principal-changed/v1",
                       "notice_id": "ntc_" + "c" * 26, "principal_id": ALICE, "change": "decision_identity_contested",
                       "decision_id": d["decision_id"], "effective_at": _iso(0)})
        await _approve_publish(c, h, job)
        g2 = await open_gate(c, job, "plan_approval", 180)
        check(g2["gate_id"] != g["gate_id"] and g2["presented_digest"] == g["presented_digest"], "new instance, same digest")
        d2 = decide_req(g2, "approve", BOB, "ui", 2)
        await h.execute_update("gate.decide", d2, id=d2["decision_id"])
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        ev = _events(world, job)
        check("gate.decision_identity_contested" in ev and "gate.reopened" in ev, "contest projected")
        snap = await snapshot(c, job)
        logs = [e for gv in snap["gates"] for e in gv.get("decision_log", [])]
        check(any(e["decision_id"] == d["decision_id"] and e["outcome"] == "accepted" for e in logs), "decision_log")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        await common_checks("contested_reopen", c, world, job)
    return "re-approved before publish"


async def s_release_block(env) -> str:
    """CCR-06-13 / XD-FIX-R2-021: bundle_rolled_back blocks approve (V12) unless overridden; an operator-consent
    migration (consent=operator) moves the job without a gate and clears the block."""
    from harness import DEFAULT_BUILD
    world = FakeWorld(risk={"plan": "high", "diff": "low"}, target_build_id=DEFAULT_BUILD)
    c = env.client
    job = new_job_id(92)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        g = await open_gate(c, job, "plan_approval")
        await h.signal("release.notice", {**_notice("bundle_rolled_back", 7, current=BUNDLE0), "reason": "canary failed"})
        await wait_for(lambda: _has_event(world, job, "gate.release_blocked"), 60, "release block")
        d = decide_req(g, "approve", ALICE, "ui", 1)
        try:
            await h.execute_update("gate.decide", d, id=d["decision_id"])
            raise AssertionError("V12 must block")
        except WorkflowUpdateFailedError as e:
            check(e.cause.type == "GateBlockedByRelease", f"V12: {e.cause.type}")
        offer = {"schema": "replicant.msg.offer-bundle-migration/v1", "action_id": "act_" + "b" * 26,
                 "actor": {"principal_id": ALICE}, "target_release_bundle_id": "rb_" + "g" * 26, "reason": "rollback",
                 "consent": "operator"}
        r = await h.execute_update("job.offer_bundle_migration", offer, id=offer["action_id"])
        check(r["outcome"] == "migration_scheduled", f"offer {r}")
        async def migrated():
            s = await snapshot(c, job)
            gv = next((x for x in s["gates"] if x["gate_id"] == g["gate_id"]), {})
            return s["release_bundle_id"] == "rb_" + "g" * 26 and "release_block" not in gv
        await wait_for(migrated, 90, "migrated, block cleared")
        d2 = decide_req(await open_gate(c, job, "plan_approval"), "approve", ALICE, "ui", 2)
        r2 = await h.execute_update("gate.decide", d2, id=d2["decision_id"])
        check(r2["outcome"] == "accepted", f"approve after migration {r2}")
        await _approve_publish(c, h, job)
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        await common_checks("release_block", c, world, job)
    return "blocked, migrated by operator, approved"


async def s_rerun_qa(env) -> str:
    """CCR-05-04 / XD-FIX-R2-027: QA inconclusive -> publish gate -> rerun_qa -> QA again with trigger env_retry ->
    pass -> new publish gate -> approve; the operator alert uses notify.operator (XD-W2-05-05)."""
    world = FakeWorld(qa_verdicts=["inconclusive", "inconclusive", "pass"])
    c = env.client
    job = new_job_id(93)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        pg = await open_gate(c, job, "publish_approval", 180)
        d = decide_req(pg, "rerun_qa", ALICE, "ui", 1)
        r = await h.execute_update("gate.decide", d, id=d["decision_id"])
        check(r["outcome"] == "accepted", f"rerun_qa {r}")
        async def second_gate():
            gg = await open_gate(c, job, "publish_approval", 5)
            return gg if gg["gate_id"] != pg["gate_id"] else None
        pg2 = await wait_for(second_gate, 180, "second publish gate")
        d2 = decide_req(pg2, "rerun_qa", ALICE, "ui", 2)
        try:
            await h.execute_update("gate.decide", d2, id=d2["decision_id"])
            raise AssertionError("rerun_qa must not be offered after a passing QA")
        except WorkflowUpdateFailedError as e:
            check(e.cause.type == "DecisionNotOffered", f"V5: {e.cause.type}")
        d3 = decide_req(pg2, "approve", ALICE, "ui", 3)
        await h.execute_update("gate.decide", d3, id=d3["decision_id"])
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        staged = [a.get("extra", {}).get("trigger") for a in world.called("context.stage_inputs") if a["role"] == "qa"]
        check("env_retry" in staged, f"QA re-run trigger: {staged}")
        check(world.called("notify.operator"), "operator alert")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        await common_checks("rerun_qa", c, world, job)
    return "QA re-run from the gate"


async def s_capacity_wait(env) -> str:
    """XD-FIX-R2-020 / XD-W2-02-04: 429 tenant quota -> waiting tenant_run_quota (durable timer, same logical ID);
    a run Pending with Scheduled=False for > 60 s -> waiting cluster_capacity; both cleared when work proceeds."""
    world = FakeWorld(capacity=[{"reason": "tenant_run_quota", "quota_used": 12, "quota_limit": 12,
                                 "retry_after_seconds": 2}] * 2,
                      unscheduled_polls_by_role={"planner": 40})
    c = env.client
    job = new_job_id(94)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        await wait_for(lambda: _status(c, job, "waiting_external"), 300, "pr_review")
        reasons = [p["snapshot"].get("waiting", {}).get("reason") for p in sorted(world.projections[job], key=lambda p: p["seq"])]
        check("tenant_run_quota" in reasons and "cluster_capacity" in reasons, f"waiting reasons {set(reasons)}")
        check(reasons[-1] is None, "waiting cleared")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        await common_checks("capacity_wait", c, world, job)
    return "quota and cluster waits projected"


async def s_takeover_critical(env) -> str:
    """XD-FIX-R2-001: a critical class on the takeover diff opens a publish_approval gate for the requester before the
    push to replicant-takeover/<job_id>; a Publisher rejection (OB-1 unattested) yields archive + patch only."""
    out = []
    for n, reject in ((95, False), (96, True)):
        world = FakeWorld(risk={"takeover": "critical"}, running_polls_by_role={"implementer": 1000},
                          takeover_push_reject=reject)
        c = env.client
        job = new_job_id(n)
        async with workers(c, world):
            h = await start_job(c, job, workflow_type="bugfix")
            await wait_for(lambda: _active_role(c, job, "implementer"), 60, "implementer running")
            req = {"schema": "replicant.msg.job-control/v1", "action_id": f"act_{str(n % 10) * 26}", "action": "takeover",
                   "actor": {"principal_id": ALICE}, "channel": "cli", "requested_at": _iso(0)}
            await h.execute_update("job.takeover", req, id=req["action_id"])
            g = await open_gate(c, job, "publish_approval", 120)
            d = decide_req(g, "approve", REQUESTER, "ui", 1)
            await h.execute_update("gate.decide", d, id=d["decision_id"])
            res = await h.result()
            check(res["status"] == "taken_over", f"result {res}")
            pkg = world.called("takeover.assemble_package")[0]
            if reject:
                check(pkg["branch"] is None and "branch" not in pkg["include"], f"archive only {pkg}")
            else:
                check(pkg["branch"] == f"replicant-takeover/{job}", f"branch {pkg['branch']}")
            await common_checks(f"takeover_critical_{n}", c, world, job)
            out.append("rejected" if reject else "pushed")
    return ",".join(out)


async def s_plan_reuse(env) -> str:
    """XD-FIX-R2-050: a job superseding an expired job reuses its approved plan: no planner run, no plan gate."""
    world = FakeWorld(risk={"plan": "high", "diff": "low"},
                      superseded_plan={"plan_uri": "s3://local-replicant-local-artifacts/tenants/payments/jobs/j/plan/plan.json",
                                       "plan_approved": True})
    c = env.client
    job = new_job_id(97)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix", related={"supersedes_job_id": new_job_id(96)})
        await _approve_publish(c, h, job)
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        check(not [r for r in world.runs.values() if r.role == "planner"], "no planner run")
        snap = await snapshot(c, job)
        check(not [g for g in snap["gates"] if g["kind"] == "plan_approval"], "no plan gate")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        await common_checks("plan_reuse", c, world, job)
    return "planning skipped"


async def s_harness_task(env) -> str:
    """XD-FIX-R1-134: options.evaluation.harness_task skips router and graph and runs the harness steps with
    role eval through the normal Runner-call sequence; the job succeeds when all steps ran."""
    world = FakeWorld(harness_steps=[
        {"node_key": "golden_snapshot", "ordinal": 1, "agent": "eval-snapshotter", "environment": "go-service",
         "network_profile": "eval-snapshot"},
        {"node_key": "golden_snapshot", "ordinal": 2, "agent": "eval-scorer", "environment": "go-service",
         "network_profile": "eval-scoring"}])
    c = env.client
    job = new_job_id(98)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="qa_only",
                            options={"evaluation": {"mode": "eval", "gate_script_uri": "s3://local-replicant-local-eval/gs/1.json",
                                                    "harness_task": {"kind": "snapshot_build",
                                                                     "spec_uri": "s3://local-replicant-local-eval/h/1.json"}}})
        res = await h.result()
        check(res["outcome"] == "harness_completed" and res["status"] == "succeeded", f"result {res}")
        lids = [a["logical_activity_id"] for a in world.called("runner.start_run")]
        check(lids == ["golden_snapshot.c0.n1.run", "golden_snapshot.c0.n2.run"], f"runs {lids}")
        check(all(a["role"] == "eval" for a in world.called("runner.start_run")), "role eval")
        check(not world.called("router.classify") and not world.called("graph.compose"), "no router, no graph")
        await common_checks("harness_task", c, world, job)
    return "2 harness steps"


async def s_onboarding(env) -> str:
    """Design §8c: a readiness job (label replicant-readiness, pinned repo_profile) composes with the profile, plans
    with the repo's commands and the tier facts, records the QA run's observations and writes readiness back; a job
    without a profile calls none of it."""
    profile = {"profile_id": "rp_01", "sha": "a" * 40, "environment": "python-service",
               "commands": {"install": "uv sync --frozen", "build": None, "test": "uv run pytest -q", "lint": None},
               "repo_config": "use", "config_base_sha": "a" * 40, "mcp_auto_allow": [], "readiness": "unknown"}
    c = env.client
    w1 = FakeWorld()
    j1 = new_job_id(77)
    async with workers(c, w1):
        r1 = await (await start_job(c, j1, workflow_type="qa_only", labels=["replicant-readiness"],
                                    repo_profile=profile)).result()
        check(r1["outcome"] == "qa_completed" and r1["status"] == "succeeded", f"readiness {r1}")
        comp = w1.called("graph.compose")[0]
        check(comp.get("repo_profile") == profile and comp["repo_ref"]["repo_id"], f"compose input {comp}")
        plans = w1.called("agents.build_run_plan")
        check(all(p.get("repo_profile") == profile and p.get("repo") and p.get("tier_facts") == {
            "repair_cycle": 0, "risk": "low", "failed_attempt": False} for p in plans), f"plan inputs {plans[:1]}")
        obs = w1.called("inventory.record_observations")
        check(len(obs) == 1 and obs[0]["role"] == "qa" and obs[0]["readiness"] is True and obs[0]["result_uri"],
              f"observations {obs}")
        rd = w1.called("inventory.record_readiness")
        check(len(rd) == 1 and rd[0]["verdict"] == "pass" and rd[0]["profile_id"] == "rp_01"
              and rd[0]["outcome"] == "qa_completed" and rd[0]["qa_result_uri"], f"readiness {rd}")
        await common_checks("onboarding_readiness", c, w1, j1)
    w2 = FakeWorld()
    j2 = new_job_id(76)
    async with workers(c, w2):
        r2 = await (await start_job(c, j2, workflow_type="qa_only")).result()
        check(r2["outcome"] == "qa_completed", f"plain qa_only {r2}")
        check("repo_profile" not in w2.called("graph.compose")[0], "no profile, no profile in compose")
        check(not any("repo_profile" in p for p in w2.called("agents.build_run_plan")), "no profile in plans")
        check(not w2.called("inventory.record_observations") and not w2.called("inventory.record_readiness"),
              "no inventory calls without a profile")
        await common_checks("onboarding_plain", c, w2, j2)
    return "readiness recorded; no profile, no inventory calls"


async def s_small_rules(env) -> str:
    """Cheap wave-2 rules in one scenario: XD-FIX-R2-064 compute dollars (budget_exceeded) and XD-FIX-R1-131 qa_only
    inconclusive -> end:qa_inconclusive (failed)."""
    c = env.client
    w1 = FakeWorld(compute_limit=5.0, compute_usd=10.0)
    j1 = new_job_id(99)
    async with workers(c, w1):
        r1 = await (await start_job(c, j1, workflow_type="bugfix")).result()
        check(r1["outcome"] == "budget_exceeded" and r1["status"] == "failed", f"compute {r1}")
        await common_checks("small_rules_compute", c, w1, j1)
    w2 = FakeWorld(qa_verdicts=["inconclusive", "inconclusive"])
    j2 = new_job_id(89)
    async with workers(c, w2):
        r2 = await (await start_job(c, j2, workflow_type="qa_only")).result()
        check(r2["outcome"] == "qa_inconclusive" and r2["status"] == "failed", f"qa_only {r2}")
        check(w2.called("notify.operator"), "qa_only env alert")
        await common_checks("small_rules_qa_only", c, w2, j2)
    # R1V-009 / R1V-015: tenant-period budget wait -> waiting.reason tenant_budget_exhausted (not waiting_human),
    # quota re-checked with raise_if_exhausted=false, then the run restarts
    w3 = FakeWorld(failures={"implementer": ["GatewayBudgetExceeded"]},
                   failure_messages={"implementer": "tenant_period: October budget exhausted"}, quota_available=[False, True],
                   extra_policies={"repair_limits": {"budget": {"extension_gate": True, "tenant_quota_check_seconds": 2}}})
    j3 = new_job_id(88)
    async with workers(c, w3):
        h3 = await start_job(c, j3, workflow_type="bugfix")
        await wait_for(lambda: _status(c, j3, "waiting_external"), 180, "pr_review")
        snaps = [p["snapshot"] for p in sorted(w3.projections[j3], key=lambda p: p["seq"])]
        check(any((x.get("waiting") or {}).get("reason") == "tenant_budget_exhausted" for x in snaps), "budget wait projected")
        check(not any(x["status"] == "waiting_human" and x["phase"] == "budget_gate" for x in snaps), "not waiting_human")
        check(all(a.get("raise_if_exhausted") is False for a in w3.called("budget.tenant_quota_status")), "raise_if_exhausted")
        await h3.signal("pr.event", pr_event(j3, "pull_request_merged", 1))
        r3 = await h3.result()
        check(r3["outcome"] == "pr_merged", f"budget wait {r3}")
        await common_checks("small_rules_budget_wait", c, w3, j3)
    return "budget_exceeded, qa_inconclusive, tenant_budget_exhausted"


async def s_maintenance_shards(env) -> str:
    """XD-W3-R1V-006 (CCR-00-01): above shard_size repositories the batch fans out MaintenanceShard children
    (`<job_id>:shard:<n>`, REQUEST_CANCEL) that create the child jobs (ABANDON) from the S3 repository list and
    receive their batch.child_progress Signals; the batch aggregates shard results. shard_size is lowered to 2."""
    from replicant_worker.workflows.maintenance import BatchInput
    repos = [{"provider": "github", "host": "github.com", "owner": "acme-payments", "name": f"svc-{i}", "repo_id": f"R_s{i}"}
             for i in range(5)]
    world = FakeWorld(batch_targets=repos,
                      extra_policies={"repair_limits": {"maintenance": {"max_concurrency": 6, "shard_size": 2}}})
    c = env.client
    parent = new_job_id(87)
    async with workers(c, world):
        jr = dict(__import__("harness").JOB_REQUEST)
        jr = {**jr, "workflow_type": "maintenance", "targets": [],
              "target_selector": {"catalog_query": {"languages": ["go"]}, "max_repos": 15000}}
        h = await c.start_workflow("MaintenanceBatch", BatchInput(job_id=parent, tenant_id="payments",
                                   release_bundle_id=BUNDLE0, job_request=jr), id=parent, task_queue="replicant-jobs")
        from replicant_worker import ids
        kids = [ids.derived("job", parent, str(i)) for i in range(5)]
        async def all_in_review():
            for cid in kids:
                if (await snapshot(c, cid))["status"] != "waiting_external":
                    return False
            return True
        await wait_for(all_in_review, 300, "5 children in PR review")
        for i, cid in enumerate(kids):
            await c.get_workflow_handle(cid).signal("pr.event", pr_event(cid, "pull_request_merged", 300 + i))
        res = await h.result()
        check(res["children_by_status"] == {"succeeded": 5} and res["shards"] == 3, f"batch {res}")
        shard_ids = [w.id async for w in c.list_workflows("WorkflowType = 'MaintenanceShard'")]
        check(sorted(shard_ids) == [f"{parent}:shard:{n}" for n in (1, 2, 3)], f"shards {shard_ids}")
        check(not world.invalid_projections and not world.contract_errors, "projections and peer schemas")
        hists = await histories(c, parent)
        for sid in shard_ids:
            hists += await histories(c, sid)
        fails = await replay_all(hists)
        check(not fails, f"replay {fails}")
    return "3 shards, 5 children"


# ---- fast-retry group: run with REPLICANT_TEST_FAST_RETRY=1 (activity retries exhaust in under a second)
async def s_dependency_outage(env) -> str:
    """R2-010: context.stage_inputs is down for 8 s: retries exhaust, the job holds `dependency` (status paused),
    re-probes with jittered backoff and completes; no execution failure."""
    from replicant_worker import build_config
    if not build_config.FAST_RETRY:
        raise Skipped("run with REPLICANT_TEST_FAST_RETRY=1")
    world = FakeWorld(extra_policies={"repair_limits": FAST_BLOCK})
    c = env.client
    job = new_job_id(81)
    async with workers(c, world):
        world.outages["context.stage_inputs"] = asyncio.get_running_loop().time() + 8
        h = await start_job(c, job, workflow_type="bugfix")
        await wait_for(lambda: _status(c, job, "paused"), 60, "dependency hold")
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        ev = _events(world, job)
        check("job.blocked_on_dependency" in ev and "job.released" in ev, "hold projected")
        await common_checks("dependency_outage", c, world, job)
    return "held then resumed"


async def s_dependency_giveup(env) -> str:
    """R2-010: the outage outlives dependency_block.max_hours -> outcome dependency_unavailable (failed), with
    _finish (terminal projection) rather than a failed execution. Also: a non-retryable activity error ends
    the job node_failed with a terminal projection (R1-006)."""
    from replicant_worker import build_config
    if not build_config.FAST_RETRY:
        raise Skipped("run with REPLICANT_TEST_FAST_RETRY=1")
    world = FakeWorld(extra_policies={"repair_limits": {"dependency_block": {**FAST_BLOCK["dependency_block"],
                                                                            "max_hours": 5 / 3600}}})
    c = env.client
    job, job2 = new_job_id(82), new_job_id(83)
    async with workers(c, world):
        world.outages["context.stage_inputs"] = asyncio.get_running_loop().time() + 600
        h = await start_job(c, job, workflow_type="bugfix")
        res = await h.result()
        check(res["outcome"] == "dependency_unavailable" and res["status"] == "failed", f"result {res}")
        check(_events(world, job)[-1] == "job.terminal", "terminal projection")
        world.outages.clear()
        world.fatal["verifier.check"] = "SchemaViolation"
        h2 = await start_job(c, job2, workflow_type="bugfix")
        res2 = await h2.result()
        check(res2["outcome"] == "node_failed" and res2["status"] == "failed", f"result {res2}")
        desc = await h2.describe()
        check(desc.status.name == "COMPLETED", f"execution status {desc.status}")
        await common_checks("dependency_giveup", c, world, job2)
    return "gave up cleanly"


async def s_identity_outage(env) -> str:
    """R2-011: identity-service down while an approver clicks: the Update completes with denied /
    identity_unverifiable (never a failed Update); a later click (new decision_id) is accepted."""
    from replicant_worker import build_config
    if not build_config.FAST_RETRY:
        raise Skipped("run with REPLICANT_TEST_FAST_RETRY=1")
    world = FakeWorld(risk={"plan": "high", "diff": "low"})
    c = env.client
    job = new_job_id(84)
    async with workers(c, world):
        h = await start_job(c, job, workflow_type="bugfix")
        g = await open_gate(c, job, "plan_approval")
        world.outages["identity.check_decision"] = asyncio.get_running_loop().time() + 600
        d = decide_req(g, "approve", ALICE, "ui", 1)
        r = await h.execute_update("gate.decide", d, id=d["decision_id"])
        check(r["outcome"] == "denied" and r["denial_reason"] == "identity_unverifiable", f"denied {r}")
        world.outages.clear()
        d2 = decide_req(g, "approve", ALICE, "ui", 2)
        r2 = await h.execute_update("gate.decide", d2, id=d2["decision_id"])
        check(r2["outcome"] == "accepted", f"retry {r2}")
        await _approve_publish(c, h, job)
        await wait_for(lambda: _status(c, job, "waiting_external"), 120, "pr_review")
        await h.signal("pr.event", pr_event(job, "pull_request_merged", 1))
        res = await h.result()
        check(res["outcome"] == "pr_merged", f"result {res}")
        await common_checks("identity_outage", c, world, job)
    return "denied, then accepted"


SCENARIOS = {f.__name__[2:]: f for f in [s_onboarding, s_bugfix_full, s_feature_parallel, s_low_risk_rebase, s_cancel_during_run,
                                         s_gate_expiry, s_router_clarify, s_repair_budget, s_takeover, s_steer,
                                         s_eval_mode, s_budget_extension, s_qa_environment, s_security_violation,
                                         s_migration, s_maintenance_batch, s_tenant_codec,
                                         s_license_prestep, s_history_budget,
                                         s_mid_run_can, s_gateway_outage, s_kill_switch, s_model_retiring,
                                         s_bundle_patched, s_revoked_migrate, s_revoked_deadline, s_job_max_age,
                                         s_held_gate, s_late_decision, s_principal_reroute, s_batch_spend_cap,
                                         s_migration_failure, s_retry_node, s_contested_reopen, s_release_block, s_rerun_qa,
                                         s_capacity_wait, s_takeover_critical, s_plan_reuse, s_harness_task, s_small_rules, s_maintenance_shards,
                                         s_dependency_outage, s_dependency_giveup, s_identity_outage]}


async def main(argv: list[str]) -> int:
    names = argv or list(SCENARIOS)
    env = await start_env()
    failed = 0
    try:
        for n in names:
            try:
                detail = await asyncio.wait_for(SCENARIOS[n](env), 1500 if n == "history_budget" else 300)
                print(f"PASS {n}: {detail}", flush=True)
            except Skipped as e:
                print(f"SKIP {n}: {e}", flush=True)
            except Exception as e:
                failed += 1
                print(f"FAIL {n}: {e!r}", flush=True)
                traceback.print_exc()
    finally:
        await env.shutdown()
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))


# ---- pytest entry point: one test per scenario on a session-scoped Temporal dev server.
# `replicant dev test unit` runs the FAST subset; the rest are marked `slow` (pytest -m slow, or `--slow`).
# A scenario that raises Skipped reports SKIP, never PASS.
import pytest  # noqa: E402

FAST = ("bugfix_full", "cancel_during_run", "eval_mode", "repair_budget", "migration", "onboarding")


def _dev_servers() -> set[int]:
    """PIDs of `temporal server start-dev` processes that are children of this pytest process."""
    import subprocess
    r = subprocess.run(["pgrep", "-P", str(os.getpid()), "-f", "server start-dev"], capture_output=True, text=True)
    return {int(x) for x in r.stdout.split()}


def _interrupt(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")  # pytest then runs session teardown (stops the dev server)


@pytest.fixture(scope="session")
def scenario_env():
    """One Temporal dev server for every scenario. Stopped at the end of the session, also when pytest is
    terminated (SIGTERM/SIGHUP become KeyboardInterrupt) and when env.shutdown() fails: a dev server this fixture
    started never outlives the session."""
    import signal
    import time as _time
    before = _dev_servers()
    prev = {s: signal.signal(s, _interrupt) for s in (signal.SIGTERM, signal.SIGHUP)}
    loop = asyncio.new_event_loop()
    env, mine = None, set()
    try:
        env = loop.run_until_complete(start_env())
        mine = _dev_servers() - before
        yield loop, env
    finally:
        try:
            if env is not None:
                loop.run_until_complete(asyncio.wait_for(env.shutdown(), 60))
        except BaseException:  # noqa: BLE001  (fall through to the kill below)
            pass
        finally:
            mine |= _dev_servers() - before
            for sig in (signal.SIGTERM, signal.SIGKILL):
                alive = {p for p in mine if _alive(p)}
                for pid in alive:
                    os.kill(pid, sig)
                deadline = _time.monotonic() + 10
                while alive and _time.monotonic() < deadline:
                    _time.sleep(0.2)
                    alive = {p for p in alive if _alive(p)}
                if not alive:
                    break
            loop.close()
            for s, h in prev.items():
                signal.signal(s, h)


def _alive(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)  # reap if it is our zombie
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.parametrize("name", [pytest.param(n, marks=() if n in FAST else pytest.mark.slow, id=n)
                                  for n in SCENARIOS])
def test_scenario(scenario_env, name: str) -> None:
    loop, env = scenario_env
    try:
        loop.run_until_complete(asyncio.wait_for(SCENARIOS[name](env), 1500 if name == "history_budget" else 300))
    except Skipped as e:
        pytest.skip(str(e))
