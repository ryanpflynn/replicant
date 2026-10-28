"""Job graph view (GRAPH-*, docs/v0/DEVIATIONS.md): the job's C-DEF graph definition (pinned through its Release
Bundle) overlaid with per-node status derived from what the workflow already records — the projected timeline
(`node.started` / `node.completed` / `node.failed`, jobs_read.job_timeline), the JobSnapshot (status, current node,
gates) and jobs_read.job_runs. No parallel tracker. Pure functions; routes_graph.py does the I/O.

Node status: pending | running | waiting | succeeded | failed | skipped | cancelled.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from replicant_telemetry.common import iso

TERMINAL = ("succeeded", "failed", "cancelled", "rejected", "expired", "taken_over")
RUN_LIVE = ("Pending", "Provisioning", "Running", "Finalizing")
AGENT_TYPES = ("plan_run", "implement_run", "qa_run", "review_run", "repair_run", "triage_run")


def _ts(v: Any) -> dt.datetime | None:
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.UTC)
    if isinstance(v, str) and v:
        try:
            d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=dt.UTC)
        except ValueError:
            return None
    return None


def edge_label(when: str | None) -> str | None:
    """`nodes['qa'].decision in ['operator_and_gate', 'gate_with_failure']` → `operator_and_gate | gate_with_failure`."""
    if not when:
        return None
    s = re.sub(r"nodes\['[^']+'\]\.\w+\s*(==|in)\s*", "", when)
    s = re.sub(r"nodes\['[^']+'\]\.(\w+)", r"\1", s)
    s = s.replace("[", "").replace("]", "").replace("'", "").replace('"', "")
    s = re.sub(r"\s*,\s*", " | ", s).replace(" and ", " & ")
    return s.strip()[:60]


def graph_edges(spec: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in spec.get("edges") or []:
        src = e.get("from")
        if e.get("to"):
            out.append({"from": src, "to": e["to"], "kind": "next"})
        for i, sw in enumerate(e.get("switch") or []):
            out.append({"from": src, "to": sw.get("to"), "kind": "switch", "when": sw.get("when"),
                        "label": edge_label(sw.get("when")), "order": i})
        if e.get("default"):
            out.append({"from": src, "to": e["default"], "kind": "default", "label": "otherwise"})
    return out


def node_visits(timeline: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Fold node.* timeline events (seq order) into per-node visit records, and the order nodes were entered.
    A skipped node (guard false) or a reused plan emits only node.completed, so that also counts as an entry."""
    nodes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for e in timeline:
        typ = e.get("type") or ""
        refs = e.get("refs") or {}
        key = refs.get("node_key")
        if not key or not typ.startswith("node."):
            continue
        n = nodes.setdefault(key, {"visits": [], "state": "pending"})
        at = _ts(e.get("at"))
        summary = e.get("summary") or ""
        cur = n["visits"][-1] if n["visits"] and n["visits"][-1].get("open") else None
        if typ == "node.started":
            n["visits"].append({"started_at": at, "open": True})
            n["state"] = "running"
            order.append(key)
        elif typ == "node.completed":
            outcome = summary.split(": ", 1)[1] if ": " in summary else ("skipped" if summary.endswith(" skipped") else "")
            if cur is None:  # entered and finished in one step (skip / reused plan)
                cur = {"started_at": at}
                n["visits"].append(cur)
                order.append(key)
            cur.update(open=False, finished_at=at, outcome=outcome)
            n["state"] = "skipped" if outcome in ("skipped", "not_required") else "succeeded"
            n["outcome"] = outcome
        elif typ == "node.failed":
            reason = summary.split("failed: ", 1)[1] if "failed: " in summary else summary
            if cur is None:
                cur = {"started_at": at}
                n["visits"].append(cur)
            cur.update(open=False, finished_at=at, outcome="failed", reason=reason)
            n["state"] = "failed"
            n["outcome"] = "failed"
            n["reason"] = reason
    return nodes, order


