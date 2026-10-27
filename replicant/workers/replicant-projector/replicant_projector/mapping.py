"""Pure mapping from a ProjectionEvent / JobSnapshot to read-model rows (jobs_read.sql, approvals.sql). No I/O, so it
is unit-tested without Postgres."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

TERMINAL = {"succeeded", "failed", "cancelled", "rejected", "expired", "taken_over"}
SUB_STATUS = {"tenant_run_quota": "waiting_capacity_tenant", "cluster_capacity": "waiting_capacity_cluster",
              "tenant_budget_exhausted": "tenant_budget_exhausted", "platform_budget_exhausted": "platform_budget_exhausted",
              "dependency_outage": "dependency_outage", "operator_hold": "operator_hold", "bundle_revoked": "bundle_revoked",
              "kill_switch_starts": "kill_switch_starts", "kill_switch_runs": "kill_switch_runs",
              "kill_switch_publishing": "kill_switch_publishing", "retrying": "retrying", "rebasing": "rebasing"}
RISK = {"low", "medium", "high", "critical"}
TIMELINE_TYPE = re.compile(r"^[a-z]+(\.[a-z_]+)+$")
DECISIONS = {"approve", "reject", "request_changes", "answer", "rerun_qa"}
CHANNELS = {"ui", "cli", "api", "slack", "github", "gitea", "system"}
PR_STATES = {"draft", "open", "merged", "closed"}
SESSION_GRACE_DAYS = 7


def ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def key_of(uri: str | None) -> str | None:
    """Bucket-relative key of an s3:// URI (the read model stores logical keys, C-ART §3)."""
    if not uri or not uri.startswith("s3://"):
        return uri
    return uri[5:].split("/", 1)[1] if "/" in uri[5:] else ""


def timeline_rows(ev: dict[str, Any]) -> list[dict[str, Any]]:
    items = ev.get("events") or [{"seq": ev["seq"], "event": ev["event"]}]
    out = []
    for it in items:
        e = it["event"]
        if not TIMELINE_TYPE.match(e.get("type", "")):
            continue
        out.append({"job_id": ev["job_id"], "seq": int(it["seq"]), "tenant_id": ev["tenant_id"], "type": e["type"],
                    "at": ts(e["at"]), "summary": str(e.get("summary", ""))[:300], "actor": e.get("actor"),
                    "refs": e.get("refs")})
    return out


def job_row(ev: dict[str, Any], *, repo_ids: list[str] | None = None) -> dict[str, Any]:
    s = ev["snapshot"]
    seq = max(int(ev["seq"]), int(s.get("seq") or 0))
    w = s.get("waiting") or None
    cur = s.get("current_node") or {}
    risk = (s.get("risk") or {}).get("level")
    term = s.get("terminal") or {}
    open_gates = [g for g in s.get("gates") or [] if g.get("state") == "open"]
    pr = s.get("pull_request") or {}
    events = [it["event"] for it in (ev.get("events") or [{"event": ev["event"]}])]
    purge = None
    for e in events:
        if e.get("type") == "artifacts.expire_session_state":
            purge = (ts(e.get("at")) or ts(s["as_of"])) + timedelta(days=SESSION_GRACE_DAYS)  # type: ignore[operator]
    waiting_since = ts(w.get("since")) if w else None
    if not waiting_since and s.get("status") in ("waiting_human", "waiting_external", "paused"):
        waiting_since = min((ts(g.get("opened_at")) for g in open_gates if g.get("opened_at")), default=None)
    evm = (s.get("evaluation") or {}).get("mode")
    return {
        "job_id": s["job_id"], "tenant_id": s["tenant_id"], "seq": seq, "as_of": ts(s["as_of"]),
        "parent_job_id": s.get("parent_job_id"), "workflow_kind": "ReplicantJob", "workflow_type": s["workflow_type"],
        "status": s["status"], "phase": s["phase"],
        "sub_status": SUB_STATUS.get((w or {}).get("reason", "")) if w else None, "sub_status_detail": w,
        "pr_repo_id": (repo_ids or [None])[0] if pr.get("number") else None, "pr_number": pr.get("number"),
        "current_node": cur.get("node_key"), "current_node_started_at": ts(cur.get("started_at")),
        "title": s.get("title"), "requester": s["requester"], "on_behalf_of": None, "priority": s.get("priority"),
        "risk_level": risk if risk in RISK else None, "evaluation_mode": evm if evm in ("eval", "shadow") else None,
        "repos": [t["repo"] for t in s.get("targets") or []], "repo_ids": list(repo_ids or []),
        "release_bundle_id": s["release_bundle_id"],
        "worker_build_id": (s.get("worker_deployment_version") or {}).get("build_id"),
        "memory_snapshot_id": s.get("memory_snapshot_id"), "cycle": int(s.get("cycle") or 0),
        "repair_used": (s.get("repair_budget") or {}).get("used"), "repair_max": (s.get("repair_budget") or {}).get("max"),
        "paused": bool(s.get("paused")), "open_gate_id": open_gates[0]["gate_id"] if open_gates else None,
        "waiting_since": waiting_since, "last_progress_at": ts(s["as_of"]),
        "terminal_outcome": term.get("outcome"), "terminal_at": ts(term.get("at")),
        "session_purge_due_at": purge, "created_at": ts(s["as_of"]), "snapshot": s,
    }


