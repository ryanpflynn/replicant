"""ReplicantJob: harness jobs and the router prologue (router.md, eval-harness.md §3.8).

A mixin of ReplicantJobBase (workflows/replicant_job.py); it runs in workflow code under the sandbox."""

from __future__ import annotations

from typing import Any

from temporalio import workflow

from replicant_worker import ids
from replicant_worker.registry import NodeFailed
from replicant_worker.workflows.context import NodeContext
from replicant_worker.workflows.job.common import SA_REPOS, SA_TYPE, _PrologueInterrupt


class PrologueMixin:
    # ============================================================ harness jobs (XD-FIX-R1-134, eval-harness.md §3.8)
    async def _harness(self, ht: dict[str, Any]) -> tuple[str, str | None]:
        """Run the harness steps of options.evaluation.harness_task in order: wait (up to 15 min) for the
        inputs manifest eval-worker staged at the precomputed run prefix, then the normal Runner-call sequence
        (grant signed in core) with role `eval`. A step's run outcome never fails the job; only infrastructure
        errors do (NodeFailed from the activity wrapper)."""
        s = self.s
        if s.workflow_type == "unrouted":
            s.workflow_type = s.request_summary.get("workflow_type") or "qa_only"
            self._queue_sa(SA_TYPE.value_set(s.workflow_type))
        self._set_status("running", "qa")
        hctx = NodeContext(self, "harness", 1, lambda p: (_ for _ in ()).throw(RuntimeError("harness steps never wait")))
        for i, t in enumerate(s.targets):
            if not t.get("base_sha"):
                r = await hctx.act("scm.resolve_base", {"repo": t["repo_ref"], "base_ref": t.get("base_ref")},
                                   effect=f"resolve_base_{i}")
                t["base_ref"], t["base_sha"] = r["base_ref"], r["base_sha"]
        spec = await self.call("eval.read_harness_spec", {"job_id": s.job_id, "tenant_id": s.tenant_id,
                               "kind": ht["kind"], "spec_uri": ht["spec_uri"]})
        done = 0
        for step in spec["steps"]:
            ctx = NodeContext(self, step["node_key"], int(step["ordinal"]), hctx._interrupt_fn)
            lid = ctx.lid("run")
            run_id = ids.run_id_for(s.tenant_id, ids.idempotency_key(workflow.info().workflow_id, lid))
            await self.call("eval.await_inputs", {"job_id": s.job_id, "tenant_id": s.tenant_id, "run_id": run_id,
                            "timeout_seconds": 900})
            s.active_nodes[step["node_key"]] = {"ordinal": int(step["ordinal"]), "started_at": workflow.now().isoformat()}
            self.emit("node.started", f"harness step {step['node_key']} n{step['ordinal']}", node_key=step["node_key"])
            out = await ctx.run_agent(role="eval", agent=step["agent"], environment=step["environment"],
                                      workload_class=step.get("workload_class", "utility"),
                                      network_profile=step["network_profile"], inputs={"harness_inputs": True},
                                      raise_on_failure=False)
            s.active_nodes.pop(step["node_key"], None)
            s.exports[f"{step['node_key']}_n{step['ordinal']}"] = {"outcome": out.outcome or out.phase, "run_id": out.run_id}
            self.emit("node.completed", f"harness step {step['node_key']}: {out.outcome or out.phase}",
                      node_key=step["node_key"])
            done += 1
        return ("harness_completed", f"{done} harness steps ran")

    # ============================================================ router prologue (router.md)
    async def _route(self) -> None:
        def interrupt_fn(payload: dict[str, Any]) -> Any:
            if self._prologue_resume is not None:
                v, self._prologue_resume = self._prologue_resume, None
                return v
            raise _PrologueInterrupt({**payload, "node_key": "router"})

        ctx = NodeContext(self, "router", 1, interrupt_fn)
        req = dict(self.s.request or {})
        decision = await ctx.act("router.classify", {"request": req, "router_rules": self.policy("router_rules")},
                                 effect="classify")
        attempt = 0
        while decision.get("needs_intent"):
            attempt += 1
            ans = self.s.clarification_answer
            try:  # router.md: the model being unavailable never blocks or fails intake; use the fallback type
                intent = await ctx.act("router.parse_intent", {
                    "request_id": req.get("request_id"), "candidates": decision["candidates"], "attempt": attempt,
                    # R1-017: the requester's answer travels in the activity input (<= 16 KiB). It is untrusted
                    # text: the activity puts it only inside a delimited untrusted-input block (G-24).
                    "clarification_answer": {"decision_id": ans["decision_id"], "text": ans["text"],
                                             "trust": "untrusted"} if ans else None},
                    effect="intent", block=False)
            except NodeFailed:
                intent = {"workflow_type": decision["fallback"], "confidence": 0.0}
                decision = {**decision, "workflow_type": decision["fallback"], "method": "fallback",
                            "needs_intent": False, "rules_fired": [*decision.get("rules_fired", []), "llm_unavailable"]}
                break
            thr = self.policy("router_rules")["thresholds"]
            if intent["confidence"] >= thr["accept"] and intent["workflow_type"] in decision["candidates"]:
                decision = {**decision, "workflow_type": intent["workflow_type"], "method": "llm",
                            "confidence": intent["confidence"], "needs_intent": False}
            elif attempt > int(thr.get("max_clarifications", 2)):
                decision = {**decision, "workflow_type": decision["fallback"], "method": "fallback", "needs_intent": False}
            else:
                self._set_status(self.s.status, "clarification")  # C-IDS phase (R1-023)
                d = await ctx.gate("clarification", packet={"questions": intent.get("questions", [])})
                if d.get("decision") != "answer":
                    raise NodeFailed("clarification_rejected", "requester declined to clarify")
                self._set_status("running", "routing")
                decision = {**decision, "clarification_answer_ref": d.get("answer_ref")}
                ctx.ordinal += 1  # next intent parse and gate are new logical steps
        wf_type = decision["workflow_type"]
        for t in self.s.targets:
            if not t.get("base_sha"):
                r = await ctx.act("scm.resolve_base", {"repo": t["repo_ref"], "base_ref": t.get("base_ref")},
                                  effect=f"resolve_base_{self.s.targets.index(t)}")
                t["base_ref"], t["base_sha"] = r["base_ref"], r["base_sha"]
        self.s.workflow_type = wf_type
        pr0 = self.s.targets[0].get("pull_request") if self.s.targets else None
        if wf_type == "pr_repair" and pr0:  # standalone repair on an existing PR (ADR-009)
            self.s.pr = {"repo": self.s.targets[0]["repo"], "number": pr0["number"], "head_ref": pr0.get("head_ref"),
                         "head_sha": pr0.get("head_sha"), "state": "open",
                         "url": f"https://{self.s.targets[0]['repo']}/pull/{pr0['number']}"}
        rl = self.policy("repair_limits")
        self.s.repair_max = int(rl["repair_cycles"].get("per_workflow_type", {}).get(wf_type, rl["repair_cycles"]["default"]))
        self.s.exports["router"] = {"outcome": wf_type, "method": decision.get("method", "rule"),
                                    "confidence": decision.get("confidence", 1.0),
                                    "rules_fired": list(decision.get("rules_fired", []))[:20]}
        self.s.memo.clear()
        self.s.clarification_answer = None  # untrusted text leaves the carry once routing is done
        self._queue_sa(SA_TYPE.value_set(wf_type), SA_REPOS.value_set([t["repo"] for t in self.s.targets]))
        self.emit("job.routed", f"routed to {wf_type} ({decision.get('method', 'rule')})")
