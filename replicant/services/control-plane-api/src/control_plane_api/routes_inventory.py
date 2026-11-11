"""Repo onboarding: inventory, readiness and the fleet impact queue (design §8c; contract
docs/design/onboarding-interfaces.md §1, §4, §7, §10). control-plane-api is the only writer of the `inventory` schema.

Internal (service tokens, REPLICANT_INTERNAL_CALLER_TOKENS_FILE):
  PUT  /internal/v1/repos/{repo_id}/profile        service:github-integration  scanner profile -> classify, infer the
                                                                                 environment, queue a readiness probe
  PUT  /internal/v1/repos/{repo_id}/readiness      service:replicant-worker    readiness probe result
  POST /internal/v1/inventory/observations         service:replicant-worker    what a job loaded / skipped / failed
Admin (fleet:admin = member of the memory admin group, `replicant-admins`; a denial is audited):
  GET  /v1/admin/fleet/items?kind=&class=&sort=impact|repos|recent&limit=&cursor=
  GET  /v1/admin/fleet/items/{id}
  POST /v1/admin/fleet/items/{id}:decide           {decision: approve|deny|map, map_to?, reason}   audited
  POST /v1/admin/fleet:rescan                      {repo_ids?}  -> github-integration                audited
Project:
  GET  /v1/projects/{p}/readiness                  job:read on the project
  POST /v1/projects/{p}/repos/{r}:recheck          tenant:policy:edit on the project or fleet/memory admin (dispatched
                                                   from routes_memory's /repos/{repo_ref} route, which owns the path)
  POST /v1/projects/{p}/fleet-items/{id}:request   project admins (tenant:policy:edit)                audited

The readiness launcher (`readiness_loop`, advisory lock (0x46414354, 5)) submits a no-LLM probe job (label
`replicant-readiness`, workflow qa_only) through intake for every repo whose environment or commands changed.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, Request, Response
from sqlalchemy import text

from control_plane_api import audit
from control_plane_api import inventory as I
from control_plane_api.auth import current_principal, require
from control_plane_api.authz import Principal
from control_plane_api.config import read_secret
from control_plane_api.db import OPERATOR_SCOPE
from control_plane_api.ids import ulid
from control_plane_api.locks import locked_loop
from control_plane_api.problems import ProblemError

log = logging.getLogger(__name__)
router = APIRouter()

READINESS_LABEL = "replicant-readiness"
READINESS_PRINCIPAL = "svc_replicant-readiness"   # in-process requester of automatic probes (no token)
READINESS_LOCK = (0x46414354, 5)
SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
REPO_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
ITEM_RE = re.compile(r"^inv_[0-9a-hjkmnp-tv-z]{26}$")
JOB_RE = re.compile(r"^job_[0-9a-hjkmnp-tv-z]{26}$")
KINDS = tuple(I.CLASSES)
STATUSES = ("loaded", "skipped", "failed")
MAX_ATTEMPTS_BACKOFF_S = 3600


def _iso(v: Any) -> str | None:
    return v.isoformat().replace("+00:00", "Z") if isinstance(v, dt.datetime) else v


# ------------------------------------------------------------------------------------------------ data access
async def repo_owner(db: Any, repo_id: str) -> dict[str, Any] | None:
    """{tenant_id, project_id, provider, host, full_name} of a repo: its project, else its github_app registration."""
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        r = (await conn.execute(text(
            "SELECT tenant_id, project_id, provider, host, full_name FROM tenancy.project_repos WHERE repo_id = :r"),
            {"r": repo_id})).first()
        if r is not None:
            return dict(r._mapping)
        if await db.has_relation("github_app.repos"):
            g = (await conn.execute(text(
                "SELECT tenant_id, NULL AS project_id, provider, host, full_name FROM github_app.repos WHERE repo_id = "
                ":r AND tenant_id IS NOT NULL ORDER BY removed_at NULLS FIRST LIMIT 1"), {"r": repo_id})).first()
            if g is not None:
                return dict(g._mapping)
    return None


async def upsert_item(conn: Any, item: dict[str, Any], *, reclassify: bool = True) -> str:
    """The catalogue item_id of (kind, identity); class and default follow the classifier when `reclassify`."""
    upd = ("class = EXCLUDED.class, default_action = EXCLUDED.default_action, details = EXCLUDED.details, "
           "last_seen = now()") if reclassify else "last_seen = now()"
    return (await conn.execute(text(
        "INSERT INTO inventory.catalog (item_id, kind, identity, class, default_action, details) VALUES (:id, :k, :i, "
        f":c, :d, CAST(:det AS jsonb)) ON CONFLICT (kind, identity) DO UPDATE SET {upd} RETURNING item_id"),
        {"id": ulid("inv"), "k": item["kind"], "i": item["identity"], "c": item["class"],
         "d": item["default_action"], "det": json.dumps(item.get("details") or {})})).scalar_one()


def _check_profile(repo_id: str, body: Any) -> list[str]:
    errs = []
    if not isinstance(body, dict):
        return ["body must be an object"]
    if str(body.get("repo_id", repo_id)) != repo_id:
        errs.append("repo_id must match the path")
    if not SHA_RE.match(str(body.get("sha", ""))):
        errs.append("sha must be a commit id")
    if not isinstance(body.get("full_name"), str) or "/" not in body["full_name"]:
        errs.append("full_name is required")
    for k in ("files", "ecosystems", "ci_commands", "mcp_servers", "errors"):
        if not isinstance(body.get(k, []), list):
            errs.append(f"{k} must be a list")
    if not isinstance(body.get("claude", {}) or {}, dict):
        errs.append("claude must be an object")
    if len(json.dumps(body)) > 1_000_000:
        errs.append("profile too large")
    return errs


async def store_profile(db: Any, repo_id: str, body: dict[str, Any], owner: dict[str, Any]) -> dict[str, Any]:
    env = I.infer_environment(body)
    items = I.catalog_items(body, env)
    ed = I.env_digest(env)
    tenant = owner["tenant_id"]
    async with db.tx(role="svc_cpa_tenancy", tenant=tenant) as conn:
        pid = (await conn.execute(text(
            "INSERT INTO inventory.repo_profiles (profile_id, tenant_id, repo_id, full_name, default_branch, sha, "
            "scanner_version, scanned_at, profile, environment, env_digest, is_current) VALUES (:id, :t, :r, :f, :b, :s, "
            ":v, :at, CAST(:p AS jsonb), CAST(:e AS jsonb), :d, false) ON CONFLICT (repo_id, sha) DO UPDATE SET "
            "profile = EXCLUDED.profile, environment = EXCLUDED.environment, env_digest = EXCLUDED.env_digest, "
            "scanner_version = EXCLUDED.scanner_version, scanned_at = EXCLUDED.scanned_at, full_name = "
            "EXCLUDED.full_name, default_branch = EXCLUDED.default_branch, updated_at = now() RETURNING profile_id"),
            {"id": ulid("rp"), "t": tenant, "r": repo_id, "f": body["full_name"],
             "b": str(body.get("default_branch") or "main"), "s": body["sha"],
             "v": int(body.get("scanner_version") or 1), "at": body.get("scanned_at") or dt.datetime.now(dt.UTC),
             "p": json.dumps(body), "e": json.dumps(env), "d": ed})).scalar_one()
        await conn.execute(text("UPDATE inventory.repo_profiles SET is_current = false WHERE repo_id = :r AND "
                                "is_current AND profile_id <> :p"), {"r": repo_id, "p": pid})
        await conn.execute(text("UPDATE inventory.repo_profiles SET is_current = true WHERE profile_id = :p"),
                           {"p": pid})
        classified = []
        for it in items:
            iid = await upsert_item(conn, it)
            classified.append({"item_id": iid, "kind": it["kind"], "identity": it["identity"], "class": it["class"],
                               "default_action": it["default_action"]})
        keep = [c["item_id"] for c in classified]
        await conn.execute(text(
            "DELETE FROM inventory.repo_items ri USING inventory.catalog c WHERE c.item_id = ri.item_id AND "
            "ri.repo_id = :r AND c.kind <> 'probe_failure' AND NOT (ri.item_id = ANY(:keep))"),
            {"r": repo_id, "keep": keep})
        for iid in keep:
            await conn.execute(text(
                "INSERT INTO inventory.repo_items (tenant_id, repo_id, item_id, profile_id) VALUES (:t, :r, :i, :p) "
                "ON CONFLICT (repo_id, item_id) DO UPDATE SET profile_id = EXCLUDED.profile_id, seen_at = now()"),
                {"t": tenant, "r": repo_id, "i": iid, "p": pid})
        cur = (await conn.execute(text("SELECT env_digest, status FROM inventory.readiness WHERE repo_id = :r FOR "
                                       "UPDATE"), {"r": repo_id})).first()
        if cur is None or cur.env_digest != ed:
            # the environment or its commands changed: probe again (an unsupported toolchain cannot be probed)
            status = "pending" if env["supported"] else "warnings"
            causes = [] if env["supported"] else [{"stage": "environment", "cause": "unsupported",
                                                   "detail": env["unsupported_reason"]}]
            await conn.execute(text(
                "INSERT INTO inventory.readiness (repo_id, tenant_id, profile_id, env_digest, status, causes, attempts, "
                "next_attempt_at, probe_job_id, last_error, updated_at) VALUES (:r, :t, :p, :d, :s, CAST(:c AS jsonb), "
                "0, now(), NULL, NULL, now()) ON CONFLICT (repo_id) DO UPDATE SET profile_id = EXCLUDED.profile_id, "
                "env_digest = EXCLUDED.env_digest, status = EXCLUDED.status, causes = EXCLUDED.causes, attempts = 0, "
                "next_attempt_at = now(), probe_job_id = NULL, last_error = NULL, updated_at = now()"),
                {"r": repo_id, "t": tenant, "p": pid, "d": ed, "s": status, "c": json.dumps(causes)})
        else:
            await conn.execute(text("UPDATE inventory.readiness SET profile_id = :p, updated_at = now() WHERE "
                                    "repo_id = :r"), {"p": pid, "r": repo_id})
    log.info(json.dumps({"event": "inventory.profile_stored", "repo_id": repo_id, "profile_id": pid,
                         "environment": env.get("environment"), "items": len(classified)}))
    return {"profile_id": pid, "environment": {k: env[k] for k in ("environment", "version", "commands", "source",
                                                                   "supported", "unsupported_reason")},
            "classified": classified}


async def current_profile(conn: Any, repo_id: str) -> Any:
    return (await conn.execute(text(
        "SELECT p.profile_id, p.sha, p.default_branch, p.profile, p.environment, p.env_digest, p.scanned_at, "
        "r.status AS readiness, r.causes, r.probe_job_id, r.probed_at, r.probe_requested_at FROM "
        "inventory.repo_profiles p LEFT JOIN inventory.readiness r ON r.repo_id = p.repo_id WHERE p.repo_id = :r AND "
        "p.is_current"), {"r": repo_id})).first()


async def repo_profile_for_job(db: Any, repo_id: str, eff: dict[str, Any] | None) -> dict[str, Any] | None:
    """Contract §4 `JobRequest.repo_profile` for a job on `repo_id` (None when the repo was never profiled)."""
    if not await db.has_relation("inventory.repo_profiles"):
        return None
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        cur = await current_profile(conn, repo_id)
        if cur is None:
            return None
        servers = cur.profile.get("mcp_servers") or []
        idents = [I.mcp_identity(s) for s in servers]
        rows = (await conn.execute(text(
            "SELECT c.identity, c.default_action, d.decision, d.map_to FROM inventory.catalog c LEFT JOIN "
            "inventory.decisions d ON d.item_id = c.item_id AND d.is_current WHERE c.kind = 'mcp_server' AND "
            "c.identity = ANY(:i)"), {"i": idents})).all() if idents else []
    allowed = {r.identity for r in rows if I.effective_action(r.default_action, r.decision, r.map_to) == "allow"}
    env = cur.environment or {}
    repo_config = str(((eff or {}).get("repo_config")) or "use")
    return {"profile_id": cur.profile_id, "sha": cur.sha, "environment": env.get("environment"),
            "commands": env.get("commands") or dict.fromkeys(I.STAGES), "repo_config": repo_config,
            "config_base_sha": cur.sha,
            "mcp_auto_allow": [{"name": s.get("name"), "command": s.get("command"), "args": s.get("args") or []}
                               for s in servers if I.mcp_identity(s) in allowed and s.get("command")],
            "readiness": cur.readiness or "pending"}


# ------------------------------------------------------------------------------------------------ internal
@router.put("/internal/v1/repos/{repo_id}/profile")
async def put_profile(repo_id: str, request: Request, body: Any = Body(...),
                      principal: Principal = require("service:github-integration")) -> dict[str, Any]:
    if not REPO_ID_RE.match(repo_id):
        raise ProblemError(404, "not_found")
    errs = _check_profile(repo_id, body)
    if errs:
        raise ProblemError(422, "invalid_profile", errors=[{"path": "/", "message": e} for e in errs])
    owner = await repo_owner(request.app.state.db, repo_id)
    if owner is None:
        raise ProblemError(404, "unknown_repo", detail="the repo is neither in a project nor registered")
    return await store_profile(request.app.state.db, repo_id, body, owner)


async def _job_tenant(db: Any, job_id: str) -> str | None:
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        return (await conn.execute(text("SELECT tenant_id FROM intake.job_requests WHERE job_id = :j"),
                                   {"j": job_id})).scalar()


def _observed_item(it: dict[str, Any]) -> dict[str, Any]:
    """A catalogue item for an observation of an item the scanner never saw (classified from what happened)."""
    kind, status, cause = it["kind"], it["status"], it.get("cause")
    cls = {"mcp_server": "review", "hook": "ok" if status != "failed" else "at_risk",
           "toolchain": "supported" if status != "failed" else "unsupported",
           "probe_failure": cause if cause in I.CLASSES["probe_failure"] else "build_failure"}[kind]
    return {"kind": kind, "identity": it["identity"], "class": cls,
            "default_action": I.DEFAULT_ACTION.get(cls, "warn"), "details": {"observed": True}}


@router.post("/internal/v1/inventory/observations", status_code=202)
async def post_observations(request: Request, body: Any = Body(...),
                            principal: Principal = require("service:replicant-worker")) -> dict[str, Any]:
    if not isinstance(body, dict) or not JOB_RE.match(str(body.get("job_id", ""))) or \
            not REPO_ID_RE.match(str(body.get("repo_id", ""))) or not isinstance(body.get("items"), list) or \
            len(body["items"]) > 500:
        raise ProblemError(422, "invalid_observations")
    items = []
    for it in body["items"]:
        if not isinstance(it, dict) or it.get("kind") not in KINDS or it.get("status") not in STATUSES or \
                not isinstance(it.get("identity"), str) or not 1 <= len(it["identity"]) <= 400:
            raise ProblemError(422, "invalid_observations", detail="items[]: {kind, identity, status, cause}")
        items.append(it)
    db = request.app.state.db
    tenant = await _job_tenant(db, body["job_id"])
    if tenant is None:
        raise ProblemError(404, "unknown_job")
    n = await record_observations(db, tenant, body, items)
    return {"accepted": n}


async def record_observations(db: Any, tenant: str, body: dict[str, Any], items: list[dict[str, Any]]) -> int:
    n = 0
    async with db.tx(role="svc_cpa_tenancy", tenant=tenant) as conn:
        for it in items:
            iid = await upsert_item(conn, _observed_item(it), reclassify=False)
            res = await conn.execute(text(
                "INSERT INTO inventory.observations (tenant_id, job_id, run_id, repo_id, workflow_type, role, item_id, "
                "status, cause, tier, model, settings_hash) VALUES (:t, :j, :run, :r, :wt, :role, :i, :s, :c, :tier, "
                ":m, :h) ON CONFLICT (job_id, run_id, item_id) DO NOTHING"),
                {"t": tenant, "j": body["job_id"], "run": str(body.get("run_id") or "")[:64],
                 "r": str(body["repo_id"]), "wt": (str(body["workflow_type"])[:40] if body.get("workflow_type")
                                                   else None),
                 "role": str(body.get("role") or "")[:40] or None, "i": iid, "s": it["status"],
                 "c": str(it.get("cause"))[:200] if it.get("cause") else None,
                 "tier": str(body.get("tier") or "")[:40] or None, "m": str(body.get("model") or "")[:120] or None,
                 "h": str(body.get("settings_hash") or "")[:80] or None})
            n += res.rowcount or 0
    return n


@router.put("/internal/v1/repos/{repo_id}/readiness")
async def put_readiness(repo_id: str, request: Request, body: Any = Body(...),
                        principal: Principal = require("service:replicant-worker")) -> dict[str, Any]:
    if not isinstance(body, dict) or body.get("status") not in ("ready", "warnings", "blocked") or \
            not JOB_RE.match(str(body.get("job_id", ""))) or not isinstance(body.get("causes", []), list):
        raise ProblemError(422, "invalid_readiness")
    db = request.app.state.db
    owner = await repo_owner(db, repo_id)
    if owner is None:
        raise ProblemError(404, "unknown_repo")
    tenant = owner["tenant_id"]
    causes = [{"stage": str(c.get("stage") or "")[:40], "cause": str(c.get("cause") or "")[:40],
               "detail": (str(c["detail"])[:200] if c.get("detail") else None)}
              for c in body.get("causes") or [] if isinstance(c, dict)][:50]
    async with db.tx(role="svc_cpa_tenancy", tenant=tenant) as conn:
        cur = await current_profile(conn, repo_id)
        environment = ((cur.environment or {}).get("environment")) if cur is not None else None
        row = (await conn.execute(text(
            "UPDATE inventory.readiness SET status = :s, causes = CAST(:c AS jsonb), probed_at = now(), probe_job_id = "
            "coalesce(probe_job_id, :j), updated_at = now(), profile_id = coalesce(:p, profile_id) WHERE repo_id = :r "
            "AND (probe_job_id IS NULL OR probe_job_id = :j OR status <> 'probing') RETURNING repo_id"),
            {"s": body["status"], "c": json.dumps(causes), "j": body["job_id"], "r": repo_id,
             "p": body.get("profile_id") if str(body.get("profile_id", "")).startswith("rp_") else None})).first()
        if row is None:
            exists = (await conn.execute(text("SELECT probe_job_id FROM inventory.readiness WHERE repo_id = :r"),
                                         {"r": repo_id})).first()
            if exists is None:
                await conn.execute(text(
                    "INSERT INTO inventory.readiness (repo_id, tenant_id, status, causes, probe_job_id, probed_at) "
                    "VALUES (:r, :t, :s, CAST(:c AS jsonb), :j, now())"),
                    {"r": repo_id, "t": tenant, "s": body["status"], "c": json.dumps(causes), "j": body["job_id"]})
            else:
                log.info("readiness result of %s for %s ignored: a newer probe %s is running", body["job_id"], repo_id,
                         exists.probe_job_id)
                return {"repo_id": repo_id, "status": "ignored_stale_probe"}
        # probe failures: fleet catalogue items linked to the repo, and observations of the probe job
        failed = [c for c in causes if c["cause"] in I.CLASSES["probe_failure"]]
        ids = []
        for c in failed:
            ids.append(await upsert_item(conn, I.probe_item(environment, c["cause"], c["detail"])))
        await conn.execute(text(
            "DELETE FROM inventory.repo_items ri USING inventory.catalog c WHERE c.item_id = ri.item_id AND "
            "ri.repo_id = :r AND c.kind = 'probe_failure' AND NOT (ri.item_id = ANY(:keep))"), {"r": repo_id, "keep": ids})
        for iid, c in zip(ids, failed, strict=True):
            await conn.execute(text(
                "INSERT INTO inventory.repo_items (tenant_id, repo_id, item_id, profile_id) VALUES (:t, :r, :i, :p) "
                "ON CONFLICT (repo_id, item_id) DO UPDATE SET seen_at = now()"),
                {"t": tenant, "r": repo_id, "i": iid, "p": cur.profile_id if cur is not None else None})
            await conn.execute(text(
                "INSERT INTO inventory.observations (tenant_id, job_id, run_id, repo_id, workflow_type, role, item_id, "
                "status, cause) VALUES (:t, :j, '', :r, 'qa_only', 'qa', :i, 'failed', :c) ON CONFLICT DO NOTHING"),
                {"t": tenant, "j": body["job_id"], "r": repo_id, "i": iid, "c": c["cause"]})
    return {"repo_id": repo_id, "status": body["status"]}


# ------------------------------------------------------------------------------------------------ readiness probes
def readiness_request(tenant_id: str, repo: dict[str, Any], repo_id: str, branch: str, sha: str, digest: str,
                      requester: str | None = None, fresh: bool = False) -> dict[str, Any]:
    """The probe JobRequest (C-JOBREQ): qa_only, label replicant-readiness, pinned to the profiled commit. Automatic
    probes run as the in-process service principal; a re-check runs as the person who asked."""
    owner, _, name = repo["full_name"].partition("/")
    now = dt.datetime.now(dt.UTC)
    # automatic probes dedupe per environment digest; a person's re-check always starts a new probe
    nonce = uuid.uuid4().hex[:12] if requester or fresh else digest.split(":")[-1][:16]
    jr: dict[str, Any] = {
        "schema_version": "replicant.job-request/v1", "request_id": ulid("jrq"), "tenant_id": tenant_id,
        "workflow_type": "qa_only", "priority": "low",
        "received_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "dedupe_key": f"readiness:{repo_id}:{sha[:12]}:{nonce}",
        "source": {"channel": "ui" if requester else "schedule", "event_type": "inventory.readiness",
                   "event_id": uuid.uuid4().hex},
        "requester": {"kind": "user", "principal_id": requester} if requester else
        {"kind": "service", "principal_id": READINESS_PRINCIPAL, "on_behalf_of": READINESS_PRINCIPAL},
        "intent": {"title": f"Readiness probe of {repo['full_name']}", "format": "markdown", "untrusted": True,
                   "text": "Bootstrap the inferred environment and run the repository's install, build and test "
                           "commands in the sandbox, without a model. Report ready, warnings or blocked with causes."},
        "targets": [{"repo": {"host": repo["host"], "owner": owner, "name": name, "repo_id": repo_id,
                              "provider": repo.get("provider") or "gitea"}, "base_ref": branch, "base_sha": sha}],
        "constraints": {"labels": [READINESS_LABEL]}}
    return jr


def readiness_principal() -> Principal:
    from control_plane_api.auth import service_principal
    # job:create plus what a project's min_trigger_role may ask for; it has no token, so it is reachable in-process only
    return service_principal(READINESS_PRINCIPAL, "job:create", "gate:decide", "tenant:policy:edit",
                             method="in_process")


async def launch_probe(state: Any, repo_id: str, requester: Principal | None = None,
                       fresh: bool = False) -> dict[str, Any]:
    """Submit the probe for the repo's current profile; marks readiness `probing`. Raises ProblemError."""
    db = state.db
    owner = await repo_owner(db, repo_id)
    if owner is None:
        raise ProblemError(404, "unknown_repo")
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        cur = await current_profile(conn, repo_id)
    if cur is None:
        raise ProblemError(409, "repo_not_profiled", detail="the inventory scanner has not profiled this repo yet")
    if not (cur.environment or {}).get("supported"):
        raise ProblemError(409, "environment_unsupported", detail=(cur.environment or {}).get("unsupported_reason"))
    jr = readiness_request(owner["tenant_id"], owner, repo_id, cur.default_branch, cur.sha, cur.env_digest,
                           requester.principal_id if requester is not None else None, fresh=fresh)
    res = await state.intake.submit(requester or readiness_principal(), jr)
    job_id = (res.body or {}).get("job_id")
    async with db.tx(role="svc_cpa_tenancy", tenant=owner["tenant_id"]) as conn:
        await conn.execute(text(
            "INSERT INTO inventory.readiness (repo_id, tenant_id, profile_id, env_digest, status, probe_job_id, "
            "probe_requested_at, attempts, last_error) VALUES (:r, :t, :p, :d, 'probing', :j, now(), 1, NULL) ON "
            "CONFLICT (repo_id) DO UPDATE SET status = 'probing', probe_job_id = :j, probe_requested_at = now(), "
            "attempts = inventory.readiness.attempts + 1, last_error = NULL, profile_id = :p, env_digest = :d, "
            "updated_at = now()"),
            {"r": repo_id, "t": owner["tenant_id"], "p": cur.profile_id, "d": cur.env_digest, "j": job_id})
    return {"job_id": job_id, "status": res.status_code}


