"""Approvals: inbox, gate detail and decisions as Update `gate.decide` through the outbox (C-MSG §2.1, §3).

Sender-side checks are pre-checks for fast feedback (C-MSG §3 rule 3); the workflow's validators (V1-V12) and the
handler's `identity.check_decision` are authoritative. Pre-checks here: DecisionInput shape (incl. its if/then rules),
GateDecideRequest against C-MSG, gate exists and is open in the projection, presented digest (V6), channel (V7),
actor is a human (V8), self-approval (V9), rerun_qa only on publish_approval (V5), and `gate:decide` on the tenant.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Body, Header, Query, Request, Response
from sqlalchemy import text

from control_plane_api import contracts
from control_plane_api.auth import check, require
from control_plane_api.authz import Principal
from control_plane_api.db import OPERATOR_SCOPE
from control_plane_api.ids import derived
from control_plane_api.problems import ProblemError

router = APIRouter()

DECISIONS_BY_KIND = {
    "plan_approval": {"approve", "reject", "request_changes"},
    "publish_approval": {"approve", "reject", "request_changes", "rerun_qa"},
    "clarification": {"answer", "reject"},
    "budget_extension": {"approve", "reject"},
    "bundle_migration": {"approve", "reject"},
    "leak_purge": {"approve", "reject"},
}
REJECTION_HTTP = {
    "UnknownGate": (404, "unknown_gate"), "GateAlreadyDecided": (409, "gate_already_decided"),
    "GateClosed": (410, "gate_closed"), "DecisionNotAllowed": (409, "decision_not_allowed"),
    "DecisionNotOffered": (409, "decision_not_offered"), "StaleApprovalView": (409, "stale_approval_view"),
    "ChannelNotAllowed": (403, "channel_not_allowed"), "ActorNotHuman": (403, "actor_not_human"),
    "SelfApprovalForbidden": (403, "self_approval_forbidden"), "InvalidPayload": (422, "invalid_payload"),
    "GateBlockedByRelease": (409, "gate_blocked_by_release"), "DuplicateApprover": (409, "duplicate_approver"),
    "InitiatorCannotApprove": (403, "initiator_cannot_approve"), "WorkflowNotFound": (410, "job_closed"),
}


def _iso(v: Any) -> Any:
    return v.isoformat().replace("+00:00", "Z") if isinstance(v, dt.datetime) else v


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _load_gate(request: Request, gate_id: str) -> Any:
    db = request.app.state.db
    if not await db.has_relation("approvals.gates"):
        raise ProblemError(404, "not_found")
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        g = (await conn.execute(text("SELECT g.*, j.repo_ids, j.snapshot -> 'gates' AS snap_gates FROM approvals.gates g "
                                     "LEFT JOIN jobs_read.jobs j USING (job_id) WHERE g.gate_id = :g"),
                                {"g": gate_id})).first()
    return g


def _gate_view(g: Any) -> dict[str, Any]:
    for gv in g.snap_gates or []:
        if gv.get("gate_id") == g.gate_id:
            return gv
    gv: dict[str, Any] = {"gate_id": g.gate_id, "kind": g.kind, "state": g.state, "opened_at": _iso(g.opened_at),
                          "presented_digest": g.presented_digest, "packet_uri": g.packet_key, "channels": list(g.channels)}
    if g.expires_at:
        gv["expires_at"] = _iso(g.expires_at)
    return gv


async def approver_group_ok(request: Request, principal: Principal, g: Any) -> bool:
    """CONFIG (live): the project's approver groups, when set, narrow who may decide its gates."""
    from control_plane_api.projects import approver_ok
    repos = tuple(g.repo_ids or ())
    eff = await request.app.state.live_config.for_repo(repos[0]) if repos else None
    return approver_ok(principal, eff[2] if eff else None)


def _precheck(principal: Principal, g: Any, decision: str, presented_digest: str, channel: str) -> None:
    if g.state != "open":
        raise ProblemError(409, "gate_already_decided", winning_decision=g.winning_decision)
    if not principal.principal_id.startswith("usr_"):
        raise ProblemError(403, "actor_not_human")
    if decision not in DECISIONS_BY_KIND.get(g.kind, set()):
        raise ProblemError(409, "decision_not_allowed" if decision != "rerun_qa" else "decision_not_offered")
    if presented_digest != g.presented_digest:
        raise ProblemError(409, "stale_approval_view", current_digest=g.presented_digest)
    if channel not in (g.channels or []):
        raise ProblemError(403, "channel_not_allowed")
    if not g.requester_may_decide and principal.principal_id == g.requester:
        raise ProblemError(403, "self_approval_forbidden")


