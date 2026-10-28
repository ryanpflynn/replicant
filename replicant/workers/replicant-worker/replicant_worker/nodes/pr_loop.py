"""PR review loop node types (D-15): wait for PR feedback, triage it, reply on the PR, write memory candidates.

Shipped in every replicant-worker build beside nodes/core.py (imported by nodes/__init__.py). Activities and their
logical-ID effects follow graphs/README.md section 2.
"""

from __future__ import annotations

from typing import Any

from replicant_worker.registry import NodeResult, NodeType, node_type
from replicant_worker.workflows.context import NodeContext


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
        if not authorized or all(b.get("event") == "replicant_command" for b in authorized):
            # command-only comments (`/replicant no-memory|remember`) never start a repair cycle on their own; they
            # apply to comments of the same batch (feedback.triage)
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
                                            "repair_budget": {"used": s.repair_used, "max": s.repair_max},
                                            "qa_verdict": s.last_qa_verdict,
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
            # MEM-01: false when this cycle's repair was not pushed (failed repair, no_action, rejected push gate)
            **({"addressed": bool(s.exports["__feedback__"].get("pushed"))}
               if isinstance(s.exports.get("__feedback__"), dict) and s.refs.get("feedback_triage_uri") else {}),
            "memory_snapshot_id": s.memory_snapshot_id}, effect="extract")
        r = await ctx.act("memory.write_candidates", {  # extract returns {candidates_uri, sha256, ...} (XD-FIX-R1-100)
            "job_id": s.job_id, "tenant_id": s.tenant_id, "cycle": s.cycle,
            "extracted_uri": ex.get("candidates_uri") or ex.get("uri"), "extracted_sha256": ex.get("sha256"),
            "memory_snapshot_id": s.memory_snapshot_id}, effect="write_candidates")
        ctx.host.emit("memory.candidates_written", f"{r.get('written', 0)} memory candidates", node_key=ctx.node_key)
        return NodeResult(outcome="written", exports={"written": r.get("written", 0)})
