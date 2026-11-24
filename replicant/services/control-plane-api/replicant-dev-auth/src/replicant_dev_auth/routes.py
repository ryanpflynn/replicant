"""`/v1/admin/dev/**` (LOCAL ONLY; dev image target only)."""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Body, Request
from sqlalchemy import text

from replicant_telemetry.common import iso

router = APIRouter()
STEP_RE = re.compile(r"^[0-9]{2}-[a-z][a-z0-9-]{1,40}$")
BUILD_RE = re.compile(r"^b[0-9]{8}-[0-9a-f]{12}$")


def _require_seed():  # noqa: ANN202
    from control_plane_api.auth import require
    return require("dev:seed")


@router.get("/v1/admin/dev/seed-status")
async def get_seed_status(request: Request, principal: Any = _require_seed()) -> dict[str, Any]:
    async with request.app.state.db.tx(role="svc_cpa_intake") as conn:
        rows = (await conn.execute(text("SELECT step, state, detail, updated_at FROM intake.dev_seed_status "
                                        "ORDER BY step"))).all()
    steps = [{"step": r.step, "state": r.state, "updated_at": iso(r.updated_at),
              **({"detail": r.detail} if r.detail else {})} for r in rows]
    return {"seeded": bool(steps) and all(s["state"] == "done" for s in steps), "steps": steps}


@router.put("/v1/admin/dev/seed-status/{step}")
async def put_seed_status(step: str, request: Request, body: dict[str, Any] = Body(...),
                          principal: Any = _require_seed()) -> dict[str, Any]:
    from control_plane_api.problems import ProblemError
    if not STEP_RE.match(step) or body.get("state") not in ("pending", "done", "failed") or \
            len(str(body.get("detail") or "")) > 2000 or set(body) - {"state", "detail"}:
        raise ProblemError(422, "invalid_seed_status")
    async with request.app.state.db.tx(role="svc_cpa_intake") as conn:
        await conn.execute(text("INSERT INTO intake.dev_seed_status (step, state, detail) VALUES (:s, :st, :d) "
                                "ON CONFLICT (step) DO UPDATE SET state = EXCLUDED.state, detail = EXCLUDED.detail, "
                                "updated_at = now()"), {"s": step, "st": body["state"], "d": body.get("detail")})
    return {"step": step, "state": body["state"]}


@router.post("/v1/admin/dev/release-bundles")
async def register_dev_bundle(request: Request, body: dict[str, Any] = Body(...),
                              principal: Any = _require_seed()) -> dict[str, Any]:
    """M1 shortcut for seed steps 8-12 (stack.md §5): register a schema-valid ReleaseBundle for the local worker build,
    mark that worker version ready, take it to `eval_passed` without an eval run (recorded in DEVIATIONS), and with
    `bootstrap: true` bootstrap the local assignment with it. Body:
    `{worker_build_id?, image_digest?, node_catalog_digest?, spec?, metadata?, bootstrap?: bool, reason?}`."""
    from control_plane_api import registry
    from control_plane_api.problems import ProblemError

    settings = request.app.state.settings
    allowed = {"worker_build_id", "image_digest", "node_catalog_digest", "spec", "metadata", "bootstrap", "reason",
               "make_default"}
    if set(body) - allowed:
        raise ProblemError(422, "invalid_request", detail=f"unknown fields {sorted(set(body) - allowed)}")
    zero = "sha256:" + "0" * 64
    spec = body.get("spec")
    build = body.get("worker_build_id") or (spec or {}).get("worker_deployment", {}).get("build_id") or \
        settings.worker_build_id
    if not BUILD_RE.match(build or ""):
        raise ProblemError(422, "invalid_worker_build_id")
    image = body.get("image_digest") or (spec or {}).get("worker_deployment", {}).get("image_digest") or zero
    if spec is None:
        spec = registry.default_dev_spec(build, image)
    md = body.get("metadata") or {}
    try:
        doc = registry.bundle_document(spec, principal.principal_id, md.get("display_version"),
                                       md.get("change_summary") or "local dev bundle (replicant dev seed)")
    except registry.BundleInvalid as e:
        raise ProblemError(422, "invalid_bundle", errors=e.errors) from None
    db = request.app.state.db
    blob_writer = pointer_writer = None
    if settings.s3_endpoint:
        blob_writer = request.app.state.s3.writer("registry")
        pointer_writer = request.app.state.s3.overwriter("registry")   # dev path repairs pre-fix blobs
    reason = body.get("reason") or "local dev seed: eval skipped in M1"
    async with db.tx(role="svc_cpa_registry") as conn:
        try:
            rb, digest, created = await registry.register_bundle(conn, db, doc, principal.principal_id, blob_writer,
                                                                 pointer_writer)
        except Exception as e:  # noqa: BLE001
            raise ProblemError(502, "registry_blob_write_failed", detail=f"{type(e).__name__}: {str(e)[:200]}") from None
        await registry.mark_worker_ready(conn, build, image, body.get("node_catalog_digest") or zero)
        state = (await conn.execute(text("SELECT promotion_state FROM registry.release_bundles WHERE "
                                         "release_bundle_id = :rb"), {"rb": rb})).scalar_one()
        if state == "built":
            await registry.transition(conn, db, rb, "evaluating", principal.principal_id, reason, tier="T1")
            state = await registry.transition(conn, db, rb, "eval_passed", principal.principal_id, reason, tier="T1",
                                              evidence={"m1_dev_seed": True})
    out: dict[str, Any] = {"release_bundle_id": rb, "digest": digest, "created": created, "worker_build_id": build}
    if body.get("make_default"):     # re-seed with changed local definitions: the new bundle becomes the default
        async with db.tx(role="svc_cpa_registry") as conn:
            try:
                out["assignment"] = await registry.dev_set_default(conn, db, settings.replicant_env, rb,
                                                                    principal.principal_id, reason)
            except registry.BootstrapConflict as e:
                raise ProblemError(409, str(e).split(":")[0], detail=str(e)) from None
    elif body.get("bootstrap"):
        async with db.tx(role="svc_cpa_registry") as conn:
            a = await registry.get_assignment(conn, settings.replicant_env)
            if a is None:
                try:
                    a = await registry.bootstrap(conn, db, settings.replicant_env, rb, principal.principal_id, reason)
                except registry.BootstrapConflict as e:
                    raise ProblemError(409, str(e).split(":")[0], detail=str(e)) from None
            out["assignment"] = a
    async with db.tx(role="svc_cpa_registry") as conn:
        out["promotion_state"] = (await conn.execute(text("SELECT promotion_state FROM registry.release_bundles WHERE "
                                                          "release_bundle_id = :rb"), {"rb": rb})).scalar_one()
    return out



