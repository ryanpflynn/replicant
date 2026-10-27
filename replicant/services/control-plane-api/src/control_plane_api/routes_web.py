"""Read endpoints the management UI (control-plane-web) needs that M1 did not have: `GET /v1/me`, `GET /v1/tenants`,
`GET /v1/releases/bundles`, `GET /v1/releases/workers` and `GET /v1/admin/kill-switches`
(control-plane-api.openapi.yaml). Read-only; shapes follow the OpenAPI schemas `Me`, `Tenant`, `BundleRow`,
`WorkerVersionRow` and `KillSwitch`. M1 simplifications are listed in replicant/docs/v0/DEVIATIONS.md (WEB-*)."""
from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import text

from control_plane_api import routes_ui_prefs
from control_plane_api.auth import require
from control_plane_api.authz import PLATFORM_ONLY, ROLE_PERMISSIONS, Principal
from control_plane_api.db import OPERATOR_SCOPE

router = APIRouter()

TENANT_ROLES = ("viewer", "engineer", "approver", "tenant_admin")
ADMIN_GROUP = "replicant-admins"          # D-25: memory:admin = membership in replicant-admins at request time
MODES = ("intake", "starts", "runs", "publishing")


def _iso(v: Any) -> Any:
    return v.isoformat().replace("+00:00", "Z") if isinstance(v, dt.datetime) else v


def capabilities(p: Principal) -> list[str]:
    """Platform-level permissions the UI may reveal navigation for (OpenAPI `Me.capabilities`; UX only)."""
    caps: set[str] = {perm for r in p.platform_roles for perm in ROLE_PERMISSIONS.get(r, ()) if perm in PLATFORM_ONLY}
    caps |= {perm for perm in p.permissions if perm in PLATFORM_ONLY}
    platform_perms = {perm for r in p.platform_roles for perm in ROLE_PERMISSIONS.get(r, ())}
    if "ops:kill-switch" in platform_perms:
        caps.add("ops:kill-switch")
    if "identity:admin" in platform_perms:      # CONFIG: identity:admin is also tenant/project scoped now
        caps.add("identity:admin")
    if ADMIN_GROUP in p.groups:
        caps.add("memory:admin")
    return sorted(caps)


def display_name(p: Principal) -> str:
    name = p.login or p.principal_id
    return name[:1].upper() + name[1:] if p.is_user else name


def me_doc(p: Principal, all_tenants: list[str]) -> dict[str, Any]:
    tenants: list[dict[str, Any]] = []
    star = [r for r in p.tenant_roles.get("*", ()) if r in TENANT_ROLES]
    for t in all_tenants:
        roles = sorted({*star, *(r for r in p.tenant_roles.get(t, ()) if r in TENANT_ROLES)})
        if roles:
            tenants.append({"tenant_id": t, "roles": roles})
    out: dict[str, Any] = {"principal_id": p.principal_id, "display_name": display_name(p), "tenants": tenants,
                           "capabilities": capabilities(p), "links": {}}
    if p.login:
        out["login"] = p.login
    if p.groups:
        out["groups"] = list(p.groups)
    if p.platform_roles:
        out["platform_roles"] = list(p.platform_roles)
    return out


async def _tenant_rows(request: Request) -> list[Any]:
    async with request.app.state.db.tx(scope=OPERATOR_SCOPE) as conn:
        return (await conn.execute(text("SELECT tenant_id, display_name, status FROM tenancy.tenants "
                                        "WHERE status <> 'offboarded' ORDER BY tenant_id"))).all()


@router.get("/v1/me")
async def get_me(request: Request, principal: Principal = require("authenticated")) -> dict[str, Any]:
    rows = await _tenant_rows(request)
    doc = me_doc(principal, [r.tenant_id for r in rows])
    doc["ui_preferences"] = await routes_ui_prefs.for_me(request, principal)   # WEB-UIPREF: effective, default simple
    from control_plane_api.routes_projects import visible_projects
    doc["projects"] = await visible_projects(request, principal)             # CONFIG-01: per-project permissions
    return doc


