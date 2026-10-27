"""Per-user notification endpoints: `GET/PUT /v1/me/notification-preferences` (control-plane-api.openapi.yaml,
schema notification-preferences v1) and `GET /v1/me/waiting` (v0 addition, DEVIATIONS NOTIFY-D4): what is waiting on
the caller, i.e. gates they can decide (the `/v1/approvals?view=mine` query) plus open PRs of non-terminal jobs where
they are the requester or a reviewer (approved the publish gate). Drives the UI's "Waiting on you" view and badge."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from fastapi import APIRouter, Body, Request
from sqlalchemy import text

from control_plane_api import contracts
from control_plane_api.auth import require
from control_plane_api.authz import Principal
from control_plane_api.db import OPERATOR_SCOPE
from control_plane_api.notify import NOTIFY_ROLE, _ts, utcnow
from control_plane_api.problems import ProblemError

router = APIRouter()


def _iso(v: Any) -> Any:
    return v.astimezone(dt.UTC).isoformat().replace("+00:00", "Z") if isinstance(v, dt.datetime) else v


def _user(principal: Principal) -> None:
    if not principal.is_user:
        raise ProblemError(403, "not_a_user", detail="notification preferences belong to users")


@router.get("/v1/me/notification-preferences")
async def get_prefs(request: Request, principal: Principal = require("authenticated")) -> dict[str, Any]:
    _user(principal)
    async with request.app.state.db.tx(scope=OPERATOR_SCOPE) as conn:
        r = (await conn.execute(text("SELECT version, prefs FROM notify.preferences WHERE principal_id = :p"),
                                {"p": principal.principal_id})).first()
    return {**(r.prefs if r else {}), "version": r.version if r else 1}


@router.put("/v1/me/notification-preferences")
async def put_prefs(request: Request, body: dict[str, Any] = Body(...),
                    principal: Principal = require("authenticated")) -> dict[str, Any]:
    _user(principal)
    doc = {k: v for k, v in body.items() if k != "version"}
    errs = contracts.errors("notification-preferences", doc)
    if errs:
        raise ProblemError(422, "invalid_preferences", errors=errs)
    for ek in ("gate.requested", "gate.escalated"):            # N-4: obligations cannot be silenced
        if (doc.get("channels") or {}).get(ek) == ["none"]:
            raise ProblemError(422, "obligation_not_mutable", detail=f"{ek} cannot be set to none")
    async with request.app.state.db.tx(role=NOTIFY_ROLE) as conn:
        r = (await conn.execute(text(
            "INSERT INTO notify.preferences (principal_id, prefs) VALUES (:p, CAST(:d AS jsonb)) ON CONFLICT "
            "(principal_id) DO UPDATE SET prefs = EXCLUDED.prefs, version = notify.preferences.version + 1, "
            "updated_at = now() RETURNING version"), {"p": principal.principal_id, "d": json.dumps(doc)})).first()
    return {**doc, "version": r.version}


@router.get("/v1/me/waiting")
async def waiting(request: Request, principal: Principal = require("authenticated")) -> dict[str, Any]:
    return await request.app.state.notify.waiting_doc(principal)


@router.get("/internal/v1/principals/{principal_id}/waiting")
async def waiting_for_principal(principal_id: str, request: Request,
                                principal: Principal = require("service:slack-integration")) -> dict[str, Any]:
    """The same list as `/v1/me/waiting` for a linked user, for slack-integration's App Home (NOTIFY-D15)."""
    who = request.app.state.auth.directory.get(principal_id)
    if who is None or not who.is_user:
        raise ProblemError(404, "unknown_principal")
    return await request.app.state.notify.waiting_doc(who)
