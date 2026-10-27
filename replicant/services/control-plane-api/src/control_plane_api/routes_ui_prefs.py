"""Per-user UI preferences: `GET/PUT /v1/me/ui-preferences` (v0 WEB-UIPREF, DEVIATIONS WEB-UIPREF-D1, schema
ui-preferences v1). Stored in web.ui_preferences, apart from notify.preferences (whose schema is closed). Today it
holds one key, `job_view` ("simple" | "detailed"); unset means "simple". The effective document is also returned
in `GET /v1/me` as `ui_preferences`.

Optimistic concurrency: the stored `version` comes back in the body and as a strong ETag. A PUT may carry the
version it read (`If-Match: "<n>"` or body `version`; If-Match wins); a stale one is refused with 412 and the
current version. Version 0 means "no row yet". A PUT with neither is a last-writer-wins upsert."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from control_plane_api import contracts
from control_plane_api.auth import require
from control_plane_api.authz import Principal
from control_plane_api.db import OPERATOR_SCOPE
from control_plane_api.notify import NOTIFY_ROLE
from control_plane_api.problems import ProblemError

router = APIRouter()

DEFAULTS: dict[str, Any] = {"job_view": "simple"}
WRITE_ROLE = NOTIFY_ROLE          # the per-user /v1/me writer module role; the migration grants it r_web_write


def effective(prefs: dict[str, Any] | None) -> dict[str, Any]:
    """Defaults overlaid with the stored keys the schema still knows (unknown or invalid keys fall back)."""
    out = dict(DEFAULTS)
    for k, v in (prefs or {}).items():
        if k in DEFAULTS and not contracts.errors("ui-preferences", {k: v}):
            out[k] = v
    return out


def parse_if_match(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    v = value.strip()
    if v.startswith("W/"):
        v = v[2:]
    v = v.strip('"')
    if not v.isdigit():
        raise ProblemError(400, "invalid_if_match", detail='If-Match must be a version, e.g. "3"')
    return int(v)


def _user(principal: Principal) -> None:
    if not principal.is_user:
        raise ProblemError(403, "not_a_user", detail="UI preferences belong to users")


async def load(request: Request, principal_id: str) -> tuple[int, dict[str, Any]]:
    async with request.app.state.db.tx(scope=OPERATOR_SCOPE) as conn:
        r = (await conn.execute(text("SELECT version, prefs FROM web.ui_preferences WHERE principal_id = :p"),
                                {"p": principal_id})).first()
    return (r.version, r.prefs) if r else (0, {})


async def for_me(request: Request, principal: Principal) -> dict[str, Any]:
    """The effective preferences for `GET /v1/me`. Never fails /v1/me: a missing table (migration not yet run)
    or a service principal gets the defaults."""
    if not principal.is_user:
        return dict(DEFAULTS)
    try:
        _, prefs = await load(request, principal.principal_id)
    except DBAPIError:
        return dict(DEFAULTS)
    return effective(prefs)


def _doc(version: int, prefs: dict[str, Any], response: Response) -> dict[str, Any]:
    response.headers["ETag"] = f'"{version}"'
    return {**effective(prefs), "version": version}


@router.get("/v1/me/ui-preferences")
async def get_ui_prefs(request: Request, response: Response,
                       principal: Principal = require("authenticated")) -> dict[str, Any]:
    _user(principal)
    version, prefs = await load(request, principal.principal_id)
    return _doc(version, prefs, response)


@router.put("/v1/me/ui-preferences")
async def put_ui_prefs(request: Request, response: Response, body: dict[str, Any] = Body(...),
                       principal: Principal = require("authenticated")) -> dict[str, Any]:
    _user(principal)
    errs = contracts.errors("ui-preferences", body)
    if errs:
        raise ProblemError(422, "invalid_preferences", errors=errs)
    expected = parse_if_match(request.headers.get("if-match"))
    if expected is None:
        expected = body.get("version")
    doc = {k: v for k, v in body.items() if k != "version"}
    p = {"p": principal.principal_id, "d": json.dumps(doc)}
    async with request.app.state.db.tx(role=WRITE_ROLE) as conn:
        if expected is None:
            r = (await conn.execute(text(
                "INSERT INTO web.ui_preferences (principal_id, prefs) VALUES (:p, CAST(:d AS jsonb)) ON CONFLICT "
                "(principal_id) DO UPDATE SET prefs = web.ui_preferences.prefs || EXCLUDED.prefs, "
                "version = web.ui_preferences.version + 1, updated_at = now() RETURNING version, prefs"), p)).first()
        elif expected == 0:
            r = (await conn.execute(text(
                "INSERT INTO web.ui_preferences (principal_id, prefs) VALUES (:p, CAST(:d AS jsonb)) "
                "ON CONFLICT (principal_id) DO NOTHING RETURNING version, prefs"), p)).first()
        else:
            r = (await conn.execute(text(
                "UPDATE web.ui_preferences SET prefs = prefs || CAST(:d AS jsonb), version = version + 1, "
                "updated_at = now() WHERE principal_id = :p AND version = :v RETURNING version, prefs"),
                {**p, "v": expected})).first()
        if r is None:
            cur = (await conn.execute(text("SELECT version FROM web.ui_preferences WHERE principal_id = :p"),
                                      {"p": principal.principal_id})).scalar()
            current = cur or 0
            raise ProblemError(412, "version_mismatch", detail=f"preferences are at version {current}",
                               headers={"ETag": f'"{current}"'}, current_version=current)
    return _doc(r.version, r.prefs, response)
