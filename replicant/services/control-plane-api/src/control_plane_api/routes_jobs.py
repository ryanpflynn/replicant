"""Jobs: intake (`POST /v1/jobs`) and the read API from the `jobs_read` projection (architecture.md §2.6)."""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Header, Query, Request, Response
from sqlalchemy import text

from control_plane_api.auth import check, require, scope_filter
from control_plane_api.authz import Principal
from control_plane_api.db import OPERATOR_SCOPE
from control_plane_api.problems import ProblemError
from replicant_telemetry.common import iso

router = APIRouter()

TERMINAL = ("succeeded", "failed", "cancelled", "rejected", "expired", "taken_over")


def _cursor_decode(c: str | None) -> int:
    if not c:
        return 0
    try:
        return int(json.loads(base64.urlsafe_b64decode(c + "==").decode())["o"])
    except Exception:  # noqa: BLE001
        raise ProblemError(400, "invalid_cursor")


def _cursor_encode(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode().rstrip("=")


@router.post("/v1/jobs", status_code=201)
async def create_job(request: Request, response: Response, body: dict[str, Any] = Body(...),
                     principal: Principal = require("job:create|service:github-integration|service:slack-integration|"
                                                    "service:jira-integration|service:scheduler|service:eval-worker|"
                                                    "service:release-controller")) -> dict[str, Any]:
    res = await request.app.state.intake.submit(principal, body)
    response.status_code = res.status_code
    if isinstance(res.body, dict) and res.body.get("job_id"):  # C-IDS §9 correlation line for the job (M2)
        logging.getLogger("control_plane_api.intake").info(
            "job.intake created=%s status=%s", res.status_code == 201, res.body.get("status"),
            extra={"job_id": res.body["job_id"], "tenant_id": body.get("tenant_id")})
    return res.body


def _readable_tenants(request: Request, principal: Principal) -> set[str] | None:
    t = principal.tenants_with("job:read")
    if t is not None and not t:
        raise ProblemError(404, "not_found")
    return t


def _summary(r: Any, gates: dict[str, dict[str, Any]], prs: dict[str, dict[str, Any]],
             costs: dict[str, float]) -> dict[str, Any]:
    snap = r.snapshot or {}
    out: dict[str, Any] = {
        "job_id": r.job_id, "tenant_id": r.tenant_id, "title": r.title or "", "status": r.status, "phase": r.phase,
        "workflow_type": r.workflow_type, "requester": r.requester, "repos": list(r.repos or []),
        "updated_at": iso(r.updated_at), "created_at": iso(r.created_at), "release_bundle_id": r.release_bundle_id,
    }
    for k in ("current_node", "sub_status", "risk_level", "on_behalf_of", "sub_status_detail"):
        v = getattr(r, k)
        if v is not None:
            out[k] = v
    if snap.get("evaluation"):
        out["evaluation"] = snap["evaluation"]
    if r.job_id in gates:
        out["open_gate"] = gates[r.job_id]
    if r.job_id in prs:
        out["pr"] = prs[r.job_id]
    if r.job_id in costs:
        out["cost_usd"] = costs[r.job_id]
    return out


async def _side_tables(request: Request, conn: Any, job_ids: list[str]) -> tuple[dict, dict, dict]:
    db = request.app.state.db
    gates: dict[str, dict[str, Any]] = {}
    prs: dict[str, dict[str, Any]] = {}
    costs: dict[str, float] = {}
    if not job_ids:
        return gates, prs, costs
    if await db.has_relation("approvals.gates"):
        for g in (await conn.execute(text("SELECT gate_id, job_id, kind, expires_at FROM approvals.gates WHERE "
                                          "state = 'open' AND job_id = ANY(:j)"), {"j": job_ids})).all():
            gates[g.job_id] = {"gate_id": g.gate_id, "kind": g.kind, **({"expires_at": iso(g.expires_at)}
                                                                        if g.expires_at else {})}
    if await db.has_relation("jobs_read.job_prs"):
        for p in (await conn.execute(text("SELECT job_id, pr_number, url, state FROM jobs_read.job_prs WHERE "
                                          "job_id = ANY(:j)"), {"j": job_ids})).all():
            prs[p.job_id] = {"number": p.pr_number, "state": p.state, **({"url": p.url} if p.url else {})}
    if await db.has_relation("cost.job_rollups"):
        try:
            async with conn.begin_nested():
                for c in (await conn.execute(text("SELECT job_id, usd_total FROM cost.job_rollups WHERE job_id = "
                                                  "ANY(:j)"), {"j": job_ids})).all():
                    costs[c.job_id] = float(c.usd_total or 0)
        except Exception:  # noqa: BLE001 - WS-03 column names not confirmed in M1
            pass
    return gates, prs, costs


@router.get("/v1/jobs")
async def list_jobs(request: Request, response: Response, principal: Principal = require("job:read"),
                    tenant_id: str | None = None, q: str | None = Query(None, max_length=500),
                    status: str | None = None, view: str = "all", include_evaluation: str = "false",
                    sort: str = "updated_desc", cursor: str | None = None,
                    limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    _readable_tenants(request, principal)
    p: dict[str, Any] = {}
    scope = scope_filter(request.app.state.auth, principal, "job:read", "j", p)
    if scope is None:
        return {"items": [], "next_cursor": None}
    where = [scope]
    if tenant_id:
        where.append("j.tenant_id = :tenant_id")
        p["tenant_id"] = tenant_id
    if status:
        where.append("j.status = ANY(:statuses)")
        p["statuses"] = [s for s in status.split(",") if s]
    views = {"all": None, "mine": "j.requester = :me", "needs_human": "j.status = 'waiting_human'",
             "running": "j.status IN ('queued', 'running')", "pr_review": "j.status = 'waiting_external'",
             "failed_24h": "j.status = 'failed' AND j.terminal_at > now() - interval '24 hours'"}
    if view not in views:
        raise ProblemError(400, "invalid_view")
    if views[view]:
        where.append(views[view])
        p["me"] = principal.principal_id
    ev = {"false": "j.evaluation_mode IS NULL", "eval": "j.evaluation_mode = 'eval'",
          "shadow": "j.evaluation_mode = 'shadow'", "all": None}
    if include_evaluation not in ev:
        raise ProblemError(400, "invalid_include_evaluation")
    if ev[include_evaluation]:
        where.append(ev[include_evaluation])
    for i, tok in enumerate((q or "").split()):
        key, _, val = tok.partition(":")
        col = {"status": "j.status", "bundle": "j.release_bundle_id", "workflow": "j.workflow_type",
               "phase": "j.phase"}.get(key)
        if col and val:
            where.append(f"{col} = :q{i}")
            p[f"q{i}"] = val
        elif key == "requester" and val:
            where.append(f"j.requester = :q{i}")
            p[f"q{i}"] = principal.principal_id if val == "me" else val
        elif key == "repo" and val:
            where.append(f"EXISTS (SELECT 1 FROM unnest(j.repos) r WHERE r LIKE :q{i})")
            p[f"q{i}"] = "%" + val.replace("*", "%")
        else:
            where.append(f"(j.title ILIKE :q{i} OR j.job_id = :qe{i})")
            p[f"q{i}"] = f"%{tok}%"
            p[f"qe{i}"] = tok
    order = {"updated_desc": "j.updated_at DESC, j.job_id DESC", "created_desc": "j.created_at DESC, j.job_id DESC",
             "cost_desc": "j.updated_at DESC, j.job_id DESC",
             "waiting_since_asc": "j.waiting_since ASC NULLS LAST, j.job_id"}.get(sort)
    if order is None:
        raise ProblemError(400, "invalid_sort")
    offset = _cursor_decode(cursor)
    p.update(lim=limit + 1, off=offset)
    sql = (f"SELECT j.* FROM jobs_read.jobs j WHERE {' AND '.join(where)} ORDER BY {order} LIMIT :lim OFFSET :off")
    async with request.app.state.db.tx(scope=OPERATOR_SCOPE) as conn:
        rows = (await conn.execute(text(sql), p)).all()
        page = rows[:limit]
        gates, prs, costs = await _side_tables(request, conn, [r.job_id for r in page])
    now = dt.datetime.now(dt.UTC)
    lags = [int((now - r.as_of).total_seconds()) for r in page if r.status not in TERMINAL and r.as_of]
    response.headers["X-Replicant-Projection-Lag-Seconds"] = str(max(0, max(lags)) if lags else 0)
    return {"items": [_summary(r, gates, prs, costs) for r in page],
            "next_cursor": _cursor_encode(offset + limit) if len(rows) > limit else None}


async def load_job_row(request: Request, principal: Principal, job_id: str) -> Any:
    """Job-scoped reads return 404 when the caller may not see the job (no existence leak)."""
    async with request.app.state.db.tx(scope=OPERATOR_SCOPE) as conn:
        r = (await conn.execute(text("SELECT * FROM jobs_read.jobs WHERE job_id = :j"), {"j": job_id})).first()
    if r is None or not check(request.app.state.auth, principal, "job:read", tenant_id=r.tenant_id,
                              repo_ids=tuple(r.repo_ids or ())):
        raise ProblemError(404, "not_found")
    return r


@router.get("/v1/jobs/{job_id}")
async def get_job(job_id: str, request: Request, response: Response,
                  principal: Principal = require("job:read")) -> dict[str, Any]:
    r = await load_job_row(request, principal, job_id)
    db = request.app.state.db
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        _, _, costs = await _side_tables(request, conn, [job_id])
        src = (await conn.execute(text("SELECT request -> 'source' AS s FROM intake.job_requests WHERE job_id = :j"),
                                  {"j": job_id})).first()
        live = None
        if await db.has_relation("jobs_read.job_runs"):
            lr = (await conn.execute(text("SELECT run_id, node_key, phase, started_at FROM jobs_read.job_runs WHERE "
                                          "job_id = :j AND phase IN ('Pending', 'Provisioning', 'Running', 'Finalizing') "
                                          "ORDER BY started_at DESC NULLS LAST LIMIT 1"), {"j": job_id})).first()
            if lr is not None:
                live = {"run_id": lr.run_id, "node_key": lr.node_key, "phase": lr.phase,
                        **({"started_at": iso(lr.started_at)} if lr.started_at else {})}
    response.headers["X-Replicant-Projection-Seq"] = str(r.seq)
    lag = 0 if r.status in TERMINAL else max(0, int((dt.datetime.now(dt.UTC) - r.as_of).total_seconds()))
    response.headers["X-Replicant-Projection-Lag-Seconds"] = str(lag)
    out: dict[str, Any] = {"snapshot": r.snapshot, "cost": {"usd_so_far": costs.get(job_id, 0.0)}, "links": {}}
    if live:
        out["live_run"] = live
    if src is not None and src.s:
        out["source"] = {"channel": src.s.get("channel"), **({"url": src.s["url"]} if src.s.get("url") else {})}
    return out


@router.get("/v1/jobs/{job_id}/timeline")
async def get_timeline(job_id: str, request: Request, principal: Principal = require("job:read"),
                       after_seq: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    r = await load_job_row(request, principal, job_id)
    async with request.app.state.db.tx(tenant=r.tenant_id) as conn:
        rows = (await conn.execute(text("SELECT seq, type, at, summary, actor, refs FROM jobs_read.job_timeline WHERE "
                                        "job_id = :j AND seq > :a ORDER BY seq LIMIT :n"),
                                   {"j": job_id, "a": after_seq, "n": limit})).all()
    items = []
    for t in rows:
        it = {"seq": t.seq, "type": t.type, "at": iso(t.at), "summary": t.summary}
        if t.actor:
            it["actor"] = t.actor
        if t.refs:
            it["refs"] = t.refs
        items.append(it)
    return {"items": items}


def _run_row(r: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"run_id": r.run_id, "node_key": r.node_key, "role": r.role, "phase": r.phase,
                           "cycle": r.cycle, "ordinal": r.ordinal}
    for k in ("reason", "isolation", "result_outcome", "qa_verdict"):
        v = getattr(r, k, None)
        if v is not None:
            out[k] = v
    for k in ("started_at", "finished_at"):
        v = getattr(r, k, None)
        if v is not None:
            out[k] = iso(v)
    return out


@router.get("/v1/jobs/{job_id}/runs")
async def list_runs(job_id: str, request: Request, principal: Principal = require("job:read")) -> dict[str, Any]:
    """RunRow = RunView plus run-result outcome and cost (from jobs_read.job_runs; falls back to the snapshot)."""
    r = await load_job_row(request, principal, job_id)
    db = request.app.state.db
    items: list[dict[str, Any]] = []
    if await db.has_relation("jobs_read.job_runs"):
        async with db.tx(scope=OPERATOR_SCOPE) as conn:
            rows = (await conn.execute(text("SELECT * FROM jobs_read.job_runs WHERE job_id = :j "
                                            "ORDER BY started_at NULLS LAST, seq"), {"j": job_id})).all()
        items = [_run_row(x) for x in rows]
    if not items:
        for x in (r.snapshot or {}).get("runs", []):
            items.append({k: v for k, v in x.items() if k not in ("result_uri", "artifact_manifest_uri")})
        items.sort(key=lambda x: x.get("started_at") or "")
    return {"items": items}


@router.get("/v1/jobs/{job_id}/artifacts")
async def list_artifacts(job_id: str, request: Request, principal: Principal = require("job:read")) -> dict[str, Any]:
    await load_job_row(request, principal, job_id)
    db = request.app.state.db
    if not await db.has_relation("jobs_read.job_artifacts"):
        return {"items": []}
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        rows = (await conn.execute(text("SELECT * FROM jobs_read.job_artifacts WHERE job_id = :j "
                                        "ORDER BY created_at, artifact_id"), {"j": job_id})).all()
    items = []
    for a in rows:
        it = {"artifact_id": a.artifact_id, "type": a.type, "name": a.name, "bucket_class": a.bucket_class,
              "size_bytes": int(a.size_bytes or 0), "created_at": iso(a.created_at)}
        for k in ("run_id", "content_type", "sha256", "redacted"):
            if getattr(a, k) is not None:
                it[k] = getattr(a, k)
        items.append(it)
    return {"items": items}


# ------------------------------------------------------------------------------------------------ job control
CONTROL_REJECTION_HTTP = {"WorkflowNotFound": (410, "job_closed"), "InvalidPayload": (422, "invalid_payload"),
                          "NotAuthorized": (403, "not_authorized")}


async def _control(request: Request, response: Response, principal: Principal, job_id: str, action: str,
                   body: dict[str, Any], idempotency_key: str | None) -> dict[str, Any]:
    """`job.cancel|job.pause|job.resume` as Updates through the outbox (C-MSG §2.1, §3; Update ID = action_id).
    Sender pre-check: job:control:any on the tenant, or job:control:own for the requester. The workflow re-checks."""
    from control_plane_api.routes_gates import now_iso
    allowed = {"action_id", "reason", "pause_mode"}
    if set(body) - allowed or not isinstance(body.get("action_id"), str):
        raise ProblemError(422, "invalid_job_control")
    r = await load_job_row(request, principal, job_id)
    auth = request.app.state.auth
    repo_ids = tuple(r.repo_ids or ())
    if not (check(auth, principal, "job:control:any", tenant_id=r.tenant_id, repo_ids=repo_ids) or
            (principal.principal_id == r.requester and
             check(auth, principal, "job:control:own", tenant_id=r.tenant_id, repo_ids=repo_ids))):
        raise ProblemError(403, "not_authorized", detail="needs job:control:any, or job:control:own as the requester")
    if not principal.principal_id.startswith("usr_"):
        raise ProblemError(403, "actor_not_human")
    req: dict[str, Any] = {"schema": "replicant.msg.job-control/v1", "action_id": body["action_id"], "action": action,
                           "actor": {"principal_id": principal.principal_id}, "channel": "ui",
                           "requested_at": now_iso()}
    if principal.login:
        req["actor"]["channel_identity"] = {"kind": "okta", "id": principal.login, "login": principal.login}
    for k in ("reason", "pause_mode"):
        if body.get(k):
            req[k] = body[k]
    if action == "pause" and "pause_mode" not in req:
        req["pause_mode"] = "at_next_node"
    from control_plane_api import contracts
    errs = contracts.errors("workflow-messages", req, "JobControlRequest")
    if errs:
        raise ProblemError(422, "invalid_payload", errors=errs)
    if r.status in TERMINAL:
        response.status_code = 200
        return {"schema": "replicant.msg.job-control-result/v1", "outcome": "no_op", "action_id": body["action_id"],
                "status": r.status}
    res = await request.app.state.outbox.submit(action_ref=body["action_id"], kind=f"job.{action}", workflow_id=job_id,
                                                tenant_id=r.tenant_id, actor=principal.principal_id, payload=req)
    if res.state == "done":
        response.status_code = 200
        return res.result or {}
    if res.state == "recording":
        response.status_code = 202
        return {"state": "recording", "action_ref": body["action_id"]}
    status, code = CONTROL_REJECTION_HTTP.get(res.error_type or "", (409, "control_rejected"))
    raise ProblemError(status, code, detail=(res.message or "")[:300], rejection_type=res.error_type)


@router.post("/v1/jobs/{job_id}:cancel")
async def cancel_job(job_id: str, request: Request, response: Response, body: dict[str, Any] = Body(...),
                     idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=200),
                     principal: Principal = require("job:control:own|job:control:any")) -> dict[str, Any]:
    return await _control(request, response, principal, job_id, "cancel", body, idempotency_key)


@router.post("/v1/jobs/{job_id}:pause")
async def pause_job(job_id: str, request: Request, response: Response, body: dict[str, Any] = Body(...),
                    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=200),
                    principal: Principal = require("job:control:own|job:control:any")) -> dict[str, Any]:
    return await _control(request, response, principal, job_id, "pause", body, idempotency_key)


@router.post("/v1/jobs/{job_id}:resume")
async def resume_job(job_id: str, request: Request, response: Response, body: dict[str, Any] = Body(...),
                     idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=200),
                     principal: Principal = require("job:control:own|job:control:any")) -> dict[str, Any]:
    return await _control(request, response, principal, job_id, "resume", body, idempotency_key)


