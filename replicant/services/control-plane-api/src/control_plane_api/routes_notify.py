"""Per-user notification endpoints: `GET/PUT /v1/me/notification-preferences` (control-plane-api.openapi.yaml,
schema notification-preferences v1) and `GET /v1/me/waiting` (v0 addition, DEVIATIONS NOTIFY-D4): what is waiting on
the caller, i.e. gates they can decide (the `/v1/approvals?view=mine` query) plus open PRs of non-terminal jobs where
they are the requester or a reviewer (approved the publish gate). Drives the UI's "Waiting on you" view and badge."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from fastapi import APIRouter, Body, Request, Response
from sqlalchemy import text

from control_plane_api import contracts
from control_plane_api.auth import require
from control_plane_api.authz import Principal
from control_plane_api.db import OPERATOR_SCOPE
from control_plane_api.notify import NOTIFY_ROLE, _ts, utcnow
from control_plane_api.problems import ProblemError
from control_plane_api.routes_ui_prefs import parse_if_match

router = APIRouter()
NO_ROW = 1        # the version GET reports while nothing is stored (schema minimum 1)


def _iso(v: Any) -> Any:
    return v.astimezone(dt.UTC).isoformat().replace("+00:00", "Z") if isinstance(v, dt.datetime) else v


def _user(principal: Principal) -> None:
    if not principal.is_user:
        raise ProblemError(403, "not_a_user", detail="notification preferences belong to users")


@router.get("/v1/me/notification-preferences")
async def get_prefs(request: Request, response: Response, principal: Principal = require("authenticated")) -> dict[str, Any]:
    _user(principal)
    async with request.app.state.db.tx(scope=OPERATOR_SCOPE) as conn:
        r = (await conn.execute(text("SELECT version, prefs FROM notify.preferences WHERE principal_id = :p"),
                                {"p": principal.principal_id})).first()
    version = r.version if r else NO_ROW
    response.headers["ETag"] = f'"{version}"'
    return {**(r.prefs if r else {}), "version": version}


@router.put("/v1/me/notification-preferences")
async def put_prefs(request: Request, response: Response, body: dict[str, Any] = Body(...),
                    principal: Principal = require("authenticated")) -> dict[str, Any]:
    """Optimistic concurrency as `/v1/me/ui-preferences` (v0 SEC-D3): the version read comes back as `If-Match` or
    body `version` (If-Match wins); a stale one is 412 `version_mismatch` with `current_version`. Neither given:
    last-writer-wins (older clients). Version 1 means "nothing stored yet" (the schema's minimum, what GET returns
    without a row); every new row starts at version 2, so a client that read 1 cannot overwrite a row created after
    its read."""
    _user(principal)
    expected = parse_if_match(request.headers.get("if-match"))
    if expected is None and body.get("version") is not None:
        if not isinstance(body["version"], int) or isinstance(body["version"], bool) or body["version"] < 1:
            raise ProblemError(422, "invalid_preferences", detail="version must be a positive integer")
        expected = body["version"]
    doc = {k: v for k, v in body.items() if k != "version"}
    errs = contracts.errors("notification-preferences", doc)
    if errs:
        raise ProblemError(422, "invalid_preferences", errors=errs)
    for ek in ("gate.requested", "gate.escalated"):            # N-4: obligations cannot be silenced
        if (doc.get("channels") or {}).get(ek) == ["none"]:
            raise ProblemError(422, "obligation_not_mutable", detail=f"{ek} cannot be set to none")
    p = {"p": principal.principal_id, "d": json.dumps(doc)}
    async with request.app.state.db.tx(role=NOTIFY_ROLE) as conn:
        if expected is None:
            r = (await conn.execute(text(
                "INSERT INTO notify.preferences (principal_id, prefs, version) VALUES (:p, CAST(:d AS jsonb), 2) "
                "ON CONFLICT (principal_id) DO UPDATE SET prefs = EXCLUDED.prefs, "
                "version = notify.preferences.version + 1, updated_at = now() RETURNING version"), p)).first()
        else:
            r = (await conn.execute(text(
                "UPDATE notify.preferences SET prefs = CAST(:d AS jsonb), version = version + 1, updated_at = now() "
                "WHERE principal_id = :p AND version = :v RETURNING version"), {**p, "v": expected})).first()
            if r is None and expected == NO_ROW:     # rows written before SEC-D3 may still be at version 1
                r = (await conn.execute(text(
                    "INSERT INTO notify.preferences (principal_id, prefs, version) VALUES (:p, CAST(:d AS jsonb), 2) "
                    "ON CONFLICT (principal_id) DO NOTHING RETURNING version"), p)).first()
        if r is None:
            current = (await conn.execute(text("SELECT version FROM notify.preferences WHERE principal_id = :p"),
                                          {"p": principal.principal_id})).scalar() or NO_ROW
            raise ProblemError(412, "version_mismatch", detail=f"preferences are at version {current}",
                               headers={"ETag": f'"{current}"'}, current_version=current)
    response.headers["ETag"] = f'"{r.version}"'
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