async def launch_pending(state: Any, limit: int = 20) -> int:
    """Readiness launcher body: probe every repo whose readiness is `pending` (retry with backoff on failure)."""
    db = state.db
    if not await db.has_relation("inventory.readiness"):
        return 0
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        rows = (await conn.execute(text(
            "SELECT repo_id, tenant_id, attempts FROM inventory.readiness WHERE status = 'pending' AND next_attempt_at "
            "<= now() ORDER BY next_attempt_at LIMIT :n"), {"n": limit})).all()
    n = 0
    for r in rows:
        try:
            await launch_probe(state, r.repo_id)
            n += 1
        except Exception as e:  # noqa: BLE001 - ProblemError or transient: back off and retry
            err = f"{getattr(e, 'code', type(e).__name__)}: {str(getattr(e, 'detail', '') or e)[:300]}"
            backoff = min(MAX_ATTEMPTS_BACKOFF_S, 30 * 2 ** min(int(r.attempts), 7))
            log.warning("readiness probe for %s not started: %s", r.repo_id, err)
            async with db.tx(role="svc_cpa_tenancy", tenant=r.tenant_id) as conn:
                await conn.execute(text(
                    "UPDATE inventory.readiness SET attempts = attempts + 1, last_error = :e, next_attempt_at = now() + "
                    "make_interval(secs => :b), updated_at = now() WHERE repo_id = :r AND status = 'pending'"),
                    {"e": err[:500], "b": backoff, "r": r.repo_id})
    return n