def _respond(response: Response, res: Any) -> dict[str, Any]:
    if res.state == "done":
        response.status_code = 200
        return res.result
    if res.state == "recording":
        response.status_code = 202
        return {"state": "recording", "action_ref": res.action_ref}
    status, code = REJECTION_HTTP.get(res.error_type or "", (409, "decision_rejected"))
    extra: dict[str, Any] = {"rejection_type": res.error_type}
    if res.details:
        extra["details"] = res.details
    raise ProblemError(status, code, detail=(res.message or "")[:300], **extra)


async def _send(request: Request, *, g: Any, req: dict[str, Any], actor: str) -> Any:
    errs = contracts.errors("workflow-messages", req, "GateDecideRequest")
    if errs:
        raise ProblemError(422, "invalid_payload", errors=errs)
    res = await request.app.state.outbox.submit(action_ref=req["decision_id"], kind="gate.decide",
                                                workflow_id=g.job_id, tenant_id=g.tenant_id, actor=actor, payload=req)
    res.action_ref = req["decision_id"]
    return res


@router.post("/v1/gates/{gate_id}/decisions")
async def decide_gate(gate_id: str, request: Request, response: Response, body: dict[str, Any] = Body(...),
                      idempotency_key: str = Header(..., alias="Idempotency-Key", max_length=200),
                      x_replicant_client: str | None = Header(None, alias="X-Replicant-Client"),
                      principal: Principal = require("gate:decide")) -> dict[str, Any]:
    allowed = {"decision", "presented_digest", "comment", "answer_text", "override_release_block"}
    if set(body) - allowed or "decision" not in body or "presented_digest" not in body:
        raise ProblemError(422, "invalid_decision_input")
    decision = body["decision"]
    if decision == "answer" and not body.get("answer_text"):
        raise ProblemError(422, "answer_text_required")
    if decision in ("reject", "request_changes") and not body.get("comment"):
        raise ProblemError(422, "comment_required")
    g = await _load_gate(request, gate_id)
    if g is None or not check(request.app.state.auth, principal, "job:read", tenant_id=g.tenant_id):
        raise ProblemError(404, "not_found")
    if not check(request.app.state.auth, principal, "gate:decide", tenant_id=g.tenant_id,
                 repo_ids=tuple(g.repo_ids or ())) or not await approver_group_ok(request, principal, g):
        raise ProblemError(403, "not_in_approver_group")
    channel = "cli" if (x_replicant_client or "").lower() == "cli" else "ui"
    idem = idempotency_key.strip().strip('"')
    decision_id = derived("dec", channel, gate_id, principal.principal_id, idem)
    # A re-submission with the same Idempotency-Key returns the original outcome (outbox insert-or-get).
    req: dict[str, Any] = {"schema": "replicant.msg.gate-decide/v1", "decision_id": decision_id, "gate_id": gate_id,
                           "decision": decision, "actor": {"principal_id": principal.principal_id}, "channel": channel,
                           "presented_digest": body["presented_digest"], "decided_at": now_iso()}
    if principal.login:
        req["actor"]["channel_identity"] = {"kind": "okta", "id": principal.login, "login": principal.login}
    for k in ("comment", "answer_text", "override_release_block"):
        if k in body:
            req[k] = body[k]
    existing = await _existing(request, decision_id, g.tenant_id)
    if existing is None:
        _precheck(principal, g, decision, body["presented_digest"], channel)
    return _respond(response, await _send(request, g=g, req=req, actor=principal.principal_id))


async def _existing(request: Request, action_ref: str, tenant: str) -> Any:
    async with request.app.state.db.tx(role="svc_cpa_intake", tenant=tenant) as conn:
        return (await conn.execute(text("SELECT state FROM intake.pending_actions WHERE action_ref = :a"),
                                   {"a": action_ref})).first()


@router.post("/internal/v1/gates/{gate_id}/decisions")
async def adapter_decide_gate(gate_id: str, request: Request, response: Response, body: dict[str, Any] = Body(...),
                              idempotency_key: str = Header(..., alias="Idempotency-Key", max_length=200),
                              principal: Principal = require("service:slack-integration|service:github-integration")
                              ) -> dict[str, Any]:
    errs = contracts.errors("workflow-messages", body, "GateDecideRequest")
    if errs or body.get("gate_id") != gate_id:
        raise ProblemError(422, "invalid_payload", errors=errs)
    want = {"svc_slack-integration": {"slack"}, "svc_github-integration": {"github", "gitea"}}
    if body["channel"] not in want.get(principal.principal_id, set()):
        raise ProblemError(403, "channel_not_allowed")
    g = await _load_gate(request, gate_id)
    if g is None:
        raise ProblemError(404, "unknown_gate")
    actor = request.app.state.auth.directory.get(body["actor"]["principal_id"])
    if actor is None or not actor.is_user:
        raise ProblemError(403, "identity_unlinked")
    existing = await _existing(request, body["decision_id"], g.tenant_id)
    # Same authorization as the UI route (NOTIFY phase 2): the linked user must hold gate:decide on the tenant/repo;
    # _precheck adds separation of duties (requester_may_decide), the digest, the channel and the gate state.
    if existing is None and (not check(request.app.state.auth, actor, "gate:decide", tenant_id=g.tenant_id,
                                       repo_ids=tuple(g.repo_ids or ()))
                             or not await approver_group_ok(request, request.app.state.auth.effective(actor), g)):
        raise ProblemError(403, "not_in_approver_group")
    if existing is None:
        _precheck(actor, g, body["decision"], body["presented_digest"], body["channel"])
    return _respond(response, await _send(request, g=g, req=body, actor=actor.principal_id))


