"""ReplicantJob: continue-As-New, the drain before it, and the Migration Handoff Protocol prologue (versioning.md 4, 6).

A mixin of ReplicantJobBase (workflows/replicant_job.py); it runs in workflow code under the sandbox."""

from __future__ import annotations

from typing import TYPE_CHECKING

from temporalio import workflow
from temporalio.exceptions import ActivityError

from replicant_worker.workflows.job.common import (
    HANDOFF_ROUTING_TIMEOUT, PROJECTION_DRAIN, SA_BUNDLE, SA_CYCLE, _Can, _opts, steering_backlog_input,
)

if TYPE_CHECKING:  # ReplicantJobInput stays beside ReplicantJob; imported in the method body at run time
    from replicant_worker.workflows.replicant_job import ReplicantJobInput


class CanMixin:
    # ============================================================ continue-as-new (versioning.md 4)
    async def _continue_as_new(self, req: _Can) -> None:
        from replicant_worker.workflows.replicant_job import ReplicantJobInput  # beside ReplicantJob (import cycle)
        s = self.s
        s.resume_at = req.resume_at
        if req.kind == "repair_cycle":
            s.cycle += 1
            s.visits = {}
            s.qa_fix_used = s.qa_env_retries = 0  # repair_limits.qa.* are per cycle (WS-05)
            # counters of finished cycles are never used again (logical IDs carry the cycle number)
            suffix = f"@c{s.cycle}"
            s.ordinals = {k: v for k, v in s.ordinals.items() if k.endswith(suffix)}
            s.gate_ordinals = {k: v for k, v in s.gate_ordinals.items() if k.endswith(suffix)}
            s.qa_gate_with_failure = False
        if req.kind in ("phase", "repair_cycle"):
            s.boundary_crossed = req.resume_at
        if req.kind == "reroute":
            s.graph_spec_digest = None
            s.boundary_crossed = None
        s.segment += 1
        s.prune(workflow.now())
        if len(s.steering_queue) > 4:  # keep the carry small: stage the backlog to S3 (XD-FIX-R1-100 shape)
            arg = steering_backlog_input(s)
            node_key = arg["node_key"]
            r = await self._aux_act("steering.stage_backlog", arg, node_key=node_key, effect="steering",
                                    ordinal=arg["ordinal"])
            s.steering_refs.extend(r.get("keys") or [r["uri"]])
            s.steering_queue = []
        self._queue_sa(SA_CYCLE.value_set(s.cycle))
        self.emit("phase.entered", f"continue-as-new ({req.kind}) before {req.resume_at}")
        await self._drain()
        self._flush_sa()  # search attributes are copied to the next run (C-IDS 3)
        if req.kind == "migration":
            m = s.migration or {}
            s.migration = {**m, "state": "handing_off"}
            workflow.continue_as_new(ReplicantJobInput(
                mode="migration_handoff", carry=s.carry(), reason="migration",
                handoff={"target_build_id": m["target_build_id"],
                         "target_release_bundle_id": m["target_release_bundle_id"]}))
        workflow.continue_as_new(ReplicantJobInput(mode="continue", carry=s.carry(), reason=req.kind))

    async def _drain(self) -> None:
        # R2-013: approval records, gate-resolution notices and audit-bearing projections (gate.*,
        # bundle.*, job.terminal) are awaited without a cap; each is bounded by its own schedule-to-close.
        await workflow.wait_condition(lambda: all(h.done() for h in self._audit) and self._sha_pending == 0)
        try:
            await workflow.wait_condition(lambda: all(h.done() for h in self._proj), timeout=PROJECTION_DRAIN)
        except TimeoutError:
            pass  # snapshot-only events: the reconciler repairs any gap (C-STATE 2.1)
        await workflow.wait_condition(workflow.all_handlers_finished)

    async def _migration_prologue(self, inp: ReplicantJobInput) -> None:
        """Migration Handoff Protocol v1 (versioning.md section 6). FROZEN: every build must issue
        exactly this command sequence first in a migration_handoff run, so that the run's history
        replays on both the source and the target build."""
        h = inp.handoff or {}
        source_build = self.s.worker_build_id
        prev_status, prev_phase = self.s.status, self.s.phase
        self._set_status("running", "migrating")  # C-IDS phase (R1-023)
        ok = True
        try:  # step 1: pin this execution to the target build (retried for 30 minutes, RELEASE_OVERRIDE)
            await workflow.execute_activity(
                "release.apply_migration_override",
                {"workflow_id": workflow.info().workflow_id, "run_id": workflow.info().run_id,
                 "target_build_id": h["target_build_id"], "target_release_bundle_id": h["target_release_bundle_id"]},
                activity_id="release.c0.n1.apply_migration_override", **_opts("release.apply_migration_override"))
        except ActivityError:
            ok = False
        if ok:  # step 2: wait until the server routes this execution's tasks to the target build
            try:
                await workflow.wait_condition(lambda: workflow.info().get_current_build_id() == h["target_build_id"],
                                              timeout=HANDOFF_ROUTING_TIMEOUT, timeout_summary="handoff routing")
            except TimeoutError:
                ok = False
        if not ok:
            # Failure path (versioning.md 6.3, R2-015). Only the source build reaches this branch: re-pin the
            # execution to the source build (best effort), alert operators, and continue on N.
            try:
                await workflow.execute_activity(
                    "release.apply_migration_override",
                    {"workflow_id": workflow.info().workflow_id, "run_id": workflow.info().run_id,
                     "target_build_id": source_build, "target_release_bundle_id": self.s.release_bundle_id},
                    activity_id="release.c0.n1.revert_migration_override", **_opts("release.apply_migration_override"))
            except ActivityError:
                pass  # the execution keeps the override; operators see the alert below
            self.s.migration = None
            self._set_status(prev_status, prev_phase)
            self.emit("bundle.migration_failed", f"migration to {h['target_release_bundle_id'][:12]} failed; "
                      "the job continues on its current bundle")
            await self._notify("notify.job_event", {"job_id": self.s.job_id, "kind": "migration_failed",
                               "audience": "operators", "target_release_bundle_id": h["target_release_bundle_id"]},
                               node_key="release", effect="notify_migration_failed", ordinal=1)
            return
        # Only the target build reaches this line.
        self.s.release_bundle_id = h["target_release_bundle_id"]
        self.s.worker_build_id = h["target_build_id"]
        self.s.migration = None
        self.s.graph_spec_digest = None
        for k in ("bundle_revoked", "model_retiring"):  # bundle-bound deadlines are met by moving bundles
            self.s.deadlines.pop(k, None)
        if source_build != h["target_build_id"]:
            self.s.deadlines.pop("worker_version_retire_scheduled", None)
        self.s.hold = None
        self.s.waiting = None
        self.s.release_blocked = None  # the new bundle is not rolled back; open gates re-evaluate V12
        for g in self.s.gates.values():
            g.release_block = None
        self._set_status(prev_status, prev_phase)
        self._queue_sa(SA_BUNDLE.value_set(self.s.release_bundle_id))
        self.emit("bundle.migrated", f"migrated to {self.s.release_bundle_id[:12]}")
