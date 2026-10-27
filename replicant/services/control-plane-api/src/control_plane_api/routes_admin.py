"""Admin operations that replace database access (D-26; architecture.md §7), the release assignment bootstrap
(WS-06 releases.openapi.yaml), and the internal gateway-quota lookup (architecture.md §2.10)."""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from fastapi import APIRouter, Body, Request, Response
from sqlalchemy import text

from control_plane_api import audit, contracts, registry
from control_plane_api.auth import require
from control_plane_api.authz import Principal
from control_plane_api.db import OPERATOR_SCOPE
from control_plane_api.problems import ProblemError

router = APIRouter()

TENANT_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
ENVS = ("local", "dev", "staging", "prod")


def _iso(v: Any) -> Any:
    return v.isoformat().replace("+00:00", "Z") if isinstance(v, dt.datetime) else v


def _num(v: Any) -> float | int:
    f = float(v)
    return int(f) if f.is_integer() else f


async def _caps(conn: Any) -> dict[str, float | int]:
    return {r.cap_name: _num(r.value) for r in (await conn.execute(text(
        "SELECT cap_name, value FROM tenancy.platform_caps ORDER BY cap_name"))).all()}


# ------------------------------------------------------------------------------------------------ platform caps
@router.get("/v1/admin/platform-caps")
async def get_platform_caps(request: Request, principal: Principal = require("admin:platform|dev:seed")) -> dict:
    async with request.app.state.db.tx() as conn:
        return await _caps(conn)


@router.put("/v1/admin/platform-caps")
async def put_platform_caps(request: Request, body: dict[str, Any] = Body(...),
                            principal: Principal = require("admin:platform|dev:seed")) -> dict:
    for k, v in body.items():
        if not re.match(r"^[a-z][a-z0-9_]{2,63}$", k) or not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            raise ProblemError(422, "invalid_cap", detail=k)
    db = request.app.state.db
    async with db.tx(role="svc_cpa_tenancy") as conn:
        for k, v in body.items():
            unit = "usd" if "usd" in k else ("runs" if "runs" in k else ("jobs" if "jobs" in k else "count"))
            await conn.execute(text(
                "INSERT INTO tenancy.platform_caps (cap_name, value, unit, updated_by) VALUES (:k, :v, :u, :by) "
                "ON CONFLICT (cap_name) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), "
                "updated_by = EXCLUDED.updated_by"), {"k": k, "v": v, "u": unit, "by": principal.principal_id})
        await audit.emit(conn, db, {"event_type": "admin.tenant_settings_changed", "outcome": "success",
                                    "target_kind": "platform_caps", "target_id": "platform",
                                    "actor_principal_id": principal.principal_id,
                                    "details": {"caps": sorted(body.keys())}})
        return await _caps(conn)


# ------------------------------------------------------------------------------------------------ tenants
def _tenant_out(r: Any) -> dict[str, Any]:
    st = r.status if r.status in ("active", "onboarding", "suspended", "offboarded") else "suspended"
    return {"tenant_id": r.tenant_id, "display_name": r.display_name, "status": st}