async def readiness_loop(state: Any, interval_s: float = 10.0) -> None:
    await locked_loop(state.db, READINESS_LOCK, interval_s, lambda: launch_pending(state), "readiness launcher")


# ------------------------------------------------------------------------------------------------ admin gating
def is_fleet_admin(principal: Principal) -> bool:
    from control_plane_api.routes_memory import is_memory_admin
    return is_memory_admin(principal)


def require_fleet_admin() -> Any:
    async def dep(request: Request, principal: Principal = Depends(current_principal)) -> Principal:
        if is_fleet_admin(principal):
            return principal
        from control_plane_api.routes_memory import ADMIN_GROUP
        db = request.app.state.db
        async with db.tx(role="svc_cpa_tenancy") as conn:
            aid = await audit.emit(conn, db, {
                "event_type": "inventory.admin_denied", "outcome": "denied", "tenant_id": "replicant-system",
                "actor_principal_id": principal.principal_id, "channel": "ui", "reason": "not_in_admin_group",
                "details": {"method": request.method, "path": request.url.path[:256], "required_group": ADMIN_GROUP}})
        raise ProblemError(403, "not_fleet_admin", detail=f"fleet administration requires membership of {ADMIN_GROUP}",
                           audit_id=aid)

    dep.__replicant_permission__ = "fleet:admin"  # type: ignore[attr-defined]
    return Depends(dep)


