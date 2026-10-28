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
        raise ProblemError(422, "invalid_bundle", errors=e.errors)
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
            raise ProblemError(502, "registry_blob_write_failed", detail=f"{type(e).__name__}: {str(e)[:200]}")
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
                raise ProblemError(409, str(e).split(":")[0], detail=str(e))
    elif body.get("bootstrap"):
        async with db.tx(role="svc_cpa_registry") as conn:
            a = await registry.get_assignment(conn, settings.replicant_env)
            if a is None:
                try:
                    a = await registry.bootstrap(conn, db, settings.replicant_env, rb, principal.principal_id, reason)
                except registry.BootstrapConflict as e:
                    raise ProblemError(409, str(e).split(":")[0], detail=str(e))
            out["assignment"] = a
    async with db.tx(role="svc_cpa_registry") as conn:
        out["promotion_state"] = (await conn.execute(text("SELECT promotion_state FROM registry.release_bundles WHERE "
                                                          "release_bundle_id = :rb"), {"rb": rb})).scalar_one()
    return out


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
        raise ProblemError(422, "invalid_override")
    base = NotifySettings.from_env(request.app.state.settings.web_origin)
    request.app.state.notify.ns = dataclasses.replace(base, **vals)
    request.app.state.notify_overrides = vals
    return await get_notify_overrides(request, principal)
