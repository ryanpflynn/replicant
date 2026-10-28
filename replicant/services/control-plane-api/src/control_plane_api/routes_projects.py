"""Projects, project access and team configuration (v0 CONFIG-01, plan §4). Every route is behind `require(...)`; the
project scope is checked in the handler with `check(..., project_id=...)` (tenant bindings cover every project of the
tenant; project bindings only their project).

  GET  /v1/tenants/{t}/projects                     job:read        projects the caller can see
  POST /v1/tenants/{t}/projects                     identity:admin  (tenant scope) create {project_id, name, owners_group?, repos?}
  GET  /v1/projects/{p}                             job:read
  PATCH /v1/projects/{p}                            identity:admin  name, owners_group, add_repos, remove_repos
  GET  /v1/projects/{p}/access                      job:read        project role bindings (+ inherited tenant bindings)
  PUT  /v1/projects/{p}/access                      identity:admin  {bindings: [{group, role}], reason}
  GET  /v1/projects/{p}/config                      job:read        effective config, versions, history, can_edit
  PUT  /v1/projects/{p}/config                      tenant:ops:edit (live) / tenant:policy:edit (pinned); If-Match
  PUT  /v1/projects/{p}/repos/{r}/config            same, repo override
  POST /v1/projects/{p}/config:validate             job:read        dry run: errors, compiled graph, changes
  POST /v1/projects/{p}/config:rollback             as PUT          {version, reason, repo_id?}
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, Body, Header, Request
from sqlalchemy import text

from control_plane_api import audit, derived, projects as P
from control_plane_api.auth import check, require
from control_plane_api.authz import Principal
from control_plane_api.config import read_secret
from control_plane_api.db import OPERATOR_SCOPE
from control_plane_api.problems import ProblemError
from control_plane_api.rbac import project_permissions
from replicant_telemetry.common import iso

log = logging.getLogger(__name__)
router = APIRouter()

PROJECT_ID_RE = re.compile(r"^prj_[a-z0-9][a-z0-9-]{1,40}$")
GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
EDIT_PERMS = ("tenant:ops:edit", "tenant:policy:edit", "identity:admin")


async def _project(request: Request, principal: Principal, project_id: str, perm: str = "job:read") -> Any:
    row = await P.project_row(request.app.state.db, project_id) if PROJECT_ID_RE.match(project_id) else None
    if row is None:
        raise ProblemError(404, "not_found")
    # Read access first: a project the caller cannot see (another tenant's, or another project of the tenant) is 404
    # for reads and writes alike, so its existence does not leak (v0 SEC-D2). Readers without the write permission
    # get 403 and see why the change is refused.
    if not check(request.app.state.auth, principal, "job:read|dev:seed", tenant_id=row.tenant_id,
                 project_id=project_id):
        raise ProblemError(404, "not_found")
    if perm != "job:read" and not _can(request, principal, row, perm):
        raise ProblemError(403, "missing_permission", detail=f"{perm} on project {project_id}")
    return row


def _can(request: Request, principal: Principal, row: Any, perm: str) -> bool:
    return check(request.app.state.auth, principal, perm + "|dev:seed", tenant_id=row.tenant_id,
                 project_id=row.project_id)


def perms_on(principal: Principal, row: Any) -> list[str]:
    have = project_permissions(principal, row.project_id, row.tenant_id)
    return sorted(p for p in have if p in ("job:read", "job:create", "gate:decide", *EDIT_PERMS))


def _project_doc(row: Any, repos: list[dict[str, Any]], principal: Principal) -> dict[str, Any]:
    return {"project_id": row.project_id, "tenant_id": row.tenant_id, "name": row.name,
            "owners_group": row.owners_group, "repos": repos, "permissions": perms_on(principal, row)}


# ------------------------------------------------------------------------------------------------ projects
@router.get("/v1/tenants/{tenant_id}/projects")
async def list_tenant_projects(tenant_id: str, request: Request, principal: Principal = require("job:read")
                               ) -> dict[str, Any]:
    items = []
    for r in await P.list_projects(request.app.state.db, tenant_id):
        if check(request.app.state.auth, principal, "job:read", tenant_id=r.tenant_id, project_id=r.project_id):
            items.append({"project_id": r.project_id, "tenant_id": r.tenant_id, "name": r.name,
                          "owners_group": r.owners_group, "repo_count": int(r.repos),
                          "permissions": perms_on(principal, r)})
    return {"items": items}


@router.get("/v1/projects")
async def list_my_projects(request: Request, principal: Principal = require("job:read")) -> dict[str, Any]:
    return {"items": await visible_projects(request, principal)}


async def visible_projects(request: Request, principal: Principal) -> list[dict[str, Any]]:
    if not await request.app.state.db.has_relation("tenancy.projects"):
        return []
    out = []
    for r in await P.list_projects(request.app.state.db):
        if check(request.app.state.auth, principal, "job:read", tenant_id=r.tenant_id, project_id=r.project_id):
            out.append({"project_id": r.project_id, "tenant_id": r.tenant_id, "name": r.name,
                        "repo_count": int(r.repos), "permissions": perms_on(principal, r)})
    return out


@router.post("/v1/tenants/{tenant_id}/projects", status_code=201)
async def create_project(tenant_id: str, request: Request, body: dict[str, Any] = Body(...),
                         principal: Principal = require("identity:admin|dev:seed")) -> dict[str, Any]:
    if not check(request.app.state.auth, principal, "identity:admin|dev:seed", tenant_id=tenant_id):
        raise ProblemError(403, "missing_permission", detail="identity:admin on the tenant")
    pid, name = str(body.get("project_id", "")), str(body.get("name", "")).strip()
    if not PROJECT_ID_RE.match(pid) or not name or len(name) > 100 or \
            (body.get("owners_group") and not GROUP_RE.match(str(body["owners_group"]))):
        raise ProblemError(422, "invalid_request", detail="project_id prj_<slug>, name 1-100 chars, owners_group")
    db = request.app.state.db
    async with db.tx(role="svc_cpa_tenancy", tenant=tenant_id) as conn:
        t = (await conn.execute(text("SELECT tenant_id FROM tenancy.tenants WHERE tenant_id = :t"),
                                {"t": tenant_id})).first()
        if t is None:
            raise ProblemError(404, "unknown_tenant")
        ex = (await conn.execute(text("SELECT tenant_id, name FROM tenancy.projects WHERE project_id = :p"),
                                 {"p": pid})).first()
        created = ex is None
        if ex is not None and ex.tenant_id != tenant_id:
            raise ProblemError(409, "project_id_taken")
        if created:
            await conn.execute(text("INSERT INTO tenancy.projects (project_id, tenant_id, name, owners_group, created_by) "
                                    "VALUES (:p, :t, :n, :o, :by)"),
                               {"p": pid, "t": tenant_id, "n": name, "o": body.get("owners_group"),
                                "by": principal.principal_id})
            await audit.emit(conn, db, {"event_type": "admin.project_created", "outcome": "success",
                                        "tenant_id": tenant_id, "actor_principal_id": principal.principal_id,
                                        "target_kind": "project", "target_id": pid})
        for rp in body.get("repos") or []:
            await _add_repo(conn, pid, tenant_id, rp, principal.principal_id)
    await request.app.state.auth.rbac.refresh(force=True)
    row = await P.project_row(db, pid)
    doc = _project_doc(row, await P.project_repos(db, pid), principal)
    doc["created"] = created
    return doc


async def _add_repo(conn: Any, pid: str, tenant_id: str, rp: dict[str, Any], actor: str) -> None:
    rid, full = str(rp.get("repo_id", "")), str(rp.get("full_name", ""))
    if not rid or not re.match(r"^[A-Za-z0-9._-]{1,64}$", rid) or "/" not in full:
        raise ProblemError(422, "invalid_repo", detail="repo_id and full_name (owner/name) are required")
    ex = (await conn.execute(text("SELECT project_id FROM tenancy.project_repos WHERE repo_id = :r"),
                             {"r": rid})).first()
    if ex is not None:
        if ex.project_id != pid:
            raise ProblemError(409, "repo_in_other_project", project_id=ex.project_id)
        return
    await conn.execute(text("INSERT INTO tenancy.project_repos (repo_id, project_id, tenant_id, provider, host, "
                            "full_name, added_by) VALUES (:r, :p, :t, :pv, :h, :f, :by)"),
                       {"r": rid, "p": pid, "t": tenant_id, "pv": rp.get("provider", "gitea"),
                        "h": rp.get("host", "git.replicant.localhost:3300"), "f": full, "by": actor})


@router.get("/v1/projects/{project_id}")
async def get_project(project_id: str, request: Request, principal: Principal = require("job:read|dev:seed")) -> dict[str, Any]:
    row = await _project(request, principal, project_id)
    return _project_doc(row, await P.project_repos(request.app.state.db, project_id), principal)


@router.patch("/v1/projects/{project_id}")
async def patch_project(project_id: str, request: Request, body: dict[str, Any] = Body(...),
                        principal: Principal = require("identity:admin|dev:seed")) -> dict[str, Any]:
    row = await _project(request, principal, project_id, "identity:admin")
    if set(body) - {"name", "owners_group", "add_repos", "remove_repos"}:
        raise ProblemError(422, "invalid_request", detail="name, owners_group, add_repos, remove_repos")
    db = request.app.state.db
    async with db.tx(role="svc_cpa_tenancy", tenant=row.tenant_id) as conn:
        if "name" in body:
            if not str(body["name"]).strip():
                raise ProblemError(422, "invalid_request", detail="name")
            await conn.execute(text("UPDATE tenancy.projects SET name = :n WHERE project_id = :p"),
                               {"n": str(body["name"]).strip()[:100], "p": project_id})
        if "owners_group" in body:
            if body["owners_group"] and not GROUP_RE.match(str(body["owners_group"])):
                raise ProblemError(422, "invalid_request", detail="owners_group")
            await conn.execute(text("UPDATE tenancy.projects SET owners_group = :o WHERE project_id = :p"),
                               {"o": body["owners_group"] or None, "p": project_id})
        for rp in body.get("add_repos") or []:
            await _add_repo(conn, project_id, row.tenant_id, rp, principal.principal_id)
        for rid in body.get("remove_repos") or []:
            await conn.execute(text("DELETE FROM tenancy.project_repos WHERE repo_id = :r AND project_id = :p"),
                               {"r": str(rid), "p": project_id})
        await audit.emit(conn, db, {"event_type": "admin.project_changed", "outcome": "success",
                                    "tenant_id": row.tenant_id, "actor_principal_id": principal.principal_id,
                                    "target_kind": "project", "target_id": project_id,
                                    "details": {"fields": sorted(body)}})
    await request.app.state.auth.rbac.refresh(force=True)
    request.app.state.live_config.invalidate()
    row = await P.project_row(db, project_id)
    return _project_doc(row, await P.project_repos(db, project_id), principal)


# ------------------------------------------------------------------------------------------------ access
async def _bindings(request: Request, row: Any) -> dict[str, Any]:
    async with request.app.state.db.tx(scope=OPERATOR_SCOPE) as conn:
        v = (await conn.execute(text("SELECT version, created_at, created_by, reason FROM identity.role_binding_versions "
                                     "WHERE is_current"))).first()
        rows = [] if v is None else (await conn.execute(text(
            "SELECT binding_id, group_name, scope, role, tenant_id, project_id FROM identity.role_bindings WHERE "
            "version = :v AND ((scope = 'project' AND project_id = :p) OR (scope = 'tenant' AND tenant_id = :t) OR "
            "scope = 'platform') ORDER BY scope, group_name, role"), {"v": v.version, "p": row.project_id,
                                                                     "t": row.tenant_id})).all()
        members: dict[str, list[dict[str, str]]] = {}
        for m in (await conn.execute(text(
                "SELECT g.display_name, u.principal_id, u.user_name FROM identity.memberships m JOIN identity.groups g "
                "ON g.group_id = m.group_id JOIN identity.users u ON u.principal_id = m.principal_id WHERE "
                "g.deleted_at IS NULL ORDER BY u.user_name"))).all():
            members.setdefault(m.display_name, []).append({"principal_id": m.principal_id,
                                                           "login": m.user_name.split("@")[0]})
    proj = [{"group": r.group_name, "role": r.role} for r in rows if r.scope == "project"]
    inherited = [{"group": r.group_name, "role": r.role, "scope": r.scope, **({"tenant_id": r.tenant_id}
                                                                            if r.tenant_id else {})}
                 for r in rows if r.scope != "project" and r.role in ("viewer", "engineer", "approver", "tenant_admin")]
    groups = sorted({b["group"] for b in proj + inherited})
    return {"project_id": row.project_id, "version": v.version if v else 0, "bindings": proj, "inherited": inherited,
            "members": {g: members.get(g, []) for g in groups}, "roles": list(P.PROJECT_ROLES),
            "updated_at": iso(v.created_at) if v else None, "updated_by": v.created_by if v else None}


@router.get("/v1/projects/{project_id}/access")
async def get_access(project_id: str, request: Request, principal: Principal = require("job:read|dev:seed")) -> dict[str, Any]:
    row = await _project(request, principal, project_id)
    doc = await _bindings(request, row)
    doc["can_edit"] = _can(request, principal, row, "identity:admin")
    return doc


@router.put("/v1/projects/{project_id}/access")
async def put_access(project_id: str, request: Request, body: dict[str, Any] = Body(...),
                     principal: Principal = require("identity:admin|dev:seed")) -> dict[str, Any]:
    row = await _project(request, principal, project_id, "identity:admin")
    reason = str(body.get("reason") or "").strip()
    bindings = body.get("bindings")
    if not reason or not isinstance(bindings, list):
        raise ProblemError(422, "invalid_request", detail="bindings [{group, role}] and a reason are required")
    clean = []
    for b in bindings:
        if not isinstance(b, dict) or not GROUP_RE.match(str(b.get("group", ""))) or b.get("role") not in P.PROJECT_ROLES:
            raise ProblemError(422, "invalid_binding", detail=f"group must match {GROUP_RE.pattern}; role one of "
                                                              f"{list(P.PROJECT_ROLES)}")
        if (b["group"], b["role"]) not in clean:
            clean.append((b["group"], b["role"]))
    db = request.app.state.db
    async with db.tx(role="svc_cpa_tenancy", tenant=row.tenant_id) as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('identity.role_binding_versions'))"))
        cur = (await conn.execute(text("SELECT version FROM identity.role_binding_versions WHERE is_current"))).scalar()
        new = int((await conn.execute(text("SELECT coalesce(max(version), 0) + 1 FROM "
                                           "identity.role_binding_versions"))).scalar_one())
        await conn.execute(text("INSERT INTO identity.role_binding_versions (version, is_current, created_by, reason) "
                                "VALUES (:v, false, :by, :r)"), {"v": new, "by": principal.principal_id,
                                                                  "r": reason[:500]})
        if cur is not None:
            await conn.execute(text(
                "INSERT INTO identity.role_bindings (version, binding_id, group_name, scope, tenant_id, project_id, "
                "role, repo_globs) SELECT :new, binding_id, group_name, scope, tenant_id, project_id, role, repo_globs "
                "FROM identity.role_bindings WHERE version = :cur AND NOT (scope = 'project' AND project_id = :p)"),
                {"new": new, "cur": cur, "p": project_id})
        nxt = int((await conn.execute(text("SELECT coalesce(max(binding_id), 0) FROM identity.role_bindings WHERE "
                                           "version = :v"), {"v": new})).scalar_one())
        for i, (g, r) in enumerate(clean, start=1):
            await conn.execute(text("INSERT INTO identity.role_bindings (version, binding_id, group_name, scope, "
                                    "tenant_id, project_id, role) VALUES (:v, :b, :g, 'project', :t, :p, :r)"),
                               {"v": new, "b": nxt + i, "g": g, "t": row.tenant_id, "p": project_id, "r": r})
        await conn.execute(text("UPDATE identity.role_binding_versions SET is_current = false WHERE is_current"))
        await conn.execute(text("UPDATE identity.role_binding_versions SET is_current = true WHERE version = :v"),
                           {"v": new})
        await audit.emit(conn, db, {"event_type": "admin.role_binding_changed", "outcome": "success",
                                    "tenant_id": row.tenant_id, "actor_principal_id": principal.principal_id,
                                    "target_kind": "project", "target_id": project_id,
                                    "details": {"reason": reason[:200], "binding_version": new,
                                                "bindings": [f"{g}:{r}" for g, r in clean][:50]}})
    await request.app.state.auth.rbac.refresh(force=True)
    doc = await _bindings(request, row)
    doc["can_edit"] = True
    return doc


# ------------------------------------------------------------------------------------------------ configuration
async def _config_doc(request: Request, principal: Principal, row: Any) -> dict[str, Any]:
    db = request.app.state.db
    repos = await P.project_repos(db, row.project_id)
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        cur = await P.current_configs(conn, row.project_id)
    proj_v, proj_cfg = cur.get("", (0, {}))
    eff = P.merge(proj_cfg)
    repo_docs = {}
    for r in repos:
        v, c = cur.get(r["repo_id"], (0, {}))
        repo_docs[r["repo_id"]] = {"full_name": r["full_name"], "version": v, "config": c,
                                   "effective": P.merge(proj_cfg, c)}
    history = await P.config_versions(db, row.project_id)
    return {"project_id": row.project_id, "tenant_id": row.tenant_id, "version": proj_v, "config": proj_cfg,
            "effective": eff, "defaults": P.DEFAULTS, "repos": repo_docs, "history": history,
            "sections": {s: ("live" if s in P.LIVE_SECTIONS else "new_jobs") for s in (*P.LIVE_SECTIONS,
                                                                                         *P.PINNED_SECTIONS)},
            "can_edit": {"live": _can(request, principal, row, "tenant:ops:edit"),
                         "pinned": _can(request, principal, row, "tenant:policy:edit")}}


@router.get("/v1/projects/{project_id}/config")
async def get_config(project_id: str, request: Request, principal: Principal = require("job:read|dev:seed")) -> dict[str, Any]:
    row = await _project(request, principal, project_id)
    return await _config_doc(request, principal, row)


async def _validate(request: Request, row: Any, scope_key: str, config: Any) -> dict[str, Any]:
    errs = P.schema_errors(config)
    db = request.app.state.db
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        cur = await P.current_configs(conn, row.project_id)
    proj_cfg = cur.get("", (0, {}))[1]
    if scope_key:
        old_eff = P.merge(proj_cfg, cur.get(scope_key, (0, {}))[1])
        new_eff = P.merge(proj_cfg, config if not errs else {})
    else:
        old_eff, new_eff = P.merge(proj_cfg), P.merge(config if not errs else {})
    out: dict[str, Any] = {"valid": not errs, "errors": errs, "changes": P.changes(old_eff, new_eff) if not errs else [],
                           "effective": new_eff, "graphs": {}}
    if not errs and new_eff["workflows"]:
        try:
            compiled = await derived.preview_graphs(request.app.state, new_eff)
        except derived.CompileError as e:
            out["valid"] = False
            out["errors"] = e.errors
            compiled = {}
        out["graphs"] = compiled
    return out


@router.post("/v1/projects/{project_id}/config:validate")
async def validate_config(project_id: str, request: Request, body: dict[str, Any] = Body(...),
                          principal: Principal = require("job:read")) -> dict[str, Any]:
    row = await _project(request, principal, project_id)
    scope_key = str(body.get("repo_id") or "")
    return await _validate(request, row, scope_key, body.get("config", {}))


def _if_match(v: str | None) -> int:
    if v is None:
        raise ProblemError(428, "precondition_required", detail="If-Match: <current version> (0 when none)")
    try:
        return int(v.strip().strip('"').removeprefix("W/").strip('"'))
    except ValueError:
        raise ProblemError(422, "invalid_if_match") from None


async def _apply(request: Request, principal: Principal, row: Any, scope_key: str, config: Any, reason: str,
                 if_match: int, rolled_back_from: int | None = None) -> dict[str, Any]:
    if not reason.strip():
        raise ProblemError(422, "reason_required")
    if scope_key and scope_key not in {r["repo_id"] for r in await P.project_repos(request.app.state.db,
                                                                                   row.project_id)}:
        raise ProblemError(404, "repo_not_in_project")
    res = await _validate(request, row, scope_key, config)
    if not res["valid"]:
        raise ProblemError(422, "invalid_config", errors=res["errors"])
    for perm in sorted(P.permissions_needed(res["changes"])):
        if not _can(request, principal, row, perm):
            raise ProblemError(403, "missing_permission", detail=f"{perm} on project {row.project_id}")
    try:
        v = await P.write_config(request.app.state.db, row.project_id, row.tenant_id, scope_key, config, if_match,
                                 principal.principal_id, reason.strip(), rolled_back_from)
    except P.VersionConflict as e:
        raise ProblemError(412, "version_conflict", current_version=e.current)
    request.app.state.live_config.invalidate()
    synced = []
    if any(c["path"].startswith("triggers.") for c in res["changes"]):
        synced = await sync_triggers(request, row, principal.principal_id)
    doc = await _config_doc(request, principal, row)
    doc.update({"written_version": v, "scope": scope_key or "project", "changes": res["changes"],
                "triggers_synced": synced})
    return doc


@router.put("/v1/projects/{project_id}/config")
async def put_config(project_id: str, request: Request, body: dict[str, Any] = Body(...),
                     if_match: str | None = Header(None, alias="If-Match"),
                     principal: Principal = require("tenant:ops:edit|tenant:policy:edit|dev:seed")) -> dict[str, Any]:
    row = await _project(request, principal, project_id, "tenant:ops:edit|tenant:policy:edit")
    return await _apply(request, principal, row, "", body.get("config", {}), str(body.get("reason") or ""),
                        _if_match(if_match))


@router.put("/v1/projects/{project_id}/repos/{repo_id}/config")
async def put_repo_config(project_id: str, repo_id: str, request: Request, body: dict[str, Any] = Body(...),
                          if_match: str | None = Header(None, alias="If-Match"),
                          principal: Principal = require("tenant:ops:edit|tenant:policy:edit|dev:seed")
                          ) -> dict[str, Any]:
    row = await _project(request, principal, project_id, "tenant:ops:edit|tenant:policy:edit")
    return await _apply(request, principal, row, repo_id, body.get("config", {}), str(body.get("reason") or ""),
                        _if_match(if_match))


@router.post("/v1/projects/{project_id}/config:rollback")
async def rollback_config(project_id: str, request: Request, body: dict[str, Any] = Body(...),
                          principal: Principal = require("tenant:ops:edit|tenant:policy:edit|dev:seed")
                          ) -> dict[str, Any]:
    row = await _project(request, principal, project_id, "tenant:ops:edit|tenant:policy:edit")
    scope_key = str(body.get("repo_id") or "")
    try:
        target = int(body.get("version"))
    except (TypeError, ValueError):
        raise ProblemError(422, "invalid_request", detail="version") from None
    versions = await P.config_versions(request.app.state.db, project_id, scope_key, limit=1000)
    old = next((v for v in versions if v["version"] == target), None)
    if old is None:
        raise ProblemError(404, "unknown_version")
    cur = next((v["version"] for v in versions if v["is_current"]), 0)
    return await _apply(request, principal, row, scope_key, old["config"],
                        str(body.get("reason") or f"rollback to version {target}"), cur, rolled_back_from=target)


# ------------------------------------------------------------------------------------------------ live trigger sync
def trigger_settings(eff: dict[str, Any], full_name: str) -> dict[str, Any]:
    t = eff["triggers"]
    return {"full_name": full_name, "enabled": bool(t["enabled"]), "trigger_label": t["label"],
            "trigger_modes": list(t["modes"]), "allowed_workflow_types": list(t["allowed_workflow_types"]),
            "default_priority": t["default_priority"]}


async def sync_triggers(request: Request, row: Any, actor: str) -> list[dict[str, Any]]:
    """Push the effective trigger settings of every repo of the project to github-integration's existing
    `/internal/v1/repo-settings` (writer of github_app.repo_settings). Live: the next webhook uses them."""
    ns = request.app.state.notify.ns
    tok = read_secret(ns.delivery_token_file)
    db = request.app.state.db
    out = []
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        cur = await P.current_configs(conn, row.project_id)
    for r in await P.project_repos(db, row.project_id):
        eff = P.merge(cur.get("", (0, {}))[1], cur.get(r["repo_id"], (0, {}))[1])
        body = {"provider": r["provider"], "host": r["host"], "tenant_id": row.tenant_id, "repo_id": r["repo_id"],
                "settings": {"repo_id": r["repo_id"], **trigger_settings(eff, r["full_name"])}, "actor": actor}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                resp = await c.put(f"{ns.github_integration_url}/internal/v1/repo-settings", json=body,
                                   headers={"Authorization": f"Bearer {tok}"} if tok else {})
            out.append({"repo_id": r["repo_id"], "status": resp.status_code})
            if resp.status_code >= 300:
                log.warning("trigger sync %s: HTTP %s %s", r["repo_id"], resp.status_code, resp.text[:200])
        except httpx.HTTPError as e:
            log.warning("trigger sync %s failed: %s", r["repo_id"], type(e).__name__)
            out.append({"repo_id": r["repo_id"], "status": 0})
    return out


__all__ = ["router", "visible_projects", "sync_triggers", "json"]