# ------------------------------------------------------------------------------------------------ impact SQL
IMPACT_SQL = """
WITH obs AS (
  SELECT DISTINCT o.item_id, o.job_id, o.repo_id FROM inventory.observations o WHERE o.item_id = ANY(:ids)
),
outc AS (
  SELECT j.job_id, j.workflow_type, j.repo_ids,
         (j.status IN ('failed', 'expired'))::int AS failed,
         (j.status = 'rejected' OR EXISTS (SELECT 1 FROM approvals.gates g WHERE g.job_id = j.job_id AND
            g.kind = 'publish_approval' AND g.state IN ('rejected', 'changes_requested')))::int AS rejected,
         coalesce(j.repair_used, 0) AS repair_cycles, q.first_pass
    FROM jobs_read.jobs j
    LEFT JOIN LATERAL (SELECT (r.qa_verdict IN ('pass', 'pass_with_warnings'))::int AS first_pass
                         FROM jobs_read.job_runs r WHERE r.job_id = j.job_id AND r.qa_verdict IS NOT NULL AND
                              r.qa_verdict <> 'not_applicable' ORDER BY r.cycle, r.ordinal LIMIT 1) q ON true
   WHERE j.is_terminal AND j.evaluation_mode IS NULL
     AND j.job_id IN (SELECT job_id FROM obs UNION
                      SELECT j2.job_id FROM jobs_read.jobs j2 WHERE j2.repo_ids && (SELECT array_agg(DISTINCT repo_id)
                                                                                     FROM obs))
),
w AS (
  SELECT o.item_id, count(*) AS n, sum(c.failed) AS failed, sum(c.rejected) AS rejected,
         sum(c.repair_cycles) AS repair_cycles, count(c.first_pass) AS qa_jobs, coalesce(sum(c.first_pass), 0) AS qa_fp
    FROM (SELECT DISTINCT item_id, job_id FROM obs) o JOIN outc c USING (job_id) GROUP BY o.item_id
),
strata AS (SELECT DISTINCT o.item_id, o.repo_id, c.workflow_type FROM obs o JOIN outc c USING (job_id)),
wo AS (
  SELECT x.item_id, count(*) AS n, sum(x.failed) AS failed, sum(x.rejected) AS rejected,
         sum(x.repair_cycles) AS repair_cycles, count(x.first_pass) AS qa_jobs, coalesce(sum(x.first_pass), 0) AS qa_fp
    FROM (SELECT DISTINCT s.item_id, c.job_id, c.failed, c.rejected, c.repair_cycles, c.first_pass
            FROM strata s JOIN outc c ON c.workflow_type = s.workflow_type AND s.repo_id = ANY(c.repo_ids)
           WHERE NOT EXISTS (SELECT 1 FROM obs o2 WHERE o2.item_id = s.item_id AND o2.job_id = c.job_id)) x
   GROUP BY x.item_id
)
SELECT i.item_id, coalesce(w.n, 0) AS jobs_with, coalesce(wo.n, 0) AS jobs_without,
       coalesce(w.failed, 0) AS w_failed, coalesce(wo.failed, 0) AS wo_failed,
       coalesce(w.rejected, 0) AS w_rejected, coalesce(wo.rejected, 0) AS wo_rejected,
       coalesce(w.repair_cycles, 0) AS w_repair_cycles, coalesce(wo.repair_cycles, 0) AS wo_repair_cycles,
       coalesce(w.qa_jobs, 0) AS w_qa_jobs, coalesce(wo.qa_jobs, 0) AS wo_qa_jobs,
       coalesce(w.qa_fp, 0) AS w_qa_first_pass, coalesce(wo.qa_fp, 0) AS wo_qa_first_pass,
       (SELECT count(DISTINCT job_id) FROM obs WHERE obs.item_id = i.item_id) AS jobs_observed
  FROM unnest(CAST(:ids AS text[])) AS i(item_id)
  LEFT JOIN w ON w.item_id = i.item_id LEFT JOIN wo ON wo.item_id = i.item_id
"""


