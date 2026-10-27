"""Gate activities (WS-07 logic, XD-07-01) for v0: routing from the bundle's gate_rules (WS-01 fixture shape, local
values), the immutable approval packet, and the approval record (C-ART 2.0.0: approval_text in artifacts,
reference-only approval_record in audit; text_sha256 = sha256 of the approval_text object)."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from temporalio.exceptions import ApplicationError

from replicant_worker.activities import bundle as bundles
from replicant_worker.activities import s3
from replicant_worker.activities.base import implements
from replicant_worker.graphs.dsl import RISK_ORDER


def routing(kind: str, facts: dict[str, Any], rules: dict[str, Any], on_contested: str = "reopen_if_unpublished") -> dict[str, Any]:
    r = rules["gates"][kind]
    required = (facts["workflow_type"] in r.get("always_for", []) or kind in facts.get("require_gates", [])
                or RISK_ORDER[facts["risk_level"]] >= RISK_ORDER[r.get("risk_at_least", "critical")])
    if r.get("enabled") is False and kind not in facts.get("require_gates", []):
        required = False
    if kind in ("clarification", "budget_extension", "bundle_migration"):
        required = True
    if r.get("approvers", {}).get("requester_only"):
        elig: dict[str, Any] = {"principals": [facts["requester"]], "requester_may_decide": True}
    else:
        may = bool(r.get("requester_may_decide", False))
        if kind == "budget_extension":
            may = may and float(facts.get("amount_usd", 0)) <= float(r.get("requester_self_approve_max_usd", 0))
        elig = {"okta_groups": list(r.get("approvers", {}).get("okta_groups") or [f"{facts.get('tenant_id', 'x')}-approvers"]),
                "min_repo_permission": r.get("approvers", {}).get("min_repo_permission", "write"),
                "requester_may_decide": may}
    return {"schema": "replicant.gate-routing/v1", "gate_kind": kind, "required": bool(required),
            "rule_id": f"local.{kind}", "channels": list(r["channels"]), "eligible": elig,
            "timing": {"reminder_seconds": list(r.get("reminder_seconds", [])),
                       "reminder_repeat_seconds": int(r.get("reminder_repeat_seconds", 86400)),
                       "escalation_seconds": list(r.get("escalation_seconds", [])),
                       "expiry_seconds": int(r["expiry_seconds"]), "on_expiry": r.get("on_expiry", "expire")},
            "reapprove_after_rebase": r.get("reapprove_after_rebase", "on_conflict_or_risk_change")}


@implements("gates.resolve_routing")
async def gates_resolve_routing(a: dict[str, Any]) -> dict[str, Any]:
    f = a["facts"]
    rules = bundles.load(f["release_bundle_id"]).policies()["gate_rules"]
    return routing(a["gate_kind"], f, rules)


def _sha_of_uri(uri: str | None) -> str | None:
    if not uri:
        return None
    return hashlib.sha256(s3.get_bytes(s3.parse(uri))).hexdigest()


@implements("gates.build_packet")
async def gates_build_packet(a: dict[str, Any]) -> dict[str, Any]:
    """Approval packet revision (immutable): what the approver sees, by reference. presented_digest = sha256 of the
    packet bytes; diff_sha256 = sha256 of the diff bundle the packet presents (the Publisher re-checks it, G-03)."""
    refs = a.get("refs") or {}
    kind = a["kind"]
    diff_sha = _sha_of_uri(refs.get("latest_diff_bundle_uri")) if kind == "publish_approval" else None
    plan_sha = _sha_of_uri(refs.get("plan_uri")) if kind == "plan_approval" else None
    packet = {"schema": "replicant.approval-packet/v1", "job_id": a["job_id"], "tenant_id": a["tenant_id"],
              "gate_id": a["gate_id"], "kind": kind, "revision": a["revision"], "risk": a.get("risk"),
              "refs": {k: v for k, v in sorted(refs.items()) if isinstance(v, str)},
              **({"diff_sha256": diff_sha} if diff_sha else {}), **({"plan_sha256": plan_sha} if plan_sha else {}),
              "extra": a.get("extra") or {}}
    body = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    key = f"tenants/{a['tenant_id']}/jobs/{a['job_id']}/gates/{a['gate_id']}/packet-r{a['revision']}.json"
    put = s3.put_once(s3.ref("artifacts", key), body, "application/json")
    out = {"packet_uri": put["uri"], "digest": "sha256:" + put["sha256"]}
    if diff_sha:
        out["diff_sha256"] = diff_sha
    if plan_sha:
        out["plan_sha256"] = plan_sha
    return out


@implements("gates.write_approval_record")
async def gates_write_approval_record(a: dict[str, Any]) -> dict[str, Any]:
    d = a["decision"]
    t, j, g, dec = a["tenant_id"], a["job_id"], a["gate_id"], d["decision_id"]
    text = {k: v for k, v in (("comment", d.get("comment")), ("answer_text", a.get("answer_text"))) if v}
    text_key = text_sha = None
    if text:
        body = json.dumps({"schema": "replicant.approval-text/v1", "gate_id": g, "decision_id": dec, **text},
                          sort_keys=True, separators=(",", ":")).encode()
        put = s3.put_once(s3.ref("artifacts", f"tenants/{t}/jobs/{j}/gates/{g}/text-{dec}.json"), body, "application/json")
        text_key, text_sha = put["key"], put["sha256"]
    record = {"schema": "replicant.approval-record/v1", "tenant_id": t, "job_id": j, "gate_id": g,
              "gate_kind": a.get("gate_kind"), "decision_id": dec, "decision": d["decision"],
              "principal_id": d["principal_id"], "channel": d["channel"], "decided_at": d.get("decided_at"),
              "presented_digest": a.get("presented_digest"), "packet_key": (a.get("packet_uri") or "").split("/", 3)[-1],
              "subject": a.get("subject") or {}, "source": a.get("source"),
              **({"text_key": text_key, "text_sha256": text_sha} if text_key else {})}
    key = f"tenants/{t}/jobs/{j}/gates/{g}/decision-{dec}.json"
    try:
        s3.put_json_once(s3.ref("audit", key), record)
    except ApplicationError:
        raise
    out: dict[str, Any] = {"key": key}
    if text_sha:
        out.update({"text_key": text_key, "text_sha256": text_sha})
    return out