def build_view(spec: dict[str, Any], timeline: list[dict[str, Any]], snapshot: dict[str, Any],
               runs: list[dict[str, Any]], now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.UTC)
    status = snapshot.get("status") or "running"
    terminal = status in TERMINAL
    visits, order = node_visits(timeline)
    gates = snapshot.get("gates") or []
    cur = (snapshot.get("current_node") or {}).get("node_key")
    runs_by_node: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        runs_by_node.setdefault(r.get("node_key") or "", []).append(r)

    nodes_out = []
    for nd in spec.get("nodes") or []:
        key = nd["key"]
        cfg = nd.get("config") or {}
        v = visits.get(key, {"visits": [], "state": "pending"})
        state = v["state"]
        nruns = sorted(runs_by_node.get(key, []), key=lambda r: (r.get("cycle") or 0, r.get("ordinal") or 0,
                                                                   str(r.get("started_at") or "")))
        live_run = any(r.get("phase") in RUN_LIVE for r in nruns)
        if state == "running":
            if terminal:
                state = "failed" if status == "failed" else "cancelled"
            elif nd.get("type") == "human_gate" and any(g.get("state") == "open" and g.get("kind") == cfg.get("gateKind")
                                                         for g in gates):
                state = "waiting"
            elif nd.get("type") == "pr_feedback_wait" and not live_run:
                state = "waiting"
            elif status in ("waiting_external", "waiting_human") and not live_run and key == cur:
                state = "waiting"
            elif status == "paused" and not live_run:
                state = "waiting"
        total_ms = 0
        for vis in v["visits"]:
            s, f = vis.get("started_at"), vis.get("finished_at")
            if s and f:
                total_ms += int((f - s).total_seconds() * 1000)
            elif s and not terminal and vis.get("open"):
                total_ms += int((now - s).total_seconds() * 1000)
        first = v["visits"][0].get("started_at") if v["visits"] else None
        last = v["visits"][-1] if v["visits"] else {}
        entered = len(v["visits"])
        item: dict[str, Any] = {
            "key": key, "type": nd.get("type"), "phase": nd.get("phase"), "status": state,
            "visits": entered, "max_visits": nd.get("maxVisits"),
            "agent": cfg.get("agent"), "gate_kind": cfg.get("gateKind"),
            "is_agent": nd.get("type") in AGENT_TYPES or bool(cfg.get("agent")),
            "is_gate": nd.get("type") == "human_gate",
            "has_guard": nd.get("when") is not None,
            "duration_ms": total_ms if entered else None,
            "started_at": iso(first), "finished_at": iso(last.get("finished_at")) if not last.get("open") else None,
            "current": key == cur and not terminal,
            "runs": [{k: r.get(k) for k in ("run_id", "role", "phase", "cycle", "ordinal", "started_at", "finished_at",
                                              "result_outcome", "qa_verdict", "reason") if r.get(k) is not None}
                     for r in nruns],
            "history": [{"started_at": iso(x.get("started_at")), "finished_at": iso(x.get("finished_at")),
                         **({"outcome": x["outcome"]} if x.get("outcome") else {}),
                         **({"reason": x["reason"]} if x.get("reason") else {})} for x in v["visits"][-20:]],
        }
        for k in ("outcome", "reason"):
            if v.get(k):
                item[k] = v[k]
        if item["is_gate"]:
            item["gates"] = [{k: g.get(k) for k in ("gate_id", "state", "opened_at", "expires_at") if g.get(k)}
                             | ({"decision": (g.get("decision") or {}).get("decision")} if g.get("decision") else {})
                             for g in gates if g.get("kind") == cfg.get("gateKind")]
        nodes_out.append(item)

    # edges, with how often the job took each one (consecutive node entries)
    taken: dict[tuple[str, str], int] = {}
    for a, b in zip(order, order[1:]):
        taken[(a, b)] = taken.get((a, b), 0) + 1
    terminal_outcome = (snapshot.get("terminal") or {}).get("outcome")
    if terminal and order and terminal_outcome:
        taken[(order[-1], "end:" + terminal_outcome)] = 1
    edges = graph_edges(spec)
    for e in edges:
        n = taken.get((e["from"], e["to"]), 0)
        if n:
            e["taken"] = n
    ends = sorted({e["to"] for e in edges if str(e["to"]).startswith("end:")})
    return {
        "entry": spec.get("entry"), "nodes": nodes_out, "edges": edges, "ends": ends,
        "boundaries": spec.get("boundaries") or [], "path": order[-200:],
        "terminal": ({"outcome": terminal_outcome, "node": "end:" + terminal_outcome} if terminal and terminal_outcome
                     else None),
        "job_status": status, "cycle": snapshot.get("cycle", 0), "current_node": cur if not terminal else None,
    }