async def impact_aggregates(conn: Any, ids: list[str]) -> dict[str, dict[str, Any]]:
    """Per item: terminal jobs that carried it vs comparable terminal jobs without it (same repo and workflow type)
    and their outcomes: failed, publish gate rejected or changes requested, repair cycles, QA first pass."""
    if not ids:
        return {}
    rows = (await conn.execute(text(IMPACT_SQL), {"ids": ids})).all()
    return {r.item_id: {k: (float(v) if not isinstance(v, int | str) and v is not None else v)
                        for k, v in r._mapping.items()} for r in rows}


# ------------------------------------------------------------------------------------------------ admin endpoints
ITEM_COLS = ("SELECT c.item_id, c.kind, c.identity, c.class, c.default_action, c.details, c.first_seen, c.last_seen, "
             "d.decision, d.map_to, (SELECT count(*) FROM inventory.repo_items ri WHERE ri.item_id = c.item_id) AS "
             "repos, (SELECT count(DISTINCT o.job_id) FROM inventory.observations o WHERE o.item_id = c.item_id) AS "
             "jobs, (SELECT count(*) FROM inventory.support_requests s WHERE s.item_id = c.item_id AND s.status = "
             "'open') AS requests FROM inventory.catalog c LEFT JOIN inventory.decisions d ON d.item_id = c.item_id "
             "AND d.is_current")