@router.get("/v1/tenants")
async def list_tenants(request: Request, principal: Principal = require("authenticated")) -> dict[str, Any]:
    rows = await _tenant_rows(request)
    visible = principal.tenants_with("job:read")
    items = []
    for r in rows:
        if visible is not None and r.tenant_id not in visible:
            continue
        st = r.status if r.status in ("active", "onboarding", "suspended", "offboarded") else "suspended"
        items.append({"tenant_id": r.tenant_id, "display_name": r.display_name, "status": st})
    return {"items": items}


# ------------------------------------------------------------------------------------------------ releases (read)
@router.get("/v1/releases/bundles")
async def list_bundles(request: Request, principal: Principal = require("release:read|dev:seed"),
                       include_derived: bool = False) -> dict[str, Any]:
    env = request.app.state.settings.replicant_env
    async with request.app.state.db.tx(role="svc_cpa_registry") as conn:
        a = (await conn.execute(text("SELECT default_release_bundle_id, rollback_release_bundle_id FROM "
                                     "registry.bundle_assignments WHERE env = :e"), {"e": env})).first()
        where = "" if include_derived else "WHERE base_release_bundle_id IS NULL"
        rows = (await conn.execute(text(
            "SELECT release_bundle_id, display_version, promotion_state, worker_build_id, base_release_bundle_id, "
            f"tenant_id, created_at FROM registry.release_bundles {where} ORDER BY created_at DESC LIMIT 200"))).all()
    async with request.app.state.db.tx(scope=OPERATOR_SCOPE) as conn:
        counts = {r.release_bundle_id: r for r in (await conn.execute(text(
            "SELECT release_bundle_id, sum(running) AS running, sum(waiting) AS waiting FROM jobs_read.v_pinned_counts "
            "GROUP BY release_bundle_id"))).all()}
        fails = {r.release_bundle_id: r for r in (await conn.execute(text(
            "SELECT release_bundle_id, count(*) FILTER (WHERE status = 'failed') AS failed, count(*) AS total "
            "FROM jobs_read.jobs WHERE is_terminal AND terminal_at > now() - interval '24 hours' "
            "GROUP BY release_bundle_id"))).all()}
    items = []
    for r in rows:
        role = "none"
        if a is not None and r.release_bundle_id == a.default_release_bundle_id:
            role = "default"
        elif a is not None and r.release_bundle_id == a.rollback_release_bundle_id:
            role = "rollback_target"
        it: dict[str, Any] = {"release_bundle_id": r.release_bundle_id, "display_version": r.display_version,
                              "promotion_state": r.promotion_state, "worker_build_id": r.worker_build_id,
                              "assignment": {"role": role}, "created_at": _iso(r.created_at)}
        if r.base_release_bundle_id:
            it["base_release_bundle_id"] = r.base_release_bundle_id
            it["tenant_layer_of"] = r.tenant_id
        c = counts.get(r.release_bundle_id)
        it["jobs_running"] = int(c.running or 0) if c else 0
        it["jobs_waiting"] = int(c.waiting or 0) if c else 0
        f = fails.get(r.release_bundle_id)
        if f and f.total:
            it["failure_rate_24h"] = round(float(f.failed) / float(f.total), 4)
            it["terminal_24h"] = int(f.total)
        items.append(it)
    return {"items": items}


