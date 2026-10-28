"""ReplicantJob: query handlers (C-MSG 2.3). Registration stays on ReplicantJob.

A mixin of ReplicantJobBase (workflows/replicant_job.py); it runs in workflow code under the sandbox."""

from __future__ import annotations

from typing import Any

from replicant_worker.workflows.job.common import _gate_view


class QueriesMixin:
    # ============================================================ Queries (C-MSG 2.3)
    def q_open_gates(self) -> dict[str, Any]:
        return {"schema": "replicant.msg.open-gates/v1",
                "gates": [_gate_view(g) for g in self.s.gates.values() if g.state == "open"]}

    def q_working_memory_refs(self) -> dict[str, Any]:
        r = self.s.refs
        out = {"schema": "replicant.msg.working-memory-refs/v1"}
        for k in ("plan_uri", "latest_handoff_uri", "latest_diff_bundle_uri", "latest_session_state_uri",
                  "latest_qa_report_uri", "briefing_uri"):
            if r.get(k):
                out[k] = r[k]
        if self.s.memory_snapshot_id:
            out["memory_snapshot_id"] = self.s.memory_snapshot_id
        return out