@router.get("/v1/gates/{gate_id}")
async def get_gate(gate_id: str, request: Request, principal: Principal = require("job:read")) -> dict[str, Any]:
    g = await _load_gate(request, gate_id)
    if g is None or not check(request.app.state.auth, principal, "job:read", tenant_id=g.tenant_id,
                              repo_ids=tuple(g.repo_ids or ())):
        raise ProblemError(404, "not_found")
    decidable, reason = True, None
    if g.state != "open":
        decidable, reason = False, "gate_not_open"
    elif not check(request.app.state.auth, principal, "gate:decide", tenant_id=g.tenant_id,
                   repo_ids=tuple(g.repo_ids or ())) or not await approver_group_ok(request, principal, g):
        decidable, reason = False, "not_in_approver_group"
    elif not g.requester_may_decide and principal.principal_id == g.requester:
        decidable, reason = False, "self_approval_forbidden"
    packet: dict[str, Any] = {"packet_key": g.packet_key, "packet_revision": g.packet_revision,
                              "presented_digest": g.presented_digest}
    packet.update(await resolve_packet(request, g))
    out: dict[str, Any] = {"gate": _gate_view(g), "packet": packet, "decidable": decidable}
    if reason:
        out["not_decidable_reason"] = reason
    if g.suggested_approver:
        out["suggested_approver"] = g.suggested_approver
    return out


# ------------------------------------------------------------------------------------------------ packet resolution
# GateDetail.packet is the "resolved approval_packet with presigned links for plan, diff, QA". M1 (WEB-02): S3 is not
# reachable from browsers locally, so the small JSON documents the packet references are inlined (`resolved`) and the
# binary diff bundle is served same-origin by `GET /v1/gates/{gate_id}/packet/refs/{ref}` instead of a presigned URL.
INLINE_REFS = {"plan_uri": "plan", "latest_qa_report_uri": "qa_report", "review_report_uri": "review_report",
               "verification_report_uri": "verification_report", "latest_handoff_uri": "handoff"}
BINARY_REFS = {"latest_diff_bundle_uri": "diff_bundle"}
MAX_INLINE_BYTES = 512 * 1024
MAX_BINARY_BYTES = 16 * 1024 * 1024


def split_s3_uri(uri: str) -> tuple[str, str] | None:
    if not isinstance(uri, str) or not uri.startswith("s3://"):
        return None
    bucket, _, key = uri[5:].partition("/")
    return (bucket, key) if bucket and key else None


def ref_allowed(settings: Any, g: Any, uri: str) -> tuple[str, str] | None:
    """Only objects of this gate's job, in the artifacts bucket (no arbitrary reads through the proxy)."""
    bk = split_s3_uri(uri)
    if bk is None:
        return None
    bucket, key = bk
    if bucket != settings.bucket("artifacts") or not key.startswith(f"tenants/{g.tenant_id}/jobs/{g.job_id}/") or \
            ".." in key:
        return None
    return bk


async def _s3_get(request: Request, bucket: str, key: str, limit: int) -> bytes | None:
    import asyncio

    def _get() -> bytes | None:
        s3 = request.app.state.s3.client
        head = s3.head_object(Bucket=bucket, Key=key)
        if int(head.get("ContentLength") or 0) > limit:
            return None
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        return await asyncio.to_thread(_get)
    except Exception:  # noqa: BLE001 - missing objects / S3 down: the packet renders without that section
        return None


