"""Core routes for the Runner (C-RUNNER 4.0.0, SIMPLIFY-D50: one cluster, no run grants). Callers: runner-local
(`svc_runner-local`) and, in a cluster, runner-api (`svc_runner-api`), each with its own internal caller token.

  GET /internal/v1/jobs/{job_id}/run-scope?tenant_id=T   the job's intake record as the Runner needs it to admit a
                                                         run: tenant, target repo_ids, acting_for, revoked. 404 for a
                                                         job of another tenant (indistinguishable from no job).
  GET /internal/v1/revocations                           job_ids whose status became cancelled/failed/taken_over and
                                                         run_ids cancelled or terminal with SecurityViolation, from
                                                         jobs_read (C-STATE rule 5); ETag + max-age=30. The Runner
                                                         copies every listed run of its own into its revocation feed,
                                                         the one feed run-token verifiers poll.

These replace the run-grants app (grants.py, its :8403 listener and its JWKS): the worker no longer signs what it
asks for; the Runner compares what it is asked for with what core accepted.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import text

from control_plane_api.auth import Principal, require
from control_plane_api.db import OPERATOR_SCOPE
from control_plane_api.problems import ProblemError

log = logging.getLogger(__name__)
router = APIRouter()

RUNNER = "service:runner-local|service:runner-api"
REVOKED_STATUSES = ("cancelled", "failed", "taken_over")
RETENTION = dt.timedelta(hours=48)   # longer than any run's overall deadline (startup + active + finalize + 300 s)


def acting_for(request_doc: dict[str, Any]) -> str | None:
    """The job's requester as the worker computes it (replicant_job.py): on_behalf_of, else principal_id."""
    req = request_doc.get("requester") or {}
    return req.get("on_behalf_of") or req.get("principal_id")


def target_repo_ids(request_doc: dict[str, Any]) -> list[str]:
    """The job's authorized targets as the worker computes them (replicant_job.py, at most 5)."""
    return [str(t["repo"]["repo_id"]) for t in request_doc.get("targets") or [] if (t.get("repo") or {}).get("repo_id")][:5]


@router.get("/internal/v1/jobs/{job_id}/run-scope")
async def run_scope(job_id: str, request: Request, tenant_id: str = Query(..., min_length=1, max_length=63),
                    principal: Principal = require(RUNNER)) -> dict[str, Any]:
    db = request.app.state.db
    async with db.tx(tenant=tenant_id) as conn:
        r = (await conn.execute(text("SELECT tenant_id, request FROM intake.job_requests WHERE job_id = :j AND tenant_id = :t"),
                                {"j": job_id, "t": tenant_id})).first()
        status = None
        if r is not None and await db.has_relation("jobs_read.jobs"):
            status = (await conn.execute(text("SELECT status FROM jobs_read.jobs WHERE job_id = :j"), {"j": job_id})).scalar()
    if r is None:
        raise ProblemError(404, "not_found")
    doc = r.request if isinstance(r.request, dict) else json.loads(r.request or "{}")
    return {"job_id": job_id, "tenant_id": r.tenant_id, "repo_ids": target_repo_ids(doc), "acting_for": acting_for(doc),
            "status": status, "revoked": status in REVOKED_STATUSES}


@router.get("/internal/v1/revocations")
async def revocations(request: Request, principal: Principal = require(RUNNER)) -> Response:
    body = await revocation_list(request.app.state.db)
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": "max-age=30"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


async def revocation_list(db: Any) -> bytes:
    cutoff = dt.datetime.now(dt.UTC) - RETENTION
    jobs: list[dict[str, str]] = []
    runs: list[dict[str, str]] = []
    iso = lambda t: t.isoformat().replace("+00:00", "Z")  # noqa: E731
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        if await db.has_relation("jobs_read.jobs"):
            for r in (await conn.execute(text(
                    "SELECT job_id, coalesce(terminal_at, updated_at) AS at FROM jobs_read.jobs WHERE status IN "
                    "('cancelled', 'failed', 'taken_over') AND coalesce(terminal_at, updated_at) > :c ORDER BY job_id"),
                    {"c": cutoff})).all():
                jobs.append({"id": r.job_id, "revoked_at": iso(r.at)})
        if await db.has_relation("jobs_read.job_runs"):
            for r in (await conn.execute(text(
                    "SELECT run_id, coalesce(finished_at, started_at, now()) AS at FROM jobs_read.job_runs WHERE "
                    "(phase = 'Cancelled' OR (phase = 'Failed' AND reason = 'SecurityViolation')) AND "
                    "coalesce(finished_at, started_at, now()) > :c ORDER BY run_id"), {"c": cutoff})).all():
                runs.append({"id": r.run_id, "revoked_at": iso(r.at)})
    return json.dumps({"job_ids": jobs, "run_ids": runs}, sort_keys=True).encode()