@router.post("/v1/jobs/{job_id}/steering")
async def steer_job(job_id: str, request: Request, response: Response, body: dict[str, Any] = Body(...),
                    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=200),
                    principal: Principal = require("job:control:own|job:control:any")) -> dict[str, Any]:
    """Update `job.steer` through the outbox (Update ID = message_id)."""
    from control_plane_api import contracts
    from control_plane_api.routes_gates import now_iso
    if set(body) - {"message_id", "text", "delivery"} or not isinstance(body.get("message_id"), str) or \
            not isinstance(body.get("text"), str):
        raise ProblemError(422, "invalid_steering")
    r = await load_job_row(request, principal, job_id)
    auth = request.app.state.auth
    repo_ids = tuple(r.repo_ids or ())
    if not (check(auth, principal, "job:control:any", tenant_id=r.tenant_id, repo_ids=repo_ids) or
            (principal.principal_id == r.requester and
             check(auth, principal, "job:control:own", tenant_id=r.tenant_id, repo_ids=repo_ids))):
        raise ProblemError(403, "not_authorized", detail="needs job:control:any, or job:control:own as the requester")
    if r.status in TERMINAL:
        raise ProblemError(409, "job_terminal", detail=f"the job already finished ({r.status})")
    req: dict[str, Any] = {"schema": "replicant.msg.job-steer/v1", "message_id": body["message_id"],
                           "actor": {"principal_id": principal.principal_id}, "channel": "ui", "text": body["text"],
                           "delivery": body.get("delivery") or "auto", "sent_at": now_iso()}
    errs = contracts.errors("workflow-messages", req, "JobSteerRequest")
    if errs:
        raise ProblemError(422, "invalid_payload", errors=errs)
    res = await request.app.state.outbox.submit(action_ref=body["message_id"], kind="job.steer", workflow_id=job_id,
                                                tenant_id=r.tenant_id, actor=principal.principal_id, payload=req)
    if res.state == "done":
        response.status_code = 200
        return res.result or {}
    if res.state == "recording":
        response.status_code = 202
        return {"state": "recording", "action_ref": body["message_id"]}
    status, code = CONTROL_REJECTION_HTTP.get(res.error_type or "", (409, "steer_rejected"))
    raise ProblemError(status, code, detail=(res.message or "")[:300], rejection_type=res.error_type)