@router.post("/v1/admin/tenants", status_code=201)
async def create_tenant(request: Request, response: Response, body: dict[str, Any] = Body(...),
                        principal: Principal = require("admin:platform|dev:seed")) -> dict[str, Any]:
    allowed = {"tenant_id", "display_name", "okta_group_prefix"}
    tid, name = body.get("tenant_id"), body.get("display_name")
    if set(body) - allowed or not isinstance(tid, str) or not isinstance(name, str) or len(name) > 100:
        raise ProblemError(422, "invalid_tenant")
    if not TENANT_RE.match(tid) or not 3 <= len(tid) <= 32:
        raise ProblemError(422, "invalid_tenant_id")
    reserved_ok = {"replicant-eval", "replicant-system", "local-dev"}
    if tid not in reserved_ok and (tid.startswith(("sbx", "kube", "aws", "replicant-"))):
        raise ProblemError(422, "reserved_tenant_id")
    db = request.app.state.db
    async with db.tx(role="svc_cpa_tenancy", tenant=tid) as conn:
        ex = (await conn.execute(text("SELECT tenant_id, display_name, status FROM tenancy.tenants WHERE "
                                      "tenant_id = :t"), {"t": tid})).first()
        if ex is not None:
            if ex.display_name != name:
                raise ProblemError(409, "tenant_exists_different")
            response.status_code = 200
            return _tenant_out(ex)
        caps = await _caps(conn)
        status = "active" if request.app.state.settings.is_local else "onboarding"
        await conn.execute(text("INSERT INTO tenancy.tenants (tenant_id, display_name, status, okta_group_prefix, "
                                "kms_key_alias, created_by) VALUES (:t, :n, :s, :p, :k, :by)"),
                           {"t": tid, "n": name, "s": status, "p": body.get("okta_group_prefix"),
                            "k": f"alias/replicant/{request.app.state.settings.replicant_env}/tenant/{tid}",
                            "by": principal.principal_id})
        settings_doc = {"quotas": {"monthly_budget_usd": caps.get("tenant_monthly_usd_default", 100),
                                   "daily_budget_usd": caps.get("tenant_daily_usd_default", 10)}}
        await conn.execute(text("INSERT INTO tenancy.tenant_settings (tenant_id, version, settings, updated_by, reason) "
                                "VALUES (:t, 1, CAST(:s AS jsonb), :by, 'tenant created')"),
                           {"t": tid, "s": json.dumps(settings_doc), "by": principal.principal_id})
        await audit.emit(conn, db, {"event_type": "admin.tenant_settings_changed", "outcome": "success", "tenant_id": tid,
                                    "target_kind": "tenant", "target_id": tid, "details": {"version": 1},
                                    "actor_principal_id": principal.principal_id})
        r = (await conn.execute(text("SELECT tenant_id, display_name, status FROM tenancy.tenants WHERE tenant_id = :t"),
                                {"t": tid})).first()
        return _tenant_out(r)


@router.put("/v1/admin/tenants/{tenant_id}/settings")
async def put_tenant_settings(tenant_id: str, request: Request, body: dict[str, Any] = Body(...),
                              principal: Principal = require("admin:platform|dev:seed")) -> dict[str, Any]:
    doc = {k: v for k, v in body.items() if k != "version"}
    errs = contracts.errors("tenant-settings", doc)
    if errs:
        raise ProblemError(422, "invalid_tenant_settings", errors=errs)
    db = request.app.state.db
    async with db.tx(role="svc_cpa_tenancy", tenant=tenant_id) as conn:
        if (await conn.execute(text("SELECT 1 FROM tenancy.tenants WHERE tenant_id = :t"), {"t": tenant_id})).first() \
                is None:
            raise ProblemError(404, "unknown_tenant")
        caps = await _caps(conn)
        q = doc["quotas"]
        if q["monthly_budget_usd"] > caps.get("tenant_monthly_usd_max", float("inf")) or \
                q["daily_budget_usd"] > caps.get("tenant_daily_usd_max", float("inf")):
            raise ProblemError(422, "quota_above_platform_cap")
        cur = (await conn.execute(text("SELECT version, settings FROM tenancy.tenant_settings WHERE tenant_id = :t AND "
                                       "is_current FOR UPDATE"), {"t": tenant_id})).first()
        if cur is not None and cur.settings == doc:
            return {**doc, "version": cur.version}
        v = (cur.version + 1) if cur else 1
        if cur:
            await conn.execute(text("UPDATE tenancy.tenant_settings SET is_current = false WHERE tenant_id = :t AND "
                                    "is_current"), {"t": tenant_id})
        await conn.execute(text("INSERT INTO tenancy.tenant_settings (tenant_id, version, settings, updated_by) VALUES "
                                "(:t, :v, CAST(:s AS jsonb), :by)"),
                           {"t": tenant_id, "v": v, "s": json.dumps(doc), "by": principal.principal_id})
        await audit.emit(conn, db, {"event_type": "admin.tenant_settings_changed", "outcome": "success",
                                    "tenant_id": tenant_id, "actor_principal_id": principal.principal_id,
                                    "details": {"version": v}})
    return {**doc, "version": v}


