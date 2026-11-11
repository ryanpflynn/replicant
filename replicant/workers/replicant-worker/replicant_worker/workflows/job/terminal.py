"""ReplicantJob: compensation (cancel, takeover, failure) and the terminal transition.

A mixin of ReplicantJobBase (workflows/replicant_job.py); it runs in workflow code under the sandbox."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow

from replicant_worker import ids
from replicant_worker.registry import NodeFailed
from replicant_worker.workflows.job.common import READINESS_LABEL, _Can
from replicant_worker.workflows.state import TERMINAL_STATUS_FOR_OUTCOME, GateRecord


class TerminalMixin:
    # ============================================================ compensation and terminal
    async def _compensate(self) -> tuple[str, str | None]:
        s = self.s
        reason = "taken_over" if self._takeover else (s.forced_outcome or "cancelled_by_user")
        why = "takeover" if self._takeover else "job_cancelled"
        if self._takeover:
            self._set_status("running", "takeover")  # C-IDS phase (R1-023)
        cancelled = [r for r in s.runs.values() if r.active]
        for r in cancelled:
            await self._aux_act("runner.cancel_run", {"run_id": r.run_id, "reason": why},
                                node_key=r.node_key, effect="cancel", ordinal=r.ordinal, cycle=r.cycle)
            r.active = False
        if self._takeover:
            # C-MSG job.takeover (R1-008): wait until each cancelled run's finalize step has uploaded the
            # workspace archive and diff, bounded by finalize_seconds + 300 s, then publish that diff.
            diff_uri = s.refs.get("latest_diff_bundle_uri")
            for r in cancelled:
                deadline = workflow.now() + timedelta(seconds=r.finalize_seconds + 300)
                st: dict[str, Any] = {"phase": r.phase}
                while workflow.now() < deadline:
                    st = await self.call("runner.get_run", {"run_id": r.run_id}, block=False)
                    if st["phase"] in ("Succeeded", "Failed", "Cancelled"):
                        break
                    await workflow.sleep(timedelta(seconds=10))
                r.phase = st["phase"]
                if st.get("result_uri"):
                    res = await self.call("runner.read_result", {"run_id": r.run_id, "result_uri": st["result_uri"],
                                          "manifest_uri": st.get("artifact_manifest_uri")}, block=False)
                    for k, ref in (("diff_bundle", "latest_diff_bundle_uri"), ("handoff", "latest_handoff_uri")):
                        if res.get("outputs", {}).get(k):
                            s.refs[ref] = res["outputs"][k]
                    diff_uri = s.refs.get("latest_diff_bundle_uri")
            tko = self._takeover["takeover_id"]
            include = list(self._takeover["include"])
            pushed = await self._takeover_push(diff_uri) if "branch" in include else False
            if not pushed and "branch" in include:  # archive + patch instead of a pushed branch (XD-FIX-R2-001)
                include = [i for i in include if i != "branch"] + [i for i in ("workspace_archive",) if i not in include]
            pkg = await self._aux_act("takeover.assemble_package", {"job_id": s.job_id, "takeover_id": tko,
                                      "include": include, "refs": dict(s.refs),
                                      "branch": f"replicant-takeover/{s.job_id}" if pushed else None},
                                      node_key="takeover", effect="package", ordinal=s.takeover_ordinal)
            s.refs["takeover_package_uri"] = pkg["package_uri"]
        elif s.pr and s.pr.get("state") == "draft":
            await self._aux_act("publisher.close_pr", {"job_id": s.job_id, "pr": s.pr},
                                node_key="publish", effect="close_pr", ordinal=1)
        return (reason, None)

    async def _takeover_push(self, diff_uri: str | None) -> bool:
        """XD-FIX-R2-001 (+ WS-07 correction): takeover pushes go to `replicant-takeover/<job_id>`. When a critical
        risk class (for example ci_executed_paths) fires on the takeover diff, the requester must approve a
        publish_approval gate first. The Publisher raises PushRejected("ci_paths_takeover_unattested") when the
        tenant has not attested onboarding item OB-1; the takeover then ships an archive and patch without a branch."""
        s = self.s
        o = s.takeover_ordinal
        try:
            risk = await self.call("risk.classify", {"job_id": s.job_id, "tenant_id": s.tenant_id, "stage": "takeover",
                                   "refs": {**s.refs, "latest_diff_bundle_uri": diff_uri}, "targets": s.targets,
                                   "workflow_type": s.workflow_type}, block=False)
        except NodeFailed:
            risk = {"level": "critical", "rules_fired": ["risk_unavailable"]}
        if risk["level"] == "critical":
            kind = "publish_approval"
            gord = s.next_gate_ordinal(kind)
            gate_id = ids.gate_id_for(s.job_id, kind, s.cycle, gord)
            built = await self._aux_act("gates.build_packet", {"job_id": s.job_id, "tenant_id": s.tenant_id,
                                        "gate_id": gate_id, "kind": kind, "revision": 1, "refs": dict(s.refs), "risk": risk,
                                        "extra": {"takeover": True, "diff_bundle_uri": diff_uri}},
                                        node_key="takeover", effect="packet_takeover", ordinal=o)
            now = workflow.now()
            g = GateRecord(gate_id=gate_id, kind=kind, node_key="takeover", cycle=s.cycle, ordinal=o, state="open",
                           opened_at=now.isoformat(), presented_digest=built["digest"], packet_uri=built["packet_uri"],
                           channels=["ui", "slack", "cli"], eligible={"principals": [s.requester], "requester_may_decide": True},
                           expires_at=(now + timedelta(days=7)).isoformat(),
                           schedule={"expiry_seconds": 604800, "on_expiry": "expire"},
                           subject={k: built[k] for k in ("diff_sha256",) if built.get(k)})
            s.gates[gate_id] = g
            self.on_gate_opened(g)
            await self._notify("notify.gate_opened", {"job_id": s.job_id, "gate_id": gate_id, "kind": kind,
                               "channels": g.channels, "packet_uri": g.packet_uri, "eligible": g.eligible},
                               node_key="takeover", effect="notify_open_takeover", ordinal=o)
            try:
                r = await self._wait_gate(gate_id, "takeover")
            except NodeFailed:  # deadline or job age while waiting: no push
                r = None
            if r is None or isinstance(r, _Can) or s.gates[gate_id].state != "approved":
                self.emit("pr.takeover_branch_skipped", "takeover branch not pushed: approval not given")
                return False
        try:
            await self._aux_act("publisher.push_takeover_branch", {"job_id": s.job_id, "repo": self.primary_repo(),
                                "diff_bundle_uri": diff_uri, "branch": f"replicant-takeover/{s.job_id}"},
                                node_key="takeover", effect="push_branch", ordinal=o, block=False)
        except NodeFailed as f:
            if f.reason == "PushRejected":
                self.emit("pr.takeover_branch_skipped", "takeover branch rejected by the Publisher; archive and patch only")
                return False
            raise
        return True

    async def _compensate_failure(self) -> None:
        """Compensation for jobs that end failed or expired (R2-010): cancel runs still active (for example a
        parallel sibling) and close a draft PR. Best effort: a failure here never changes the outcome."""
        s = self.s
        try:
            for r in [r for r in s.runs.values() if r.active]:
                await self._aux_act("runner.cancel_run", {"run_id": r.run_id, "reason": "job_cancelled",
                                    "detail": "job ended"}, node_key=r.node_key, effect="cancel", ordinal=r.ordinal,
                                    cycle=r.cycle, block=False)
                r.active = False
            if s.pr and s.pr.get("state") == "draft":
                await self._aux_act("publisher.close_pr", {"job_id": s.job_id, "pr": s.pr},
                                    node_key="publish", effect="close_pr", ordinal=1, block=False)
        except NodeFailed as e:
            workflow.logger.warning("compensation incomplete", extra={"error": str(e)[:200]})

    async def _record_readiness(self, outcome: str) -> None:
        """Readiness probe (design §8c, onboarding-interfaces.md §7): the QA verdict and the latest QA run's result go
        back to the repo profile as ready, warnings or blocked. Only jobs with the repo-profile-env-v1 patch reach
        here (self._repo_profile). Best effort: never changes the outcome."""
        s, rp = self.s, self._repo_profile or {}
        qa = [r for r in s.runs.values() if r.role == "qa"]
        last = qa[-1] if qa else None
        try:
            await self._aux_act("inventory.record_readiness", {
                "job_id": s.job_id, "tenant_id": s.tenant_id, "repo_id": self.primary_repo().get("repo_id"),
                "profile_id": rp.get("profile_id"), "outcome": outcome, "verdict": s.last_qa_verdict,
                "qa_result_uri": last.result_uri if last else None, "qa_failure_reason": last.reason if last else None,
                "environment": self._compose.get("environment")},
                node_key="finalize", effect="readiness", ordinal=1, block=False)
        except NodeFailed as e:
            workflow.logger.warning("readiness not recorded", extra={"error": str(e)[:200]})

    async def _finish(self, outcome: str | None, reason: str | None) -> str:
        outcome = outcome or "infrastructure_failure"
        if self.s.evaluation and outcome == "pr_merged":
            outcome = "ready_to_publish"  # scripted rounds ended with `merged` (WS-06 eval-harness.md §3.3, R1V-014)
        status = TERMINAL_STATUS_FOR_OUTCOME.get(outcome, "failed")
        self.s.terminal = {"outcome": outcome, "reason": reason, "at": workflow.now().isoformat()}
        if not self.s.evaluation and self._ready:
            mem = {"job_id": self.s.job_id, "tenant_id": self.s.tenant_id, "outcome": outcome, "status": status,
                   "memory_snapshot_id": self.s.memory_snapshot_id, "cycle": self.s.cycle, "refs": dict(self.s.refs),
                   # MEM: outcome metrics key (memory.job_outcomes)
                   "repo": self.s.targets[0]["repo"] if self.s.targets else None, "workflow_type": self.s.workflow_type,
                   "release_bundle_id": self.s.release_bundle_id}
            if self.s.pr and self.s.targets and workflow.patched("memory-outcome-pr-v1"):
                # MEM-D12: the PR and repo, so the activity can check that the repair survived to the merged head
                mem["pr"] = {"number": self.s.pr.get("number"), "head_sha": self.s.pr.get("head_sha")}
                mem["repo_ref"] = self.primary_repo()
            try:  # best effort: memory bookkeeping never changes the job's outcome (WS-04 XD-04-01)
                await self._aux_act("memory.record_outcome", mem, node_key="finalize", effect="record_outcome",
                                    ordinal=1, block=False)
                await self._aux_act("memory.write_episode", mem, node_key="finalize", effect="write_episode",
                                    ordinal=1, block=False)
            except NodeFailed as e:  # retries exhausted or non-retryable
                workflow.logger.warning("memory bookkeeping failed", extra={"error": str(e)[:200]})
        if self._repo_profile and self._ready and READINESS_LABEL in (self.s.request_summary.get("labels") or []):
            await self._record_readiness(outcome)
        self._set_status(status, "done")
        self.emit("job.terminal", f"job {status} ({outcome})")
        # notifications.md §2 `job.succeeded` / `job.failed` (and the other terminal statuses) to the requester.
        # Fire-and-forget (block=False): a notification failure never changes the outcome. Patched for replay safety.
        if workflow.patched("notify-terminal-v1") and self.notifications_enabled() and self._ready:
            try:
                await self._aux_act("notify.job_event", {
                    "job_id": self.s.job_id, "kind": status, "outcome": outcome, "reason": (reason or "")[:500],
                    "pr": self.s.pr, "title": self.s.request_summary.get("title")},
                    node_key="finalize", effect="notify_terminal", ordinal=1, block=False)
            except NodeFailed as e:
                workflow.logger.warning("terminal notification not delivered", extra={"error": str(e)[:200]})
        await self._drain()
        self._flush_sa()
        return outcome