def _item_doc(r: Any, imp: dict[str, Any] | None) -> dict[str, Any]:
    m = r._mapping
    decision = None if m["decision"] is None else (f"map:{m['map_to']}" if m["decision"] == "map" else m["decision"])
    return {"id": m["item_id"], "kind": m["kind"], "identity": m["identity"], "class": m["class"],
            "default": m["default_action"], "decision": decision,
            "effective": I.effective_action(m["default_action"], m["decision"], m["map_to"]),
            "details": m["details"] or {}, "repos": int(m["repos"]), "jobs": int(m["jobs"]),
            "impact": I.impact(imp or {}, m["kind"], int(m["repos"])),
            "first_seen": _iso(m["first_seen"]), "last_seen": _iso(m["last_seen"]), "requests": int(m["requests"])}


async def _items(db: Any, where: str = "", params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        rows = (await conn.execute(text(ITEM_COLS + where), params or {})).all()
        agg = await impact_aggregates(conn, [r.item_id for r in rows])
    return [_item_doc(r, agg.get(r.item_id)) for r in rows]


@router.get("/v1/admin/fleet/items")
async def list_items(request: Request, kind: str | None = None, sort: str = "impact", limit: int = 50,
                     cursor: str | None = None, principal: Principal = require_fleet_admin()) -> dict[str, Any]:
    cls = request.query_params.get("class")
    if kind is not None and kind not in KINDS:
        raise ProblemError(422, "invalid_request", detail=f"kind is one of {', '.join(KINDS)}")
    if sort not in ("impact", "repos", "recent"):
        raise ProblemError(422, "invalid_request", detail="sort is impact, repos or recent")
    limit = max(1, min(int(limit), 200))
    try:
        offset = max(0, int(cursor)) if cursor else 0
    except ValueError:
        raise ProblemError(422, "invalid_cursor") from None
    db = request.app.state.db
    if not await db.has_relation("inventory.catalog"):
        return {"items": [], "next": None}
    conds, params = [], {}
    if kind:
        conds.append("c.kind = :k")
        params["k"] = kind
    if cls:
        conds.append("c.class = :c")
        params["c"] = cls
    items = await _items(db, (" WHERE " + " AND ".join(conds) if conds else "") + " LIMIT 5000", params)
    key = {"impact": lambda x: (-x["impact"]["score"], -x["repos"], x["identity"]),
           "repos": lambda x: (-x["repos"], -x["impact"]["score"], x["identity"]),
           "recent": lambda x: (x["last_seen"] or "",)}[sort]
    items.sort(key=key, reverse=(sort == "recent"))
    page = items[offset:offset + limit]
    return {"items": page, "next": str(offset + limit) if offset + limit < len(items) else None}


async def _item_detail(db: Any, item_id: str) -> dict[str, Any]:
    found = await _items(db, " WHERE c.item_id = :i", {"i": item_id})
    if not found:
        raise ProblemError(404, "not_found")
    doc = found[0]
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        repos = (await conn.execute(text(
            "SELECT ri.repo_id, coalesce(pr.full_name, p.full_name) AS full_name, pr.project_id FROM "
            "inventory.repo_items ri LEFT JOIN tenancy.project_repos pr ON pr.repo_id = ri.repo_id LEFT JOIN "
            "inventory.repo_profiles p ON p.repo_id = ri.repo_id AND p.is_current WHERE ri.item_id = :i ORDER BY 2 "
            "LIMIT 200"), {"i": item_id})).all()
        jobs = (await conn.execute(text(
            "SELECT o.job_id, max(o.observed_at) AS at, j.status, j.terminal_outcome FROM inventory.observations o LEFT "
            "JOIN jobs_read.jobs j ON j.job_id = o.job_id WHERE o.item_id = :i GROUP BY o.job_id, j.status, "
            "j.terminal_outcome ORDER BY 2 DESC LIMIT 20"), {"i": item_id})).all()
        decisions = (await conn.execute(text(
            "SELECT decision_id, decision, map_to, reason, actor, created_at, is_current FROM inventory.decisions WHERE "
            "item_id = :i ORDER BY created_at DESC LIMIT 50"), {"i": item_id})).all()
        reqs = (await conn.execute(text(
            "SELECT request_id, tenant_id, project_id, reason, actor, status, created_at FROM "
            "inventory.support_requests WHERE item_id = :i ORDER BY created_at DESC LIMIT 50"), {"i": item_id})).all()
    doc["repos"] = [{"repo_id": r.repo_id, "full_name": r.full_name, "project_id": r.project_id} for r in repos]
    doc["repo_count"] = len(doc["repos"])
    doc["recent_jobs"] = [{"job_id": r.job_id, "status": r.status, "outcome": r.terminal_outcome, "at": _iso(r.at)}
                          for r in jobs]
    doc["decisions"] = [{"decision_id": r.decision_id, "decision": r.decision, "map_to": r.map_to, "reason": r.reason,
                         "actor": r.actor, "at": _iso(r.created_at), "current": r.is_current} for r in decisions]
    doc["requests"] = [{"request_id": r.request_id, "tenant_id": r.tenant_id, "project_id": r.project_id,
                        "reason": r.reason, "actor": r.actor, "status": r.status, "at": _iso(r.created_at)}
                       for r in reqs]
    return doc


@router.get("/v1/admin/fleet/items/{item_id}")
async def get_item(item_id: str, request: Request, principal: Principal = require_fleet_admin()) -> dict[str, Any]:
    if not ITEM_RE.match(item_id):
        raise ProblemError(404, "not_found")
    return await _item_detail(request.app.state.db, item_id)


@router.post("/v1/admin/fleet/items/{item_ref}")
async def decide_item(item_ref: str, request: Request, body: Any = Body(...),
                      principal: Principal = require_fleet_admin()) -> dict[str, Any]:
    item_id, _, verb = item_ref.partition(":")
    if verb != "decide" or not ITEM_RE.match(item_id):
        raise ProblemError(404, "not_found")
    if not isinstance(body, dict) or body.get("decision") not in ("approve", "deny", "map"):
        raise ProblemError(422, "invalid_request", detail="decision is approve, deny or map")
    reason = str(body.get("reason") or "").strip()
    if not 1 <= len(reason) <= 1000:
        raise ProblemError(422, "reason_required")
    map_to = body.get("map_to")
    if body["decision"] == "map" and not (isinstance(map_to, str) and re.match(r"^[a-z][a-z0-9_-]{1,40}$", map_to)):
        raise ProblemError(422, "invalid_request", detail="map needs map_to: a platform service name")
    if body["decision"] != "map":
        map_to = None
    db = request.app.state.db
    async with db.tx(role="svc_cpa_tenancy") as conn:
        kind = (await conn.execute(text("SELECT kind FROM inventory.catalog WHERE item_id = :i FOR UPDATE"),
                                   {"i": item_id})).scalar()
        if kind is None:
            raise ProblemError(404, "not_found")
        did = ulid("ivd")
        aid = await audit.emit(conn, db, {
            "event_type": "inventory.item_decided", "outcome": "success", "tenant_id": "replicant-system",
            "actor_principal_id": principal.principal_id, "channel": "ui", "target_kind": "inventory_item",
            "target_id": item_id,
            "details": {"decision": body["decision"], "map_to": map_to, "kind": kind, "decision_id": did,
                        "reason": reason[:500]}})
        await conn.execute(text("UPDATE inventory.decisions SET is_current = false WHERE item_id = :i AND is_current"),
                           {"i": item_id})
        await conn.execute(text(
            "INSERT INTO inventory.decisions (decision_id, item_id, decision, map_to, reason, actor, audit_id) VALUES "
            "(:d, :i, :dec, :m, :r, :a, :aid)"),
            {"d": did, "i": item_id, "dec": body["decision"], "m": map_to, "r": reason, "a": principal.principal_id,
             "aid": aid})
    return await _item_detail(db, item_id)


@router.post("/v1/admin/fleet:rescan", status_code=202)
async def rescan(request: Request, body: Any = Body(default=None),
                 principal: Principal = require_fleet_admin()) -> dict[str, Any]:
    ids = (body or {}).get("repo_ids") if isinstance(body, dict) else None
    if ids is not None and (not isinstance(ids, list) or not all(REPO_ID_RE.match(str(x)) for x in ids)):
        raise ProblemError(422, "invalid_request", detail="repo_ids: a list of repo ids")
    ns = request.app.state.notify.ns
    tok = read_secret(ns.delivery_token_file)
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{ns.github_integration_url}/internal/v1/inventory:rescan",
                             json={"repo_ids": [str(x) for x in ids]} if ids is not None else {},
                             headers={"Authorization": f"Bearer {tok}"} if tok else {})
    except httpx.HTTPError as e:
        raise ProblemError(503, "github_integration_unavailable", detail=type(e).__name__) from None
    if r.status_code >= 300:
        raise ProblemError(502, "rescan_rejected", detail=r.text[:300])
    queued = (r.json() or {}).get("queued")
    db = request.app.state.db
    async with db.tx(role="svc_cpa_tenancy") as conn:
        aid = await audit.emit(conn, db, {
            "event_type": "inventory.rescan_requested", "outcome": "success", "tenant_id": "replicant-system",
            "actor_principal_id": principal.principal_id, "channel": "ui",
            "details": {"repo_ids": [str(x) for x in ids][:100] if ids is not None else "all", "queued": queued}})
    return {"queued": queued, "audit_id": aid}