# ------------------------------------------------------------------------------------------------ migrations
@router.get("/v1/admin/migrations")
async def migration_status(request: Request, principal: Principal = require("admin:platform|dev:seed")) -> dict:
    db = request.app.state.db
    out: dict[str, Any] = {"current_revision": "", "head_known_to_api": "", "compat_by_service": [], "last_runs": [],
                           "pending_contracts": []}
    async with db.tx() as conn:
        if await db.has_relation("ops.alembic_version"):
            out["current_revision"] = (await conn.execute(text("SELECT version_num FROM ops.alembic_version "
                                                               "LIMIT 1"))).scalar() or ""
        if await db.has_relation("ops.migration_runs"):
            for r in (await conn.execute(text("SELECT to_revision, started_at, finished_at, outcome FROM "
                                              "ops.migration_runs ORDER BY run_seq DESC LIMIT 20"))).all():
                out["last_runs"].append({"revision": r.to_revision or "", "started_at": _iso(r.started_at),
                                         **({"finished_at": _iso(r.finished_at)} if r.finished_at else {}),
                                         "outcome": r.outcome})
    out["head_known_to_api"] = out["current_revision"]
    return out


# ------------------------------------------------------------------------------------------------ role bindings
@router.put("/v1/admin/role-bindings")
async def put_role_bindings(request: Request, principal: Principal = require("admin:platform|dev:seed")) -> dict:
    raise ProblemError(501, "identity_service_absent_m1",
                       detail="M1 has no identity-service; roles come from the static dev directory")


@router.get("/v1/admin/role-bindings")
async def get_role_bindings(request: Request, principal: Principal = require("identity:admin")) -> dict:
    raise ProblemError(501, "identity_service_absent_m1")


# ------------------------------------------------------------------------------------------------ releases
@router.get("/v1/releases/{env}/assignment")
async def get_assignment(env: str, request: Request, response: Response,
                         principal: Principal = require("release:read|dev:seed")) -> dict[str, Any]:
    if env not in ENVS:
        raise ProblemError(404, "unknown_env")
    async with request.app.state.db.tx(role="svc_cpa_registry") as conn:
        a = await registry.get_assignment(conn, env)
    if a is None:
        raise ProblemError(404, "no_assignment")
    response.headers["ETag"] = f'"{a["version"]}"'
    return a


@router.post("/v1/releases/{env}/assignment:bootstrap", status_code=201)
async def bootstrap_assignment(env: str, request: Request, body: dict[str, Any] = Body(...),
                               principal: Principal = require("release:operate|dev:seed")) -> dict[str, Any]:
    if env not in ENVS:
        raise ProblemError(404, "unknown_env")
    if env != request.app.state.settings.replicant_env:
        raise ProblemError(409, "env_mismatch", detail="this control-plane-api serves another environment")
    if env in ("staging", "prod") and len(set(body.get("signoff_decision_ids") or [])) < 2:
        raise ProblemError(409, "signoffs_required")
    rb, reason = body.get("release_bundle_id"), body.get("reason")
    if not isinstance(rb, str) or not isinstance(reason, str) or not 3 <= len(reason) <= 2000:
        raise ProblemError(422, "invalid_bootstrap_request")
    db = request.app.state.db
    try:
        async with db.tx(role="svc_cpa_registry") as conn:
            if env == "local" and "dev:seed" in principal.permissions:
                # M1: no eval-worker, so the single-arm bootstrap evaluation cannot run; the local seeder may take a
                # `built` bundle through evaluating -> eval_passed (recorded as such; DEVIATIONS CONTROL-D3).
                st = (await conn.execute(text("SELECT promotion_state FROM registry.release_bundles WHERE "
                                              "release_bundle_id = :rb"), {"rb": rb})).scalar()
                if st == "built":
                    m1 = "M1 local seed: bootstrap evaluation skipped (no eval-worker)"
                    await registry.transition(conn, db, rb, "evaluating", principal.principal_id, m1, tier="T1")
                    await registry.transition(conn, db, rb, "eval_passed", principal.principal_id, m1, tier="T1",
                                              evidence={"m1_dev_seed": True})
            return await registry.bootstrap(conn, db, env, rb, principal.principal_id, reason)
    except registry.BootstrapConflict as e:
        raise ProblemError(409, str(e).split(":")[0], detail=str(e))