@router.get("/v1/releases/workers")
async def list_workers(request: Request, principal: Principal = require("release:read|dev:seed")) -> dict[str, Any]:
    """M1: routing status is derived from the registry (the default bundle's build is `current`, other ready builds
    `inactive`, retired builds `drained`) instead of DescribeWorkerDeployment; pollers are not reported."""
    env = request.app.state.settings.replicant_env
    async with request.app.state.db.tx(role="svc_cpa_registry") as conn:
        cur = (await conn.execute(text(
            "SELECT b.worker_build_id FROM registry.bundle_assignments a JOIN registry.release_bundles b ON "
            "b.release_bundle_id = a.default_release_bundle_id WHERE a.env = :e"), {"e": env})).scalar()
        rows = (await conn.execute(text(
            "SELECT deployment_name, build_id, image_digest, registered_at, ready_at, retired_at FROM "
            "registry.worker_deployment_versions ORDER BY registered_at DESC LIMIT 100"))).all()
        bundles: dict[str, list[str]] = {}
        for b in (await conn.execute(text("SELECT release_bundle_id, worker_build_id FROM registry.release_bundles "
                                          "ORDER BY created_at DESC"))).all():
            bundles.setdefault(b.worker_build_id, []).append(b.release_bundle_id)
    async with request.app.state.db.tx(scope=OPERATOR_SCOPE) as conn:
        pinned = {r.worker_build_id: r for r in (await conn.execute(text(
            "SELECT worker_build_id, sum(running + waiting) AS n, min(oldest_started_at) AS oldest FROM "
            "jobs_read.v_pinned_counts GROUP BY worker_build_id"))).all()}
    items = []
    for r in rows:
        status = "drained" if r.retired_at else ("current" if r.build_id == cur else "inactive")
        p = pinned.get(r.build_id)
        it: dict[str, Any] = {"deployment_name": r.deployment_name, "build_id": r.build_id, "routing_status": status,
                              "pinned_workflows": int(p.n or 0) if p else 0, "bundles": bundles.get(r.build_id, []),
                              "image_digest": r.image_digest, "registered_at": _iso(r.registered_at)}
        if r.ready_at:
            it["ready_at"] = _iso(r.ready_at)
        if p and p.oldest:
            it["oldest_pinned_started_at"] = _iso(p.oldest)
        items.append(it)
    return {"items": items}


# ------------------------------------------------------------------------------------------------ kill switches (read)
def fold_kill_switches(rows: list[Any]) -> list[dict[str, Any]]:
    """`tenancy.kill_switches` keeps one row per (scope, key, mode); the API shape is one `KillSwitch` per scope."""
    by: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        k = (r.scope, r.scope_key)
        ks = by.setdefault(k, {"scope": r.scope, "scope_value": r.scope_key, "intake": "open", "starts": "open",
                               "runs": "open", "publishing": "open", "reason_code": r.reason_code,
                               "notice_ids": {}, "mirror_generation": 0})
        if r.engaged:
            ks[r.mode] = (r.intake_state or "paused") if r.mode == "intake" else "paused"
        ks["notice_ids"][r.mode] = r.notice_id
        latest = ks.get("set_at")
        if latest is None or (r.issued_at and _iso(r.issued_at) > latest):
            ks.update(set_at=_iso(r.issued_at), set_by=r.issued_by, reason_code=r.reason_code)
            if r.note:
                ks["note"] = r.note
            if r.signal_batch_job_id:
                ks["signal_batch_job_id"] = r.signal_batch_job_id
        ks["mirror_generation"] = max(int(ks["mirror_generation"]), int(r.mirror_generation or 0))
    return sorted(by.values(), key=lambda x: (x["scope"] != "global", x["scope"], x["scope_value"]))


@router.get("/v1/admin/kill-switches")
async def list_kill_switches(request: Request, principal: Principal = require("ops:kill-switch")) -> dict[str, Any]:
    tenants = principal.tenants_with("ops:kill-switch")
    async with request.app.state.db.tx(scope=OPERATOR_SCOPE) as conn:
        rows = (await conn.execute(text("SELECT * FROM tenancy.kill_switches ORDER BY issued_at DESC"))).all()
    if tenants is not None:
        rows = [r for r in rows if r.tenant_id in tenants]
    return {"items": fold_kill_switches(rows), "mirror": {"store": "postgres", "in_sync": True}}