# ------------------------------------------------------------------------------------------------ test job intake
JOB_RE = re.compile(r"^job_[0-9a-hjkmnp-tv-z]{26}$")
TENANT_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
PRINCIPAL_RE = re.compile(r"^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$")
RB_RE = re.compile(r"^rb_[0-9a-hjkmnp-tv-z]{26}$")
REPO_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@router.post("/v1/admin/dev/job-requests")
async def register_test_job(request: Request, body: dict[str, Any] = Body(...),
                            principal: Any = _require_seed()) -> Any:
    """LOCAL ONLY (SIMPLIFY-D54): record an intake row for a job that no channel submitted, so a test driver that
    plays the worker (the Runner contract driver, go/cmd/runner-local/smoke in v0 mode) can create runs the live
    runner-local admits: run-scope (routes_runner.py) reads targets and acting_for from this row. No workflow is
    started and the row is born `started`, so the intake retrier never starts one either. Body:
    `{job_id, tenant_id, repo_ids: [..1-5], requester_principal_id, release_bundle_id?}`. Idempotent for the same
    tenant (the targets and requester are replaced); 409 when the job_id belongs to another tenant or to a job a
    channel submitted."""
    import datetime as dt
    import json

    from fastapi.responses import JSONResponse

    from control_plane_api.db import OPERATOR_SCOPE
    from control_plane_api.ids import ulid
    from control_plane_api.problems import ProblemError

    if not request.app.state.settings.is_local:     # install() refuses non-local too; defence in depth
        raise ProblemError(404, "not_found")
    allowed = {"job_id", "tenant_id", "repo_ids", "requester_principal_id", "release_bundle_id"}
    if set(body) - allowed:
        raise ProblemError(422, "invalid_request", detail=f"unknown fields {sorted(set(body) - allowed)}")
    job, tenant, who = body.get("job_id"), body.get("tenant_id"), body.get("requester_principal_id")
    repos = body.get("repo_ids")
    rb = body.get("release_bundle_id") or "rb_" + "0" * 26
    if not (isinstance(job, str) and JOB_RE.match(job)) or not (isinstance(tenant, str) and TENANT_RE.match(tenant)) \
            or len(tenant) > 63 or not (isinstance(who, str) and PRINCIPAL_RE.match(who)) \
            or not (isinstance(rb, str) and RB_RE.match(rb)) \
            or not (isinstance(repos, list) and 1 <= len(repos) <= 5
                    and all(isinstance(r, str) and REPO_ID_RE.match(r) for r in repos)):
        raise ProblemError(422, "invalid_request")
    now = dt.datetime.now(dt.UTC)
    doc = {"schema_version": "replicant.job-request/v1", "tenant_id": tenant,
           "source": {"channel": "api", "event_type": "dev.test_job", "event_id": job},
           "requester": {"kind": "user" if who.startswith("usr_") else "service", "principal_id": who},
           "targets": [{"repo": {"repo_id": r}, "base_ref": "main"} for r in repos],
           "intent": {"title": "dev test job (SIMPLIFY-D54)", "text": "", "format": "markdown", "untrusted": True},
           "workflow_type": "bugfix", "priority": "normal", "received_at": now.isoformat().replace("+00:00", "Z")}
    db = request.app.state.db
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        row = (await conn.execute(text("SELECT tenant_id, dedupe_key FROM intake.job_requests WHERE job_id = :j"),
                                  {"j": job})).first()
    owner = row.tenant_id if row is not None else None
    if row is not None and (owner != tenant or not str(row.dedupe_key).startswith("dev:test-job:")):
        raise ProblemError(409, "job_exists", detail="the job_id belongs to another tenant or to a submitted job")
    async with db.tx(role="svc_cpa_intake", tenant=tenant) as conn:
        if owner is None:
            await conn.execute(text(
                "INSERT INTO intake.job_requests (job_id, tenant_id, request_id, dedupe_key, channel, requester, "
                "submitted_by, workflow_type, request, release_bundle_id, assignment_reason, release_cohort, "
                "assignment_version, start_status, started_at, received_at) VALUES (:j, :t, :rq, :k, 'api', :who, :by, "
                "'bugfix', CAST(:doc AS jsonb), :rb, 'default', 'default', 0, 'started', :now, :now)"),
                {"j": job, "t": tenant, "rq": ulid("jrq"), "k": f"dev:test-job:{job}", "who": who,
                 "by": principal.principal_id, "doc": json.dumps(doc), "rb": rb, "now": now})
        else:
            await conn.execute(text("UPDATE intake.job_requests SET requester = :who, request = CAST(:doc AS jsonb) "
                                    "WHERE job_id = :j AND tenant_id = :t"),
                               {"j": job, "t": tenant, "who": who, "doc": json.dumps(doc)})
    return JSONResponse({"job_id": job, "tenant_id": tenant, "repo_ids": repos, "acting_for": who,
                         "created": owner is None}, status_code=201 if owner is None else 200)

