"""memory-service HTTP API: the v0 subset of spec/04-memory-and-context/api/memory-service.openapi.yaml (MEM-D1).

Internal (service callers: replicant-worker, control-plane-api):
  POST /internal/v1/snapshots                         create the job's pinned snapshot (idempotent per job)
  POST /internal/v1/snapshots/{id}:select             revocation filter: {memory_snapshot_id (unchanged), revoked[]}
  POST /internal/v1/injections                        select + pack + render for one run; records memory.injections
  POST /internal/v1/candidates                        replicant.memory-candidates/v1 envelope -> candidates
  POST /internal/v1/findings                          reviewer/QA findings (recurrence -> negative candidates)
  POST /internal/v1/outcomes                          job outcome metrics
  GET  /internal/v1/jobs/{job_id}/memory?tenant_id=   entries injected into the job (read-only view)
Admin (control-plane-api with a signed admin principal, re-checked here; D-25):
  /internal/v1/admin/{candidates,entries,entries/{id}[:verb],quarantines,purges,exports,conflicts,analytics/{r}}
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from memory_service import gateway, store
from memory_service.auth import Auth, HttpError
from memory_service.config import Settings
from memory_service.db import Database
from memory_service.s3 import S3

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, db: Database | None = None, s3: S3 | None = None,
               auth: Auth | None = None, judge: Any = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = db or Database(settings)
    s3 = s3 or S3(settings)
    auth = auth or Auth(settings)

    async def _judge(**kw: Any) -> bool:
        return await gateway.same_meaning(settings, **kw)
    judge = judge or _judge

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        yield
        await db.dispose()

    app = FastAPI(title="memory-service", version="0.1.0", lifespan=lifespan)
    app.state.db, app.state.s3, app.state.auth = db, s3, auth

    @app.exception_handler(HttpError)
    async def _err(request: Request, e: HttpError) -> JSONResponse:
        return JSONResponse({"status": e.status, "code": e.code, "detail": e.detail, **e.extra}, status_code=e.status,
                            media_type="application/problem+json")

    async def body(request: Request) -> dict[str, Any]:
        try:
            b = await request.json()
        except ValueError:
            raise HttpError(400, "invalid_request", "JSON body required") from None
        if not isinstance(b, dict):
            raise HttpError(400, "invalid_request", "JSON object required")
        return b

    def need(b: dict[str, Any], *keys: str) -> None:
        missing = [k for k in keys if not b.get(k)]
        if missing:
            raise HttpError(400, "invalid_request", f"missing {', '.join(missing)}")

    def idem(request: Request) -> str:
        k = (request.headers.get("idempotency-key") or "").strip('"')
        if not 8 <= len(k) <= 128:
            raise HttpError(428, "idempotency_key_required", "Idempotency-Key (8-128 chars) is required")
        return k

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ok = await db.ping()
        return JSONResponse({"status": "ok" if ok else "unavailable", "db": ok}, status_code=200 if ok else 503)

    # ------------------------------------------------------------------------------------------ internal
    @app.post("/internal/v1/snapshots")
    async def snapshots(request: Request) -> dict[str, Any]:
        auth.caller(request)
        b = await body(request)
        need(b, "tenant_id", "job_id")
        return await store.create_snapshot(db, s3, b)

    @app.post("/internal/v1/snapshots/{snapshot_id}:select")
    async def snapshot_select(snapshot_id: str, request: Request) -> dict[str, Any]:
        auth.caller(request)
        b = await body(request)
        need(b, "tenant_id")
        return {"memory_snapshot_id": snapshot_id, "revoked": await store.select_revoked(db, b["tenant_id"], snapshot_id),
                "mode": "revocation_filter"}

    @app.post("/internal/v1/injections")
    async def injections(request: Request) -> dict[str, Any]:
        auth.caller(request)
        b = await body(request)
        need(b, "tenant_id", "job_id", "run_id", "snapshot_id", "role")
        return await store.inject(db, b)

    @app.post("/internal/v1/candidates")
    async def candidates(request: Request) -> dict[str, Any]:
        auth.caller(request)
        b = await body(request)
        need(b, "tenant_id", "job_id")
        return await store.write_candidates(db, settings, b, judge=judge)

    @app.post("/internal/v1/findings")
    async def findings(request: Request) -> dict[str, Any]:
        auth.caller(request)
        b = await body(request)
        need(b, "tenant_id", "job_id")
        return await store.findings(db, settings, b, judge=judge)

    @app.post("/internal/v1/outcomes")
    async def outcomes(request: Request) -> dict[str, Any]:
        auth.caller(request)
        b = await body(request)
        need(b, "tenant_id", "job_id")
        return await store.record_outcome(db, b)

    @app.get("/internal/v1/jobs/{job_id}/memory")
    async def job_memory(job_id: str, request: Request, tenant_id: str) -> dict[str, Any]:
        auth.caller(request)
        return await store.job_memory(db, tenant_id, job_id)

    # ------------------------------------------------------------------------------------------ admin
    @app.get("/internal/v1/admin/candidates")
    async def admin_candidates(request: Request) -> dict[str, Any]:
        auth.admin(request)
        q = dict(request.query_params)
        q["state"] = "candidate"
        return {"items": await store.list_entries(db, q)}

    @app.get("/internal/v1/admin/entries")
    async def admin_entries(request: Request) -> dict[str, Any]:
        auth.admin(request)
        return {"items": await store.list_entries(db, dict(request.query_params))}

    @app.post("/internal/v1/admin/entries")
    async def admin_create(request: Request) -> JSONResponse:
        a = auth.admin(request)
        return JSONResponse(await store.create_entry(db, a, await body(request), idem(request)), status_code=201)

    @app.get("/internal/v1/admin/entries/{entry_ref}")
    async def admin_entry(entry_ref: str, request: Request) -> dict[str, Any]:
        auth.admin(request)
        return await store.get_entry(db, entry_ref)

    @app.post("/internal/v1/admin/entries/{entry_ref}")
    async def admin_entry_action(entry_ref: str, request: Request) -> dict[str, Any]:
        a = auth.admin(request)
        eid, sep, verb = entry_ref.partition(":")
        if not sep:
            raise HttpError(404, "unknown_action", "POST /entries/{id}:<verb>")
        b = await body(request)
        if verb == "edit":
            return await store.edit_entry(db, a, eid, b, request.headers.get("if-match"), idem(request))
        return await store.entry_action(db, a, eid, verb, b, request.headers.get("if-match"), idem(request))

    @app.post("/internal/v1/admin/quarantines")
    async def admin_quarantine(request: Request) -> dict[str, Any]:
        a = auth.admin(request)
        b = await body(request)
        return await store.bulk_quarantine(db, a, b, idem(request) if not b.get("dry_run") else "dry-run-x")

    @app.post("/internal/v1/admin/purges")
    async def admin_purge(request: Request) -> dict[str, Any]:
        a = auth.admin(request)
        b = await body(request)
        return await store.purge(db, s3, a, b, idem(request) if not b.get("dry_run") else "dry-run-x")

    @app.post("/internal/v1/admin/exports")
    async def admin_export(request: Request) -> dict[str, Any]:
        a = auth.admin(request)
        return await store.export(db, s3, a, await body(request), idem(request))

    @app.get("/internal/v1/admin/conflicts")
    async def admin_conflicts(request: Request) -> dict[str, Any]:
        auth.admin(request)
        return {"items": await store.list_conflicts(db, dict(request.query_params))}

    @app.post("/internal/v1/admin/conflicts/{conflict_ref}")
    async def admin_conflict_resolve(conflict_ref: str, request: Request) -> dict[str, Any]:
        a = auth.admin(request)
        cid, _, verb = conflict_ref.partition(":")
        if verb != "resolve":
            raise HttpError(404, "unknown_action", verb)
        return await store.resolve_conflict(db, a, cid, await body(request), idem(request))

    @app.get("/internal/v1/admin/analytics/{report}")
    async def admin_analytics(report: str, request: Request) -> dict[str, Any]:
        auth.admin(request)
        return await store.analytics(db, report, dict(request.query_params))

    return app
