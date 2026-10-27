"""Builds a realistic ProjectionEvent sequence for one bugfix job (shapes of replicant_worker ReplicantJob.emit/snapshot)."""
from __future__ import annotations

import copy
from typing import Any

JOB = "job_01m5p7x7r0bt69j0h0fbxepw90"
T = "payments"
RB = "rb_ds5p3wmcj4dag2e9a8ncp4py6k"
REQ = "usr_01knk8dfr0fvjc7v0b9vhvkzbp"
BOB = "usr_01knk8dfr0fvjc7v0b9vhvkzbb"
GATE = "gate_01m5p7x7r0bt69j0h0fbxepw91"
DEC = "dec_01m5p7x7r0bt69j0h0fbxepw92"
RUN = "run_gj3h3ep3phjfs9e23v768zrca5"
DIG = "sha256:" + "a" * 64


def base_snapshot() -> dict[str, Any]:
    return {"schema": "replicant.job-snapshot/v1", "job_id": JOB, "tenant_id": T, "seq": 1, "as_of": "2026-10-24T08:00:01+00:00",
            "workflow_type": "bugfix", "status": "running", "phase": "context", "release_bundle_id": RB, "requester": REQ,
            "title": "Refund ledger entries double-count FX fees", "priority": "normal",
            "risk": {"level": "low", "rules_fired": [], "raised_by_llm": False}, "cycle": 0,
            "targets": [{"repo": "git.replicant.localhost:3300/acme-payments/ledger-api", "base_ref": "main",
                         "base_sha": "8c1e4f2a9b7d3c6e5f0a1b2c3d4e5f6a7b8c9d0e"}],
            "repair_budget": {"used": 0, "max": 0}, "gates": [], "runs": [], "steering": [], "paused": False,
            "pending_notices": [], "worker_deployment_version": {"deployment_name": "replicant-worker",
                                                                 "build_id": "b20261024-000000000000"},
            "memory_snapshot_id": "msnap_my7g26p6dy194fr4kjdehzb5hr",
            "current_node": {"node_key": "context", "cycle": 0, "ordinal": 1, "started_at": "2026-10-24T08:00:01+00:00"}}


def ev(seq: int, typ: str, summary: str, snap: dict[str, Any], **refs: Any) -> dict[str, Any]:
    s = copy.deepcopy(snap)
    s["seq"] = seq
    e: dict[str, Any] = {"type": typ, "at": s["as_of"], "summary": summary}
    if refs:
        e["refs"] = refs
    return {"schema": "replicant.projection-event/v1", "job_id": JOB, "tenant_id": T, "seq": seq,
            "workflow_run_id": "0199d9b2-0000-7000-8000-000000000001", "event": e, "snapshot": s}


def sequence() -> list[dict[str, Any]]:
    out = []
    s = base_snapshot()
    out.append(ev(1, "job.routed", "routed to bugfix (rule)", s))
    s["phase"], s["as_of"] = "implementing", "2026-10-24T08:01:00+00:00"
    s["current_node"] = {"node_key": "implement", "cycle": 0, "ordinal": 1, "started_at": s["as_of"]}
    s["runs"] = [{"run_id": RUN, "node_key": "implement", "cycle": 0, "ordinal": 1, "role": "implementer", "phase": "Pending",
                  "started_at": s["as_of"]}]
    out.append(ev(2, "run.status_changed", f"implementer run {RUN} created", s, run_id=RUN))
    s["runs"][0].update(phase="Succeeded", finished_at="2026-10-24T08:05:00+00:00",
                        result_uri=f"s3://local-replicant-local-artifacts/tenants/{T}/jobs/{JOB}/runs/{RUN}/run_result/result.json",
                        artifact_manifest_uri=f"s3://local-replicant-local-artifacts/tenants/{T}/jobs/{JOB}/runs/{RUN}/manifest.json")
    s["as_of"] = "2026-10-24T08:05:00+00:00"
    out.append(ev(3, "run.status_changed", f"run {RUN} Succeeded", s, run_id=RUN))
    s["status"], s["phase"], s["as_of"] = "waiting_human", "publish_gate", "2026-10-24T08:07:00+00:00"
    s["current_node"] = {"node_key": "publish_gate", "cycle": 0, "ordinal": 1, "started_at": s["as_of"]}
    gate = {"gate_id": GATE, "kind": "publish_approval", "state": "open", "opened_at": s["as_of"], "presented_digest": DIG,
            "packet_uri": f"s3://local-replicant-local-artifacts/tenants/{T}/jobs/{JOB}/gates/{GATE}/packet-r1.json",
            "channels": ["ui", "cli", "api"], "escalation_level": 0, "expires_at": "2026-10-31T08:07:00+00:00",
            "eligible": {"okta_groups": ["payments-approvers"], "min_repo_permission": "write", "requester_may_decide": True},
            "qa_reruns_used": 0}
    s["gates"] = [gate]
    out.append(ev(4, "gate.opened", "publish_approval gate opened", s, gate_id=GATE))
    s["status"], s["phase"], s["as_of"] = "running", "publish_gate", "2026-10-24T08:09:00+00:00"
    gate.update(state="approved", decision={"decision_id": DEC, "decision": "approve", "principal_id": BOB, "channel": "api",
                                           "decided_at": s["as_of"], "comment": "ship it"},
                decision_log=[{"decision_id": DEC, "outcome": "accepted", "decision": "approve", "principal_id": BOB,
                               "channel": "api", "decided_at": s["as_of"], "presented_digest": DIG,
                               "text_key": f"tenants/{T}/jobs/{JOB}/gates/{GATE}/text-{DEC}.json"}])
    out.append(ev(5, "gate.decided", "publish_approval approve via api", s, gate_id=GATE, decision_id=DEC))
    gate["decision_log"][0]["text_sha256"] = "b" * 64
    s["as_of"] = "2026-10-24T08:09:01+00:00"
    out.append(ev(6, "gate.approval_recorded", "publish_approval approval record written", s, gate_id=GATE, decision_id=DEC))
    s["status"], s["phase"], s["as_of"] = "waiting_external", "pr_review", "2026-10-24T08:10:00+00:00"
    s["pull_request"] = {"repo": "git.replicant.localhost:3300/acme-payments/ledger-api", "number": 7,
                         "url": "http://git.replicant.localhost:3300/acme-payments/ledger-api/pulls/7", "head_sha": "c" * 40,
                         "state": "open"}
    out.append(ev(7, "pr.opened", "PR #7 opened", s))
    s["status"], s["phase"], s["as_of"] = "succeeded", "done", "2026-10-24T09:00:00+00:00"
    s["current_node"] = None
    del s["current_node"]
    s["terminal"] = {"outcome": "pr_merged", "at": s["as_of"]}
    out.append(ev(8, "job.terminal", "job succeeded (pr_merged)", s))
    return out