# ------------------------------------------------------------------------------------------------ internal
@router.get("/internal/v1/tenants/{tenant_id}/gateway-quota")
async def gateway_quota(tenant_id: str, request: Request,
                        principal: Principal = require("service:replicant-gateway-shim")) -> dict[str, Any]:
    async with request.app.state.db.tx(tenant=tenant_id) as conn:
        r = (await conn.execute(text("SELECT s.settings, s.version, s.updated_at FROM tenancy.tenant_settings s JOIN "
                                     "tenancy.tenants t USING (tenant_id) WHERE s.tenant_id = :t AND s.is_current AND "
                                     "t.status IN ('active', 'onboarding')"), {"t": tenant_id})).first()
        caps = await _caps(conn)
    if r is None:
        raise ProblemError(404, "tenant_quota_unset")
    q = r.settings.get("quotas") or {}
    return {"tenant_id": tenant_id, "contact_group": q.get("contact_group") or f"{tenant_id}-admins",
            "monthly_budget_usd": q["monthly_budget_usd"], "daily_budget_usd": q["daily_budget_usd"],
            "platform_daily_usd_max": caps.get("platform_daily_usd_max", 0),
            "platform_monthly_usd_max": caps.get("platform_monthly_usd_max", 0),
            "version": r.version, "as_of": _iso(r.updated_at)}


# ------------------------------------------------------------------------------------------------ tenant repos
async def _repo_tenant(request: Request, repo_id: str) -> str | None:
    """The tenant whose project holds `repo_id` (tenancy.project_repos), or None when no project claims it."""
    db = request.app.state.db
    if not await db.has_relation("tenancy.project_repos"):
        return None
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        return (await conn.execute(text("SELECT tenant_id FROM tenancy.project_repos WHERE repo_id = :r LIMIT 1"),
                                   {"r": repo_id})).scalar()


@router.put("/v1/tenants/{tenant_id}/github/repos/{repo_id}/settings")
async def put_repo_settings(tenant_id: str, repo_id: str, request: Request, body: dict[str, Any] = Body(...),
                            principal: Principal = require("tenant:ops:edit|dev:seed")) -> dict[str, Any]:
    """Operational repo trigger settings (github-app.md §7.1). github-integration is the writer of `github_app`
    (C-PG), so control-plane-api forwards after authorization. M1: Gitea only; the SCM host comes from
    `X-Replicant-Scm-Host` (default git.replicant.localhost:3300); the forward authenticates with the caller's dev seed
    token, so in M1 only the seeder can use this route (DEVIATIONS)."""
    import httpx

    from control_plane_api.auth import check
    if not (("dev:seed" in principal.permissions) or
            check(request.app.state.auth, principal, "tenant:ops:edit", tenant_id=tenant_id)):
        raise ProblemError(403, "missing_permission")
    if await _repo_tenant(request, repo_id) not in (None, tenant_id):
        raise ProblemError(404, "not_found")      # another tenant's repo (v0 SEC-D2): no existence leak
    if str(body.get("repo_id", repo_id)) != repo_id or not body.get("full_name"):
        raise ProblemError(422, "invalid_repo_settings", detail="repo_id must match the path; full_name is required")
    provider = request.headers.get("x-replicant-scm-provider", "gitea")
    host = request.headers.get("x-replicant-scm-host", "git.replicant.localhost:3300")
    if provider != "gitea":
        raise ProblemError(422, "unsupported_provider")
    import os
    url = os.environ.get("REPLICANT_GITHUB_INTEGRATION_URL", "http://github-integration:8080")
    try:
        async with httpx.AsyncClient(base_url=url, timeout=15) as c:
            r = await c.put("/internal/v1/repo-settings", json={"provider": provider, "host": host, "repo_id": repo_id,
                                                                "tenant_id": tenant_id, "settings": body},
                            headers={"Authorization": request.headers.get("authorization", "")})
    except httpx.HTTPError as e:
        raise ProblemError(503, "github_integration_unavailable", detail=type(e).__name__)
    if r.status_code >= 400:
        raise ProblemError(r.status_code if r.status_code < 500 else 502, "repo_settings_rejected", detail=r.text[:300])
    return r.json()


# ------------------------------------------------------------------------------------------------ notifications
@router.post("/internal/v1/notifications", status_code=202)
async def post_notification(request: Request, body: dict[str, Any] = Body(...),
                            principal: Principal = require("service:replicant-worker")) -> dict[str, Any]:
    """Notification intents (notifications.md N-1, XD-07-01): recipients, preferences and channels are resolved and
    `notify.deliveries` rows written (dedupe_key unique: activity retries are no-ops); the dispatcher pushes them to
    slack-integration / github-integration (notify_loops.py)."""
    if not isinstance(body.get("dedupe_key"), str) or not re.match(r"^[a-z]+(\.[a-z_]+)+$", str(body.get("event_key"))) \
            or not TENANT_RE.match(str(body.get("tenant_id", ""))):
        raise ProblemError(422, "invalid_notification")
    return await request.app.state.notify.accept_intent(body)