# ------------------------------------------------------------------------------------------------ project endpoints
@router.get("/v1/projects/{project_id}/readiness")
async def project_readiness(project_id: str, request: Request,
                            principal: Principal = require("job:read|dev:seed")) -> dict[str, Any]:
    from control_plane_api import projects as P
    from control_plane_api.routes_projects import _project
    row = await _project(request, principal, project_id)
    db = request.app.state.db
    repos = await P.project_repos(db, project_id)
    out = []
    if not await db.has_relation("inventory.repo_profiles"):
        return {"repos": [{"repo_id": r["repo_id"], "full_name": r["full_name"], "status": "unknown"} for r in repos]}
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        cur_cfg = await P.current_configs(conn, project_id)
        for r in repos:
            cur = await current_profile(conn, r["repo_id"])
            eff = P.merge(cur_cfg.get("", (0, {}))[1], cur_cfg.get(r["repo_id"], (0, {}))[1])
            if cur is None:
                out.append({"repo_id": r["repo_id"], "full_name": r["full_name"], "status": "unknown",
                            "profile_id": None, "sha": None, "environment": None, "repo_config": None,
                            "repo_config_mode": eff.get("repo_config", "use"), "warnings": [], "last_probe": None})
                continue
            items = (await conn.execute(text(
                "SELECT c.item_id, c.kind, c.identity, c.class, c.default_action, c.details, d.decision, d.map_to FROM "
                "inventory.repo_items ri JOIN inventory.catalog c ON c.item_id = ri.item_id LEFT JOIN "
                "inventory.decisions d ON d.item_id = c.item_id AND d.is_current WHERE ri.repo_id = :r ORDER BY c.kind, "
                "c.identity"), {"r": r["repo_id"]})).all()
            warnings = []
            for it in items:
                eff_action = I.effective_action(it.default_action, it.decision, it.map_to)
                if eff_action in ("skip", "warn", "block") or eff_action.startswith("map"):
                    warnings.append({"item_id": it.item_id, "kind": it.kind, "identity": it.identity,
                                     "class": it._mapping["class"], "effective": eff_action,
                                     "message": I.warning_message(it.kind, it._mapping["class"], it.details or {})})
            claude = (cur.profile or {}).get("claude") or {}
            env = cur.environment or {}
            out.append({
                "repo_id": r["repo_id"], "full_name": r["full_name"], "status": cur.readiness or "pending",
                "profile_id": cur.profile_id, "sha": cur.sha, "scanned_at": _iso(cur.scanned_at),
                "environment": {k: env.get(k) for k in ("environment", "version", "commands", "source", "supported",
                                                         "unsupported_reason")},
                "repo_config_mode": eff.get("repo_config", "use"),
                "repo_config": {"settings_present": claude.get("settings_present", False),
                                "hooks": [{"event": h.get("event"), "matcher": h.get("matcher"),
                                           "command": h.get("command"), "command_hash": h.get("command_hash")}
                                          for h in claude.get("hooks") or []],
                                "rules": claude.get("rules") or [], "skills": claude.get("skills") or [],
                                "commands": claude.get("commands") or [], "agents": claude.get("agents") or [],
                                "claude_md": claude.get("claude_md") or [],
                                "permission_rules": claude.get("permission_rules", 0),
                                "mcp_servers": [{"name": s.get("name"), "identity": s.get("identity"),
                                                 "transport": s.get("transport")}
                                                for s in (cur.profile or {}).get("mcp_servers") or []],
                                "agents_md": (cur.profile or {}).get("agents_md", False),
                                "errors": (cur.profile or {}).get("errors") or []},
                "warnings": warnings,
                "last_probe": None if not cur.probe_job_id else {
                    "job_id": cur.probe_job_id, "status": cur.readiness, "at": _iso(cur.probed_at or
                                                                                    cur.probe_requested_at),
                    "causes": cur.causes or []}})
    return {"project_id": row.project_id, "repos": out}