# ------------------------------------------------------------------------------------------------ notify overrides
NOTIFY_OVERRIDABLE = {"pr_idle_seconds": int, "pr_remind_every_seconds": int, "job_max_age_days": int,
                      "lifetime_lead_seconds": int, "digest_threshold": int, "quiet_default": str,
                      "sweep_seconds": float}


@router.get("/v1/admin/dev/notify-overrides")
async def get_notify_overrides(request: Request, principal: Any = _require_seed()) -> dict[str, Any]:
    ns = request.app.state.notify.ns
    return {"overrides": dict(getattr(request.app.state, "notify_overrides", {})),
            "effective": {k: getattr(ns, k) for k in NOTIFY_OVERRIDABLE if hasattr(ns, k)}}


@router.put("/v1/admin/dev/notify-overrides")
async def put_notify_overrides(request: Request, body: dict[str, Any] = Body(...),
                               principal: Any = _require_seed()) -> dict[str, Any]:
    """LOCAL ONLY, in memory (test hook): override notification thresholds of this control-plane-api process without
    touching dev/compose/.env, e.g. `{"pr_idle_seconds": 120}` for the E2E. `{}` restores the environment values."""
    import dataclasses

    from control_plane_api.notify_policy import NotifySettings
    from control_plane_api.problems import ProblemError
    bad = set(body) - set(NOTIFY_OVERRIDABLE)
    if bad:
        raise ProblemError(422, "invalid_override", detail=f"unknown keys {sorted(bad)}")
    try:
        vals = {k: NOTIFY_OVERRIDABLE[k](v) for k, v in body.items()}
    except (TypeError, ValueError):
        raise ProblemError(422, "invalid_override") from None
    base = NotifySettings.from_env(request.app.state.settings.web_origin)
    request.app.state.notify.ns = dataclasses.replace(base, **vals)
    request.app.state.notify_overrides = vals
    return await get_notify_overrides(request, principal)
