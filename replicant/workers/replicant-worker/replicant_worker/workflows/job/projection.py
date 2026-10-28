"""ReplicantJob: projection events and the JobSnapshot (C-STATE 2).

A mixin of ReplicantJobBase (workflows/replicant_job.py); it runs in workflow code under the sandbox."""

from __future__ import annotations

from typing import Any

from temporalio import workflow

from replicant_worker.activities.options import PROJECTION
from replicant_worker.workflows.job.common import AUDIT_EVENT_PREFIXES, SA_SEQ, _gate_view


class ProjectionMixin:
    # ============================================================ projection (C-STATE 2)
    def emit(self, event_type: str, summary: str, **refs: Any) -> None:
        s = self.s
        s.projection_seq += 1
        self._queue_sa(SA_SEQ.value_set(s.projection_seq))
        event = {"schema": "replicant.projection-event/v1", "job_id": s.job_id, "tenant_id": s.tenant_id,
                 "seq": s.projection_seq, "workflow_run_id": workflow.info().run_id,
                 "event": {"type": event_type, "at": workflow.now().isoformat(), "summary": summary[:300],
                           **({"refs": {k: v for k, v in refs.items() if v}} if refs else {})},
                 "snapshot": self.snapshot()}
        h = workflow.start_activity(
            "projection.apply", event, task_queue=PROJECTION.task_queue,
            schedule_to_close_timeout=PROJECTION.schedule_to_close, start_to_close_timeout=PROJECTION.start_to_close,
            retry_policy=PROJECTION.retry, summary=f"projection seq {s.projection_seq}")
        if event_type.startswith(AUDIT_EVENT_PREFIXES):  # the projector audits these (R2-013)
            self._audit = [a for a in self._audit if not a.done()] + [h]
        else:
            self._proj = [p for p in self._proj if not p.done()] + [h]

    def snapshot(self) -> dict[str, Any]:
        s = self.s
        snap: dict[str, Any] = {
            "schema": "replicant.job-snapshot/v1", "job_id": s.job_id, "tenant_id": s.tenant_id,
            "seq": max(1, s.projection_seq), "as_of": workflow.now().isoformat(),
            "workflow_type": s.workflow_type, "status": s.status, "phase": s.phase,
            "release_bundle_id": s.release_bundle_id, "requester": s.requester, "title": s.title,
            "priority": s.priority, "risk": s.risk, "cycle": s.cycle,
            "targets": [{k: v for k, v in {"repo": t["repo"], "base_ref": t.get("base_ref"),
                                           "base_sha": t.get("base_sha")}.items() if v} for t in s.targets],
            "repair_budget": {"used": s.repair_used, "max": s.repair_max},
            "gates": [{**_gate_view(g), **({"qa_reruns_used": min(2, s.qa_reruns)} if g.kind == "publish_approval"
                                            and g.cycle == s.cycle else {})} for g in list(s.gates.values())[-50:]],
            "runs": [{k: v for k, v in {"run_id": r.run_id, "node_key": r.node_key, "cycle": r.cycle,
                                        "ordinal": r.ordinal, "role": r.role, "phase": r.phase, "reason": r.reason,
                                        "isolation": r.isolation, "started_at": r.started_at,
                                        "finished_at": r.finished_at, "artifact_manifest_uri": r.artifact_manifest_uri,
                                        "result_uri": r.result_uri}.items() if v is not None}
                     for r in list(s.runs.values())[-100:]],
            "steering": s.steering_log[-200:], "paused": s.paused,
            "pending_notices": [n for n in s.pending_notices if n in (
                "newer_bundle_available", "worker_version_draining", "worker_version_retire_scheduled",
                "bundle_revoked", "bundle_patched", "model_retiring", "bundle_rolled_back")][-20:],  # R1V-007
        }
        if s.worker_build_id:
            snap["worker_deployment_version"] = {"deployment_name": "replicant-worker", "build_id": s.worker_build_id}
        if s.parent_job_id:
            snap["parent_job_id"] = s.parent_job_id
        if s.memory_snapshot_id:
            snap["memory_snapshot_id"] = s.memory_snapshot_id
        if s.active_nodes:
            k, v = next(iter(s.active_nodes.items()))
            snap["current_node"] = {"node_key": k, "cycle": s.cycle, "ordinal": v["ordinal"], "started_at": v["started_at"]}
        if s.pr and s.pr.get("number"):
            snap["pull_request"] = {k: v for k, v in {"repo": s.pr.get("repo"), "number": s.pr["number"],
                                                      "url": s.pr.get("url"), "head_sha": s.pr.get("head_sha"),
                                                      "state": s.pr.get("state", "open")}.items() if v}
        if s.terminal:
            snap["terminal"] = {k: v for k, v in s.terminal.items() if v is not None}
        if s.evaluation:
            snap["evaluation"] = {k: s.evaluation[k] for k in ("mode", "eval_run_id", "shadow_of_job_id") if s.evaluation.get(k)}
        if s.waiting:
            snap["waiting"] = dict(s.waiting)
        return snap