async def resolve_packet(request: Request, g: Any) -> dict[str, Any]:
    import json
    settings = request.app.state.settings
    if not settings.s3_endpoint or not g.packet_key:
        return {}
    raw = await _s3_get(request, settings.bucket("artifacts"), g.packet_key, MAX_INLINE_BYTES)
    if raw is None:
        return {"resolve_error": "packet_unavailable"}
    try:
        doc = json.loads(raw)
    except ValueError:
        return {"resolve_error": "packet_unreadable"}
    out: dict[str, Any] = {"content": doc, "resolved": {}, "links": {}}
    refs = doc.get("refs") or {}
    for ref, name in INLINE_REFS.items():
        bk = ref_allowed(settings, g, refs.get(ref, ""))
        if bk is None:
            continue
        body = await _s3_get(request, bk[0], bk[1], MAX_INLINE_BYTES)
        if body is None:
            continue
        try:
            out["resolved"][name] = json.loads(body)
        except ValueError:
            pass
    for ref, name in BINARY_REFS.items():
        if ref_allowed(settings, g, refs.get(ref, "")) is not None:
            out["links"][name] = f"/v1/gates/{g.gate_id}/packet/refs/{name}"
    return out


@router.get("/v1/gates/{gate_id}/packet/refs/{ref_name}")
async def get_packet_ref(gate_id: str, ref_name: str, request: Request,
                         principal: Principal = require("job:read")) -> Response:
    import json
    g = await _load_gate(request, gate_id)
    if g is None or not check(request.app.state.auth, principal, "job:read", tenant_id=g.tenant_id,
                              repo_ids=tuple(g.repo_ids or ())):
        raise ProblemError(404, "not_found")
    names = {v: k for k, v in {**INLINE_REFS, **BINARY_REFS}.items()}
    if ref_name not in names:
        raise ProblemError(404, "unknown_ref")
    settings = request.app.state.settings
    raw = await _s3_get(request, settings.bucket("artifacts"), g.packet_key or "", MAX_INLINE_BYTES)
    try:
        refs = (json.loads(raw) if raw else {}).get("refs") or {}
    except ValueError:
        refs = {}
    bk = ref_allowed(settings, g, refs.get(names[ref_name], ""))
    if bk is None:
        raise ProblemError(404, "ref_unavailable")
    body = await _s3_get(request, bk[0], bk[1], MAX_BINARY_BYTES)
    if body is None:
        raise ProblemError(404, "ref_unavailable")
    ctype = "application/zstd" if bk[1].endswith(".zst") else "application/json"
    return Response(content=body, media_type=ctype, headers={"Cache-Control": "private, max-age=300",
                                                             "X-Content-Type-Options": "nosniff"})


@router.get("/v1/approvals")
async def list_inbox(request: Request, principal: Principal = require("authenticated"), view: str = "mine",
                     gate_kind: str | None = None, risk: str | None = None, sort: str = "expiry",
                     limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    db = request.app.state.db
    if not await db.has_relation("approvals.gates"):
        return {"items": [], "counts": {"mine": 0, "teams": 0, "delegated": 0}}
    tenants = principal.tenants_with("gate:decide")
    if tenants is not None and not tenants:
        return {"items": [], "counts": {"mine": 0, "teams": 0, "delegated": 0}}
    where = ["g.state = 'open'" if view != "decided" else "g.state <> 'open'"]
    p: dict[str, Any] = {"lim": limit}
    if tenants is not None:
        where.append("g.tenant_id = ANY(:tenants)")
        p["tenants"] = sorted(tenants)
    if gate_kind:
        where.append("g.kind = ANY(:kinds)")
        p["kinds"] = gate_kind.split(",")
    if risk:
        where.append("g.risk_level = ANY(:risks)")
        p["risks"] = risk.split(",")
    order = {"expiry": "g.expires_at ASC NULLS LAST", "age": "g.opened_at ASC", "risk": "g.risk_level DESC",
             "repo": "g.repo"}.get(sort, "g.expires_at ASC NULLS LAST")
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        rows = (await conn.execute(text(f"SELECT g.* FROM approvals.gates g WHERE {' AND '.join(where)} "
                                        f"ORDER BY {order}, g.gate_id LIMIT :lim"), p)).all()
    items = []
    for g in rows:
        if g.requester == principal.principal_id and not g.requester_may_decide and view != "decided":
            continue
        it: dict[str, Any] = {"gate_id": g.gate_id, "job_id": g.job_id, "tenant_id": g.tenant_id, "kind": g.kind,
                              "title": g.title or "", "repo": g.repo or "", "requester": g.requester,
                              "risk_level": g.risk_level or "low", "opened_at": _iso(g.opened_at),
                              "channels": list(g.channels or []), "why": "eligible_group", "state": g.state,
                              "escalation_level": g.escalation_level, "batchable": g.batchable,
                              "approvals_recorded": g.approvals_recorded, "approvals_required": g.min_approvers}
        if g.expires_at:
            it["expires_at"] = _iso(g.expires_at)
        if g.winning_decision:
            it["decision"] = g.winning_decision
        if g.suggested_approver:
            it["suggested_approver"] = g.suggested_approver
        items.append(it)
    return {"items": items, "counts": {"mine": len(items) if view == "mine" else 0, "teams": 0, "delegated": 0},
            "next_cursor": None}
