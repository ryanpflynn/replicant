"""Release bundles and assignment (WS-06 release-bundle.md §2, §8; C-DEF 3.0.0 §4.3-4.5).

`resolve()` is the WS-06 reference algorithm (release-bundle.md §8.3) on an async connection, called inside the intake
transaction. Bundle registration and bootstrap implement the parts `replicant dev seed` needs in M1.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from control_plane_api import audit, contracts

log = logging.getLogger(__name__)

EVAL_TENANT = "replicant-eval"
COHORT = {"canary": "candidate", "holdback": "holdback", "experiment": "experiment"}
REASON_RE = (r"^(default|eval_pin|shadow_pin|explicit_pin|ramp:(canary|holdback|experiment):[0-2]|"
             r"tenant_override:(holdback_tenant|pinned_by_operator|layer_pending))(\+layer)?$")


def bucket(job_id: str) -> int:
    """C-DEF §4.5: int(sha256('ramp:' + job_id)[0:8], 16) mod 10000."""
    return int(hashlib.sha256(f"ramp:{job_id}".encode()).hexdigest()[:8], 16) % 10000


@dataclass(frozen=True)
class Resolution:
    release_bundle_id: str
    base_release_bundle_id: str
    assignment_reason: str
    release_cohort: str
    assignment_version: int
    worker_build_id: str = ""


class AssignmentError(Exception):
    """503 bundle_unresolvable; no intake row is written (release-bundle.md §8.5)."""


class PinNotAllowed(Exception):
    """403: requested_release_bundle_id without release:pin."""


async def _one(conn: AsyncConnection, sql: str, **p: Any) -> dict[str, Any] | None:
    r = (await conn.execute(text(sql), p)).first()
    return dict(r._mapping) if r is not None else None


async def _bundle(conn: AsyncConnection, rb: str) -> dict[str, Any] | None:
    return await _one(conn, "SELECT release_bundle_id, base_release_bundle_id, promotion_state, eval_only, tenant_id, "
                            "worker_build_id FROM registry.release_bundles WHERE release_bundle_id = :rb", rb=rb)


async def resolve(conn: AsyncConnection, env: str, tenant_id: str, job_id: str, requested: str | None,
                  may_pin: bool, evaluation_mode: str | None, requester: str) -> Resolution:
    a = await _one(conn, "SELECT env, default_release_bundle_id, version FROM registry.bundle_assignments "
                         "WHERE env = :env", env=env)
    if a is None:
        raise AssignmentError(f"environment {env} has no assignment (bootstrap it, release-bundle.md §8.6)")
    if requested is not None:
        res = await _resolve_pin(conn, a, tenant_id, requested, may_pin, evaluation_mode, requester)
    else:
        override = await _one(conn, "SELECT release_bundle_id, kind FROM registry.bundle_assignment_tenant_overrides "
                                    "WHERE env = :env AND tenant_id = :t", env=env, t=tenant_id)
        ramps = (await conn.execute(text(
            "SELECT slot, candidate_bundle_id, purpose, percent, bucket_lo, tenants_included, tenants_excluded "
            "FROM registry.bundle_assignment_ramps WHERE env = :env ORDER BY slot"), {"env": env})).all()
        if override is not None:
            base, reason, cohort = override["release_bundle_id"], f"tenant_override:{override['kind']}", "override"
        else:
            hit = next((dict(r._mapping) for r in ramps if _in_ramp(dict(r._mapping), tenant_id, job_id)), None)
            if hit is not None:
                base, reason, cohort = hit["candidate_bundle_id"], f"ramp:{hit['purpose']}:{hit['slot']}", \
                    COHORT[hit["purpose"]]
            else:
                base, reason, cohort = a["default_release_bundle_id"], "default", "default"
        res = await _apply_layer(conn, a, tenant_id, base, reason, cohort, allowed=("promoted", "canary", "deprecated"))
    rb = await _bundle(conn, res.release_bundle_id)
    if rb is None:
        raise AssignmentError(f"bundle {res.release_bundle_id} not in registry")
    build = rb["worker_build_id"]
    ready = await _one(conn, "SELECT 1 AS ok FROM registry.worker_deployment_versions WHERE deployment_name = "
                             "'replicant-worker' AND build_id = :b AND ready_at IS NOT NULL AND retired_at IS NULL", b=build)
    if ready is None:
        # versioning.md §3.1 precondition; never a fallback (XD-FIX-R1-137a)
        raise AssignmentError(f"worker build {build} is not ready")
    return Resolution(res.release_bundle_id, res.base_release_bundle_id, res.assignment_reason, res.release_cohort,
                      res.assignment_version, build)


async def _resolve_pin(conn, a, tenant_id, requested, may_pin, evaluation_mode, requester) -> Resolution:
    if not may_pin:
        raise PinNotAllowed("release:pin required")
    rb = await _bundle(conn, requested)
    if rb is None or rb["promotion_state"] in ("revoked", "withdrawn"):
        raise AssignmentError("requested bundle not assignable")
    base = rb["base_release_bundle_id"] or requested
    if tenant_id == EVAL_TENANT:
        return Resolution(requested, base, "eval_pin", "eval", a["version"])
    if rb["eval_only"]:
        raise AssignmentError("eval-only bundle outside replicant-eval")
    if evaluation_mode == "shadow":
        if requester != "svc_release-controller" or rb["promotion_state"] != "shadowing":
            raise AssignmentError("shadow pins need svc_release-controller and a shadowing bundle")
        if rb["tenant_id"] not in ("replicant-system", tenant_id):
            raise AssignmentError("derived bundle of another tenant")
        return Resolution(requested, base, "shadow_pin", "shadow", a["version"])
    if rb["promotion_state"] not in ("promoted", "deprecated"):
        raise AssignmentError("live-tenant pins accept only promoted or deprecated bundles")
    return Resolution(requested, base, "explicit_pin", "override", a["version"])


async def _apply_layer(conn, a, tenant_id, base, reason, cohort, allowed) -> Resolution:
    layer = await _one(conn, "SELECT max(layer_version) AS v FROM registry.tenant_layers WHERE tenant_id = :t",
                       t=tenant_id)
    if layer is None or layer["v"] is None:
        rb = await _bundle(conn, base)
        if rb is None or rb["promotion_state"] not in allowed:
            raise AssignmentError(f"bundle {base} is not assignable ({rb and rb['promotion_state']})")
        return Resolution(base, base, reason, cohort, a["version"])
    d = await _one(conn, "SELECT release_bundle_id FROM registry.derived_bundles WHERE base_release_bundle_id = :b "
                         "AND tenant_id = :t AND layer_version = :v", b=base, t=tenant_id, v=layer["v"])
    if d is None:
        raise AssignmentError(f"derived bundle missing for {base}/{tenant_id}@{layer['v']}")
    rb = await _bundle(conn, d["release_bundle_id"])
    if rb is None or rb["promotion_state"] not in allowed:
        raise AssignmentError(f"derived bundle {d['release_bundle_id']} is {rb and rb['promotion_state']}")
    return Resolution(rb["release_bundle_id"], base, reason + "+layer", cohort, a["version"])


def _in_ramp(r: dict[str, Any], tenant_id: str, job_id: str) -> bool:
    if tenant_id in (r["tenants_excluded"] or []):
        return False
    if tenant_id in (r["tenants_included"] or []):
        return True
    width = int(Decimal(r["percent"]) * 100)
    return r["bucket_lo"] <= bucket(job_id) < r["bucket_lo"] + width


# ------------------------------------------------------------------------------------------------------------------
# Bundle registration (M1 dev path) and bootstrap
# ------------------------------------------------------------------------------------------------------------------

def _placeholder_ref(ref: str) -> dict[str, str]:
    return {"ref": ref, "digest": "sha256:" + hashlib.sha256(f"replicant-m1-placeholder:{ref}".encode()).hexdigest()}


def default_dev_spec(build_id: str, image_digest: str) -> dict[str, Any]:
    """A schema-valid bundle spec for M1 when no registry publish/build has run. Refs are placeholders
    (digest = sha256 of the ref string); consumers that need definition bodies must publish real ones."""
    graphs = {t: _placeholder_ref(f"graph/{t.replace('_', '-')}@1") for t in
              ("bugfix", "feature", "api_change", "docs", "hotfix", "maintenance", "pr_repair", "qa_only")}
    return {
        "worker_deployment": {"deployment_name": "replicant-worker", "build_id": build_id, "image_digest": image_digest},
        "node_catalog": _placeholder_ref("nodecatalog/replicant-worker@1"),
        "router": _placeholder_ref("policy/router-rules@1"),
        "graphs": graphs,
        "agents": sorted([_placeholder_ref("agent/implementer-claude@1"), _placeholder_ref("agent/qa-runner@1"),
                          _placeholder_ref("agent/reviewer-claude@1")], key=lambda r: r["ref"]),
        "environments": [_placeholder_ref("environment/go-service@1")],
        "policies": sorted([_placeholder_ref(f"policy/{p}@1") for p in
                            ("approval-channels", "budgets", "codebase-bindings", "gate-rules", "network-profiles",
                             "repair-limits", "risk-rules", "runtime-selection")], key=lambda r: r["ref"]),
        "prompts": [],
        "cli_images": [],
        "models": [],
        "memory_snapshot_policy": _placeholder_ref("policy/memory-snapshot-default@1"),
        "lineage": {},
    }


class BundleInvalid(Exception):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("bundle document invalid")
        self.errors = errors


def bundle_document(spec: dict[str, Any], created_by: str, display_version: str | None = None,
                    change_summary: str | None = None) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    md = {"display_version": display_version or now.strftime("%Y.%m.%d-1"),
          "created_at": now.isoformat().replace("+00:00", "Z"), "created_by": created_by}
    if change_summary:
        md["change_summary"] = change_summary
    doc = {"apiVersion": "replicant.example.com/v1", "kind": "ReleaseBundle", "metadata": md, "spec": spec}
    errs = contracts.errors("release-bundle", doc)
    if errs:
        raise BundleInvalid(errs)
    return doc


async def register_bundle(conn: AsyncConnection, db: Any, doc: dict[str, Any], actor: str,
                          blob_writer: Any = None, repair_writer: Any = None) -> tuple[str, str, bool]:
    """Content-addressed insert-or-get (C-DEF §4.3 step 5): blob, then pointer, then row. Returns
    (release_bundle_id, digest, created). Layout and digests come from `replicant_cdef` (shared with replicant-worker):
    blob `blobs/sha256/<bundle digest>.json` = JCS(spec), pointer `bundles/<rb>.json` = {release_bundle_id, digest}.
    `repair_writer` (local dev path only) overwrites instead of write-once, to replace blobs written by the pre-fix
    layout (whole document under the spec digest; INTERFACES-LOG CONTROL-06)."""
    from replicant_cdef import blob_key, bundle_blob, bundle_pointer
    spec = doc["spec"]
    rb, digest, body = bundle_blob(spec)
    key = blob_key(digest)
    write = repair_writer or blob_writer
    if write is not None:
        await write(key, body)
        pk, pbody = bundle_pointer(rb, digest)
        await write(pk, pbody)
    existing = await _one(conn, "SELECT blob_key FROM registry.release_bundles WHERE release_bundle_id = :rb", rb=rb)
    if existing:
        return rb, digest, False
    await conn.execute(text(
        "INSERT INTO registry.release_bundles (release_bundle_id, digest, display_version, spec, metadata, blob_key, "
        "worker_build_id, parent_release_bundle_id, change_classes, required_tier, created_by) VALUES (:rb, :d, :dv, "
        "CAST(:spec AS jsonb), CAST(:md AS jsonb), :bk, :b, :parent, '{}', 'T1', :by) ON CONFLICT DO NOTHING"),
        {"rb": rb, "d": digest, "dv": doc["metadata"]["display_version"], "spec": json.dumps(spec),
         "md": json.dumps(doc["metadata"]), "bk": key,
         "b": spec["worker_deployment"]["build_id"], "parent": (spec.get("lineage") or {}).get("parent_release_bundle_id"),
         "by": actor})
    await audit.emit(conn, db, {"event_type": "registry.published", "outcome": "success", "actor_principal_id": actor,
                                "release_bundle_id": rb, "digest": digest, "target_kind": "release_bundle",
                                "target_id": rb})
    return rb, digest, True


async def transition(conn: AsyncConnection, db: Any, rb: str, to_state: str, actor: str, reason: str,
                     tier: str | None = None, evidence: dict[str, Any] | None = None) -> str:
    cur = await _one(conn, "SELECT promotion_state, state_version FROM registry.release_bundles "
                           "WHERE release_bundle_id = :rb FOR UPDATE", rb=rb)
    if cur is None:
        raise KeyError(rb)
    if cur["promotion_state"] == to_state:
        return to_state
    aid = await audit.emit(conn, db, {"event_type": "release.bundle_promoted" if to_state == "promoted" else
                                      "registry.published", "outcome": "success", "target_kind": "release_bundle",
                                      "actor_principal_id": actor, "release_bundle_id": rb,
                                      "details": {"from_state": cur["promotion_state"], "to_state": to_state}})
    await conn.execute(text("UPDATE registry.release_bundles SET promotion_state = :s, state_version = state_version + 1 "
                            "WHERE release_bundle_id = :rb"), {"s": to_state, "rb": rb})
    seq = (await conn.execute(text("SELECT coalesce(max(seq), 0) + 1 FROM registry.bundle_promotion_events "
                                   "WHERE release_bundle_id = :rb"), {"rb": rb})).scalar_one()
    await conn.execute(text(
        "INSERT INTO registry.bundle_promotion_events (release_bundle_id, seq, from_state, to_state, tier, actor, reason, "
        "evidence, audit_event_id) VALUES (:rb, :seq, :f, :t, :tier, :a, :r, CAST(:ev AS jsonb), :aid)"),
        {"rb": rb, "seq": seq, "f": cur["promotion_state"], "t": to_state, "tier": tier, "a": actor, "r": reason,
         "ev": json.dumps(evidence or {}), "aid": aid})
    return to_state


async def mark_worker_ready(conn: AsyncConnection, build_id: str, image_digest: str, node_catalog_digest: str) -> None:
    await conn.execute(text(
        "INSERT INTO registry.worker_deployment_versions (deployment_name, build_id, image_digest, node_catalog_digest, "
        "ready_at) VALUES ('replicant-worker', :b, :i, :n, now()) ON CONFLICT (deployment_name, build_id) DO UPDATE "
        "SET ready_at = coalesce(registry.worker_deployment_versions.ready_at, now())"),
        {"b": build_id, "i": image_digest, "n": node_catalog_digest})


async def get_assignment(conn: AsyncConnection, env: str) -> dict[str, Any] | None:
    a = await _one(conn, "SELECT env, default_release_bundle_id, rollback_release_bundle_id, version, updated_at, "
                         "updated_by FROM registry.bundle_assignments WHERE env = :env", env=env)
    if a is None:
        return None
    ramps = (await conn.execute(text("SELECT slot, candidate_bundle_id, purpose, percent, bucket_lo, tenants_included, "
                                     "tenants_excluded, pipeline_id FROM registry.bundle_assignment_ramps WHERE env = :e "
                                     "ORDER BY slot"), {"e": env})).all()
    ovr = (await conn.execute(text("SELECT tenant_id, release_bundle_id, kind, reason, expires_at FROM "
                                   "registry.bundle_assignment_tenant_overrides WHERE env = :e"), {"e": env})).all()
    out: dict[str, Any] = {  # WS-06 releases.openapi.yaml `Assignment`
        "env": a["env"], "version": a["version"], "default": a["default_release_bundle_id"],
        "tenant_overrides": {o.tenant_id: o.release_bundle_id for o in ovr},
        "ramps": [{"slot": r.slot, "candidate": r.candidate_bundle_id, "purpose": r.purpose,
                   "percent": float(r.percent), "bucket_lo": r.bucket_lo} for r in ramps],
    }
    if a["rollback_release_bundle_id"]:
        out["rollback_target"] = a["rollback_release_bundle_id"]
    return out


class BootstrapConflict(Exception):
    pass


async def bootstrap(conn: AsyncConnection, db: Any, env: str, rb: str, actor: str, reason: str) -> dict[str, Any]:
    """POST /v1/releases/{env}/assignment:bootstrap (release-bundle.md §8.6): only while env has no assignment; the
    bundle goes eval_passed -> promoted and becomes default and rollback target in one transaction."""
    if await _one(conn, "SELECT env FROM registry.bundle_assignments WHERE env = :e", e=env):
        raise BootstrapConflict("assignment_exists")
    b = await _bundle(conn, rb)
    if b is None:
        raise BootstrapConflict("bundle_not_found")
    if b["promotion_state"] != "eval_passed":
        raise BootstrapConflict(f"bundle_not_eval_passed:{b['promotion_state']}")
    await transition(conn, db, rb, "promoted", actor, reason, tier="T1")
    aid = await audit.emit(conn, db, {"event_type": "release.assignment_changed", "outcome": "success",
                                      "actor_principal_id": actor, "release_bundle_id": rb,
                                      "details": {"env": env, "change_kind": "bootstrap_default"}})
    await conn.execute(text("INSERT INTO registry.bundle_assignments (env, default_release_bundle_id, "
                            "rollback_release_bundle_id, version, updated_by) VALUES (:e, :rb, :rb, 1, :a)"),
                       {"e": env, "rb": rb, "a": actor})
    snap = {"default": rb, "tenant_overrides": {}, "ramps": []}
    await conn.execute(text("INSERT INTO registry.bundle_assignment_history (env, version, change_kind, snapshot, reason, "
                            "changed_by, audit_event_id) VALUES (:e, 1, 'bootstrap_default', CAST(:s AS jsonb), :r, :a, "
                            ":aid)"), {"e": env, "s": json.dumps(snap), "r": reason, "a": actor, "aid": aid})
    return await get_assignment(conn, env) or {}


async def dev_set_default(conn: AsyncConnection, db: Any, env: str, rb: str, actor: str, reason: str) -> dict[str, Any]:
    """LOCAL ONLY (replicant_dev_auth `make_default`): a re-seeded dev bundle (new local definitions) becomes the
    default; the previous default becomes the rollback target. Stands in for the release pipeline's promote +
    set_default (release-bundle.md §8) that v0 does not have (DEVIATIONS NOTIFY-D16)."""
    cur = await _one(conn, "SELECT default_release_bundle_id, version FROM registry.bundle_assignments WHERE env = :e",
                     e=env)
    if cur is None:
        return await bootstrap(conn, db, env, rb, actor, reason)
    if cur["default_release_bundle_id"] == rb:
        return await get_assignment(conn, env) or {}
    b = await _bundle(conn, rb)
    if b is None:
        raise BootstrapConflict("bundle_not_found")
    if b["promotion_state"] == "eval_passed":
        await transition(conn, db, rb, "promoted", actor, reason, tier="T1")
    v = int(cur["version"]) + 1
    aid = await audit.emit(conn, db, {"event_type": "release.assignment_changed", "outcome": "success",
                                      "actor_principal_id": actor, "release_bundle_id": rb,
                                      "details": {"env": env, "change_kind": "set_default", "dev_reseed": True}})
    await conn.execute(text("UPDATE registry.bundle_assignments SET default_release_bundle_id = :rb, "
                            "rollback_release_bundle_id = :old, version = :v, updated_by = :a, updated_at = now() "
                            "WHERE env = :e"), {"rb": rb, "old": cur["default_release_bundle_id"], "v": v, "a": actor,
                                                "e": env})
    snap = {"default": rb, "tenant_overrides": {}, "ramps": []}
    await conn.execute(text("INSERT INTO registry.bundle_assignment_history (env, version, change_kind, snapshot, reason, "
                            "changed_by, audit_event_id) VALUES (:e, :v, 'set_default', CAST(:s AS jsonb), :r, :a, :aid)"),
                       {"e": env, "v": v, "s": json.dumps(snap), "r": reason, "a": actor, "aid": aid})
    return await get_assignment(conn, env) or {}


WORKER_QUEUES_DEFAULT = ("replicant-jobs", "replicant-runner-calls", "replicant-llm-calls")


async def register_ready_worker_versions(db: Any, temporal: Any, queues: tuple[str, ...] = WORKER_QUEUES_DEFAULT) -> int:
    """versioning.md §2 step 4: record `registry.worker_deployment_versions.ready_at` once Temporal's
    DescribeWorkerDeploymentVersion lists every task queue of the build (pollers registered). Builds come from
    registered bundles. Returns the number of builds newly marked ready."""
    from temporalio.api.deployment.v1 import WorkerDeploymentVersion as PVer
    from temporalio.api.workflowservice.v1 import DescribeWorkerDeploymentVersionRequest
    async with db.tx(role="svc_cpa_registry") as conn:
        rows = (await conn.execute(text(
            "SELECT DISTINCT b.worker_build_id, b.spec -> 'worker_deployment' ->> 'image_digest' AS img, "
            "b.spec -> 'node_catalog' ->> 'digest' AS nc FROM registry.release_bundles b LEFT JOIN "
            "registry.worker_deployment_versions w ON w.deployment_name = 'replicant-worker' AND "
            "w.build_id = b.worker_build_id WHERE w.ready_at IS NULL AND b.promotion_state NOT IN "
            "('revoked', 'withdrawn')"))).all()
    n = 0
    if not rows:
        return 0
    client = await temporal.client()
    for r in rows:
        try:
            d = await client.workflow_service.describe_worker_deployment_version(DescribeWorkerDeploymentVersionRequest(
                namespace=client.namespace, deployment_version=PVer(deployment_name="replicant-worker",
                                                                    build_id=r.worker_build_id)))
        except Exception:  # noqa: BLE001 - not registered yet
            continue
        have = {t.name for t in d.version_task_queues}
        if set(queues) <= have:
            async with db.tx(role="svc_cpa_registry") as conn:
                await mark_worker_ready(conn, r.worker_build_id, r.img or "sha256:" + "0" * 64,
                                        r.nc or "sha256:" + "0" * 64)
            n += 1
            log.info("worker version replicant-worker:%s ready (task queues %s)", r.worker_build_id, sorted(have))
    return n


async def worker_registrar_loop(db: Any, temporal: Any, queues: tuple[str, ...], interval_s: float = 10.0) -> None:
    import asyncio
    while True:
        try:
            await register_ready_worker_versions(db, temporal, queues)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("worker version registrar: %s", type(e).__name__)
        await asyncio.sleep(interval_s)