def run_rows(ev: dict[str, Any]) -> list[dict[str, Any]]:
    s = ev["snapshot"]
    seq = int(ev["seq"])
    out = []
    for r in s.get("runs") or []:
        out.append({"run_id": r["run_id"], "job_id": s["job_id"], "tenant_id": s["tenant_id"], "seq": seq,
                    "node_key": r["node_key"], "cycle": int(r.get("cycle") or 0), "ordinal": int(r.get("ordinal") or 1),
                    "role": r["role"], "phase": r["phase"], "reason": r.get("reason"), "isolation": r.get("isolation"),
                    "started_at": ts(r.get("started_at")), "finished_at": ts(r.get("finished_at")),
                    "artifact_manifest_key": key_of(r.get("artifact_manifest_uri")), "result_key": key_of(r.get("result_uri"))})
    return out


def _revision(packet_uri: str) -> int:
    m = re.search(r"packet-r(\d+)\.json$", packet_uri or "")
    return int(m.group(1)) if m else 1


def gate_rows(ev: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(gates, eligibility, decisions)."""
    s = ev["snapshot"]
    seq = int(ev["seq"])
    risk = (s.get("risk") or {}).get("level")
    repo = (s.get("targets") or [{}])[0].get("repo") if s.get("targets") else None
    gates, elig, decs = [], [], []
    for g in s.get("gates") or []:
        e = g.get("eligible") or {}
        d = g.get("decision") or None
        gates.append({"gate_id": g["gate_id"], "job_id": s["job_id"], "tenant_id": s["tenant_id"], "seq": seq,
                      "kind": g["kind"], "state": g["state"], "risk_level": risk, "title": s.get("title"), "repo": repo,
                      "requester": s["requester"], "release_bundle_id": s["release_bundle_id"],
                      "opened_at": ts(g["opened_at"]), "expires_at": ts(g.get("expires_at")),
                      "next_reminder_at": ts(g.get("next_reminder_at")), "escalation_level": int(g.get("escalation_level") or 0),
                      "packet_revision": _revision(g["packet_uri"]), "presented_digest": g["presented_digest"],
                      "packet_key": key_of(g["packet_uri"]) or "", "channels": list(g.get("channels") or []),
                      "min_repo_permission": e.get("min_repo_permission"),
                      "requester_may_decide": bool(e.get("requester_may_decide", False)),
                      "qa_reruns_used": min(2, int(g.get("qa_reruns_used") or 0)), "winning_decision": d,
                      "decided_at": ts((d or {}).get("decided_at")), "release_block": g.get("release_block")})
        since = ts(g["opened_at"])
        for p in e.get("principals") or []:
            elig.append({"gate_id": g["gate_id"], "tenant_id": s["tenant_id"], "kind": "principal", "value": p, "since": since})
        for og in e.get("okta_groups") or []:
            elig.append({"gate_id": g["gate_id"], "tenant_id": s["tenant_id"], "kind": "okta_group", "value": og, "since": since})
        for entry in g.get("decision_log") or []:
            if entry.get("decision") not in DECISIONS or entry.get("channel") not in CHANNELS:
                continue
            text_key, text_sha = entry.get("text_key"), entry.get("text_sha256")
            if not (text_key and text_sha):  # CHECK ck_gate_decisions_text_pair: both or neither
                text_key = text_sha = None
            decs.append({"decision_id": entry["decision_id"], "gate_id": g["gate_id"], "tenant_id": s["tenant_id"],
                         "outcome": entry.get("outcome", "accepted"), "decision": entry["decision"],
                         "principal_id": entry["principal_id"], "channel": entry["channel"],
                         "delegate_of": entry.get("delegate_of"), "denial_reason": entry.get("denial_reason"),
                         "text_key": text_key, "text_sha256": text_sha, "decided_at": ts(entry.get("decided_at")),
                         "presented_digest": entry.get("presented_digest") or g["presented_digest"]})
    return gates, elig, decs


def pr_row(ev: dict[str, Any], repo_ids: list[str] | None) -> dict[str, Any] | None:
    s = ev["snapshot"]
    pr = s.get("pull_request") or {}
    if not pr.get("number"):
        return None
    term = s.get("terminal") or {}
    state = pr.get("state", "open")
    at = ts(s["as_of"])
    merged_at = closed_at = None
    if term.get("outcome") == "pr_merged":
        state, merged_at = "merged", ts(term.get("at")) or at
    elif term.get("outcome") == "pr_closed_unmerged":
        state, closed_at = "closed", ts(term.get("at")) or at
    events = [it["event"] for it in (ev.get("events") or [{"event": ev["event"]}])]
    for e in events:
        if e.get("type") == "pr.merged":
            state, merged_at = "merged", ts(e.get("at"))
        elif e.get("type") == "pr.closed":
            state, closed_at = "closed", ts(e.get("at"))
    return {"repo_id": (repo_ids or [None])[0] or pr.get("repo") or "", "pr_number": int(pr["number"]),
            "job_id": s["job_id"], "tenant_id": s["tenant_id"], "repo": pr.get("repo") or "", "head_ref": f"replicant/{s['job_id']}",
            "head_sha": pr.get("head_sha"), "state": state if state in PR_STATES else "open", "url": pr.get("url"),
            "job_open": s["status"] not in TERMINAL, "opened_at": at, "merged_at": merged_at, "closed_at": closed_at}


def steering_rows(ev: dict[str, Any]) -> list[dict[str, Any]]:
    s = ev["snapshot"]
    out = []
    for m in s.get("steering") or []:
        if not str(m.get("message_id", "")).startswith("msg_"):
            continue
        out.append({"message_id": m["message_id"], "job_id": s["job_id"], "tenant_id": s["tenant_id"],
                    "actor": m.get("actor"), "outcome": m.get("outcome", "queued"), "run_id": m.get("run_id"),
                    "at": ts(m.get("at")) or ts(s["as_of"])})
    return out


AUDIT_CHANNEL = {"ui": "ui", "cli": "cli", "api": "api", "slack": "slack", "github": "github", "gitea": "github",
                 "system": "system"}
AUDIT_OUTCOME = {"accepted": "success", "denied": "denied", "superseded": "superseded", "recorded": "success"}
AUDITED = ("gate.opened", "gate.decided", "gate.decision_denied", "gate.decision_superseded", "gate.expired",
           "gate.auto_resolved", "job.terminal")


def audit_events(ev: dict[str, Any]) -> list[dict[str, Any]]:
    """audit.emit() payloads for gate and decision events (C-STATE §2.1 step 2; audit.sql key whitelist)."""
    s = ev["snapshot"]
    items = ev.get("events") or [{"seq": ev["seq"], "event": ev["event"]}]
    out = []
    for it in items:
        e = it["event"]
        if e.get("type") not in AUDITED:
            continue
        refs = e.get("refs") or {}
        a: dict[str, Any] = {"event_type": e["type"], "occurred_at": e["at"], "tenant_id": s["tenant_id"],
                             "job_id": s["job_id"], "source_service": "replicant-projector",
                             "actor_principal_id": "svc_replicant-projector",
                             "release_bundle_id": s["release_bundle_id"], "channel": "system", "outcome": "success",
                             "idempotency_key": f"proj:{s['job_id']}:{it['seq']}"}
        if refs.get("gate_id"):
            a["gate_id"] = refs["gate_id"]
            g = next((g for g in s.get("gates") or [] if g["gate_id"] == refs["gate_id"]), None)
            if g:
                a["digest"] = g["presented_digest"]
                if refs.get("decision_id"):
                    le = next((x for x in g.get("decision_log") or [] if x.get("decision_id") == refs["decision_id"]), None)
                    if le:
                        a["actor_principal_id"] = le["principal_id"]
                        a["channel"] = AUDIT_CHANNEL.get(le.get("channel"), "system")
                        a["outcome"] = AUDIT_OUTCOME.get(le.get("outcome"), "success")
        if refs.get("decision_id"):
            a["decision_id"] = refs["decision_id"]
        if e["type"] == "job.terminal":
            a["outcome"] = "success" if s["status"] == "succeeded" else "failure"
            a["reason"] = re.sub(r"[^A-Za-z0-9_:.-]", "_", str((s.get("terminal") or {}).get("outcome") or "unknown"))[:120]
            a["details"] = {"status": s["status"]}
        out.append({k: v for k, v in a.items() if v is not None})
    return out
