"""Memory (v0 MEM, plan §2): the admin curation proxy, the read-only job memory view and the repo scan.

  ANY  /v1/admin/memory/{path}               memory:admin = member of `replicant-admins` (D-25), checked here on every
                                             request against the effective groups; a denial is audited
                                             (`memory.admin_denied`) and answers 403 with the audit id. Forwarded to
                                             memory-service /internal/v1/admin/{path} with a signed caller principal,
                                             which memory-service verifies and re-checks.
  GET  /v1/jobs/{job_id}/memory              job:read: the entries injected into the job (statement, type, scope; no
                                             author identity or scores)
  POST /v1/projects/{p}/repos/{r}:scan       project admins (tenant:policy:edit on the project) or memory admins who
                                             may start jobs: a JobRequest (workflow_type docs, label
                                             `replicant-repo-scan`) that the router sends to the repo-scan graph
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request, Response

from control_plane_api import audit
from control_plane_api.auth import check, current_principal, require
from control_plane_api.authz import Principal
from control_plane_api.config import read_secret
from control_plane_api.problems import ProblemError

log = logging.getLogger(__name__)
router = APIRouter()
ADMIN_GROUP = os.environ.get("REPLICANT_MEMORY_ADMIN_GROUP", "replicant-admins")
SCAN_LABEL = "replicant-repo-scan"


def _mem_url() -> str:
    return (os.environ.get("REPLICANT_MEMORY_URL") or "http://memory-service:8080").rstrip("/")


def _token() -> str:
    tok = read_secret(os.environ.get("REPLICANT_MEMORY_TOKEN_FILE"))
    if not tok:
        raise ProblemError(503, "memory_unavailable", detail="memory-service caller token is not configured")
    return tok


def signed_headers(principal: Principal, ttl: int = 60) -> dict[str, str]:
    """Bearer + X-Replicant-Caller (base64url JSON {principal_id, login, groups, exp}) + HMAC-SHA256 signature keyed
    with the bearer token (memory-service auth.py)."""
    tok = _token()
    raw = base64.urlsafe_b64encode(json.dumps({"principal_id": principal.principal_id, "login": principal.login or "",
                                               "groups": list(principal.groups), "exp": int(time.time()) + ttl}
                                              ).encode()).decode().rstrip("=")
    sig = hmac.new(tok.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": f"Bearer {tok}", "X-Replicant-Caller": raw, "X-Replicant-Caller-Sig": sig}


def is_memory_admin(principal: Principal) -> bool:
    return principal.is_user and ADMIN_GROUP in principal.groups


def require_memory_admin() -> Any:
    async def dep(request: Request, principal: Principal = Depends(current_principal)) -> Principal:
        if is_memory_admin(principal):
            return principal
        db = request.app.state.db
        async with db.tx(role="svc_cpa_tenancy") as conn:
            aid = await audit.emit(conn, db, {
                "event_type": "memory.admin_denied", "outcome": "denied", "tenant_id": "replicant-system",
                "actor_principal_id": principal.principal_id, "channel": "ui", "reason": "not_in_admin_group",
                "details": {"method": request.method, "path": request.url.path[:256], "required_group": ADMIN_GROUP}})
        raise ProblemError(403, "not_memory_admin", detail=f"memory curation requires membership of {ADMIN_GROUP}",
                           audit_id=aid)

    dep.__replicant_permission__ = "memory:admin"  # type: ignore[attr-defined]
    return Depends(dep)


@router.get("/v1/admin/memory/{path:path}")
@router.post("/v1/admin/memory/{path:path}")
async def memory_admin_proxy(path: str, request: Request, principal: Principal = require_memory_admin()) -> Response:
    headers = signed_headers(principal)
    for h in ("if-match", "idempotency-key", "content-type"):
        if request.headers.get(h):
            headers[h] = request.headers[h]
    body = await request.body() if request.method == "POST" else None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as c:
            r = await c.request(request.method, f"{_mem_url()}/internal/v1/admin/{path}",
                                params=dict(request.query_params), content=body, headers=headers)
    except httpx.HTTPError as e:
        raise ProblemError(503, "memory_unavailable", detail=type(e).__name__) from None
    return Response(r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))


@router.get("/v1/jobs/{job_id}/memory")
async def job_memory(job_id: str, request: Request, response: Response,
                     principal: Principal = require("job:read")) -> dict[str, Any]:
    from control_plane_api.routes_jobs import load_job_row
    r = await load_job_row(request, principal, job_id)
    response.headers["Cache-Control"] = "no-store"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as c:
            m = await c.get(f"{_mem_url()}/internal/v1/jobs/{job_id}/memory", params={"tenant_id": r.tenant_id},
                            headers={"Authorization": f"Bearer {_token()}"})
        if m.status_code != 200:
            raise httpx.HTTPError(f"HTTP {m.status_code}")
        doc = m.json()
    except (httpx.HTTPError, ProblemError, ValueError) as e:
        log.warning("job memory view unavailable: %s", str(e)[:120])
        return {"job_id": job_id, "available": False, "memory_snapshot_id": (r.snapshot or {}).get("memory_snapshot_id"),
                "entries": [], "runs": []}
    keep = ("ref", "memory_type", "scope", "title", "statement", "purged")
    return {"job_id": job_id, "available": True, "memory_snapshot_id": doc.get("memory_snapshot_id"),
            "runs": doc.get("runs") or [], "entries": [{k: e.get(k) for k in keep} for e in doc.get("entries") or []]}


def scan_request(tenant_id: str, project_id: str, repo: dict[str, Any], requester: str) -> dict[str, Any]:
    """The repo-scan JobRequest (C-JOBREQ): docs + label replicant-repo-scan (DEVIATIONS MEM-D7)."""
    from control_plane_api.ids import ulid
    owner, _, name = repo["full_name"].partition("/")
    now = dt.datetime.now(dt.UTC)
    return {"schema_version": "replicant.job-request/v1", "request_id": ulid("jrq"),
            "tenant_id": tenant_id, "workflow_type": "docs", "priority": "low",
            "received_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "dedupe_key": f"memscan:{project_id}:{repo['repo_id']}:{uuid.uuid4().hex[:12]}",
            "source": {"channel": "ui", "event_type": "memory.repo_scan", "event_id": uuid.uuid4().hex},
            "requester": {"kind": "user", "principal_id": requester},
            "intent": {"title": f"Learn the conventions of {repo['full_name']}", "format": "markdown", "untrusted": True,
                       "text": "Scan the repository's linter, formatter and CI configuration, contributing guides, README, "
                               "existing agent instruction files and a sample of source files. Propose convention "
                               "candidates for the memory admin queue."},
            "targets": [{"repo": {"host": repo["host"], "owner": owner, "name": name, "repo_id": repo["repo_id"],
                                  "provider": repo["provider"]}, "base_ref": "main"}],
            "constraints": {"labels": [SCAN_LABEL]}}


@router.post("/v1/projects/{project_id}/repos/{repo_ref}")
async def scan_repo(project_id: str, repo_ref: str, request: Request,
                    principal: Principal = require("job:create")) -> Response:
    from control_plane_api import projects as P
    from control_plane_api.routes_projects import _can, _project
    repo_id, _, verb = repo_ref.partition(":")
    if verb != "scan":
        raise ProblemError(404, "not_found")
    row = await _project(request, principal, project_id)
    if not (_can(request, principal, row, "tenant:policy:edit") or is_memory_admin(principal)):
        raise ProblemError(403, "missing_permission", detail="project admins or memory admins may start a repo scan")
    repo = next((x for x in await P.project_repos(request.app.state.db, project_id) if x["repo_id"] == repo_id), None)
    if repo is None:
        raise ProblemError(404, "not_found")
    jr = scan_request(row.tenant_id, project_id, repo, principal.principal_id)
    res = await request.app.state.intake.submit(principal, jr)
    body = res.body if isinstance(res.body, dict) else {}
    return Response(json.dumps({**body, "workflow": "repo_scan", "candidates_url": "/admin/memory?tab=candidates"}),
                    status_code=res.status_code, media_type="application/json")