async def recheck(project_id: str, repo_id: str, request: Request, principal: Principal) -> Response:
    """POST /v1/projects/{p}/repos/{r}:recheck (dispatched from routes_memory.scan_repo, which owns the path)."""
    from control_plane_api import projects as P
    from control_plane_api.routes_projects import _can, _project
    row = await _project(request, principal, project_id)
    if not (_can(request, principal, row, "tenant:policy:edit") or is_fleet_admin(principal)):
        raise ProblemError(403, "missing_permission", detail="project admins or fleet admins may re-check readiness")
    if repo_id not in {r["repo_id"] for r in await P.project_repos(request.app.state.db, project_id)}:
        raise ProblemError(404, "not_found")
    # a person's re-check runs as that person when they may start jobs, else as the platform's probe principal
    from control_plane_api.auth import check
    as_user = principal.is_user and check(request.app.state.auth, principal, "job:create", tenant_id=row.tenant_id,
                                          repo_ids=(repo_id,))
    res = await launch_probe(request.app.state, repo_id, principal if as_user else None, fresh=True)
    db = request.app.state.db
    async with db.tx(role="svc_cpa_tenancy", tenant=row.tenant_id) as conn:
        await audit.emit(conn, db, {"event_type": "inventory.readiness_recheck", "outcome": "success",
                                    "tenant_id": row.tenant_id, "actor_principal_id": principal.principal_id,
                                    "channel": "ui", "job_id": res["job_id"], "target_kind": "repo",
                                    "target_id": repo_id, "details": {"project_id": project_id}})
    return Response(json.dumps({"job_id": res["job_id"]}), status_code=202, media_type="application/json")


@router.post("/v1/projects/{project_id}/fleet-items/{item_ref}", status_code=201)
async def request_support(project_id: str, item_ref: str, request: Request, body: Any = Body(...),
                          principal: Principal = require("tenant:policy:edit|dev:seed")) -> dict[str, Any]:
    from control_plane_api.routes_projects import _project
    item_id, _, verb = item_ref.partition(":")
    if verb != "request" or not ITEM_RE.match(item_id):
        raise ProblemError(404, "not_found")
    row = await _project(request, principal, project_id, "tenant:policy:edit")
    reason = str((body or {}).get("reason") or "").strip() if isinstance(body, dict) else ""
    if not 1 <= len(reason) <= 2000:
        raise ProblemError(422, "reason_required")
    db = request.app.state.db
    async with db.tx(role="svc_cpa_tenancy", tenant=row.tenant_id) as conn:
        if (await conn.execute(text("SELECT 1 FROM inventory.catalog WHERE item_id = :i"), {"i": item_id})).first() \
                is None:
            raise ProblemError(404, "not_found")
        rid = ulid("srq")
        aid = await audit.emit(conn, db, {"event_type": "inventory.support_requested", "outcome": "success",
                                          "tenant_id": row.tenant_id, "actor_principal_id": principal.principal_id,
                                          "channel": "ui", "target_kind": "inventory_item", "target_id": item_id,
                                          "details": {"project_id": project_id, "request_id": rid,
                                                      "reason": reason[:500]}})
        await conn.execute(text(
            "INSERT INTO inventory.support_requests (request_id, tenant_id, project_id, item_id, reason, actor, audit_id) "
            "VALUES (:r, :t, :p, :i, :why, :a, :aid)"),
            {"r": rid, "t": row.tenant_id, "p": project_id, "i": item_id, "why": reason, "a": principal.principal_id,
             "aid": aid})
    return {"request_id": rid, "item_id": item_id, "project_id": project_id, "status": "open", "audit_id": aid}
