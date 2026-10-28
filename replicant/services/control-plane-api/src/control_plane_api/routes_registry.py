"""Registry publish and bundle build (C-DEF 3.0.0 §4.1-4.3; WS-03 config-publishing.md; WS-06 releases.openapi.yaml).

M1 scope (recorded in DEVIATIONS):
* `POST /v1/registry/publish`: envelope validation (definition-envelope.schema.json) only, no kind-schema validation and
  no GraphDefinition compile; `digest = sha256(JCS(document))`; blob (`blobs/sha256/<hex>.json` = the JCS bytes, so
  sha256(blob) == digest) then index (`index/<kind>/global/<name>/<version>.json`) then row, heads moved for
  `source: config_repo` (absent names lose their head). Idempotent on `publish_id`.
* `POST /v1/registry/bundles:build`: `mode: full` only. Selectors resolve to registry heads (or exact pins); the closure is
  the agents and environments referenced by the selected graphs and policies (graph node `config.agent` and any
  `agent/…`/`environment/…` reference string); prompts are not resolved (no Langfuse in M1, `prompts: []`); `cli_images`
  lists only digest-pinned images; `node_catalog` is the published `nodecatalog/replicant-worker` head or a placeholder.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import yaml
from fastapi import APIRouter, Body, Request, Response
from sqlalchemy import text

from control_plane_api import audit, contracts, registry
from control_plane_api.auth import require
from control_plane_api.authz import Principal
from control_plane_api.ids import jcs, sha256_digest
from control_plane_api.problems import ProblemError
from replicant_cdef import blob_key, definition_blob

log = logging.getLogger(__name__)
router = APIRouter()

KINDS = {"AgentDefinition": "agent", "EnvironmentDefinition": "environment", "GraphDefinition": "graph",
         "Policy": "policy", "NodeCatalog": "nodecatalog"}
REF_RE = re.compile(r"^(agent|environment|graph|policy|nodecatalog)/([a-z][a-z0-9]*(?:-[a-z0-9]+)*)"
                    r"(?:@([1-9][0-9]*))?(?:#(sha256:[0-9a-f]{64}))?$")
IMAGE_RE = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
WORKFLOW_TYPES = ("bugfix", "feature", "api_change", "docs", "hotfix", "maintenance", "pr_repair", "qa_only")


def _load_yaml(content: str) -> Any:
    """YAML 1.2-ish safe load; anchors/aliases are rejected (C-DEF §4.1 rule 3)."""
    for ev in yaml.parse(content, Loader=yaml.SafeLoader):
        if getattr(ev, "anchor", None) or isinstance(ev, yaml.AliasEvent):
            raise ValueError("YAML anchors and aliases are not allowed (C-DEF §4.1 rule 3)")
    return yaml.safe_load(content)


@router.post("/v1/registry/publish")
async def registry_publish(request: Request, body: dict[str, Any] = Body(...),
                           principal: Principal = require("registry:publish|dev:seed")) -> dict[str, Any]:
    for k in ("publish_id", "commit_sha", "source", "dry_run", "documents"):
        if k not in body:
            raise ProblemError(422, "invalid_publish_request", detail=f"missing {k}")
    if not re.match(r"^pub_[0-9a-hjkmnp-tv-z]{26}$", str(body["publish_id"])) or \
            not re.match(r"^[0-9a-f]{40}$", str(body["commit_sha"])) or body["source"] not in ("config_repo", "worker_ci"):
        raise ProblemError(422, "invalid_publish_request")
    docs = body["documents"]
    if not isinstance(docs, list) or not 1 <= len(docs) <= 2000:
        raise ProblemError(422, "invalid_publish_request", detail="documents: 1..2000")
    parsed: list[tuple[str, dict[str, Any], str, str, str | None, str]] = []
    violations = []
    for d in docs:
        path = str(d.get("path", ""))[:512]
        try:
            doc = _load_yaml(str(d.get("content_yaml", "")))
            errs = contracts.errors("definition-envelope", doc)
            if errs:
                raise ValueError(json.dumps(errs[:3]))
            kind = KINDS[doc["kind"]]
            if body["source"] == "worker_ci" and kind != "nodecatalog":
                raise ValueError("source worker_ci publishes NodeCatalog documents only")
            scope = doc["metadata"]["scope"]
            if scope["level"] != "global":
                raise ValueError("tenant-scoped definitions are not supported in M1")
            ptype = (doc.get("spec") or {}).get("policyType") if kind == "policy" else None
            if kind == "policy" and not ptype:
                raise ValueError("Policy without spec.policyType")
            digest = definition_blob(doc)[0]
            parsed.append((path, doc, kind, doc["metadata"]["name"], ptype, digest))
        except Exception as e:  # noqa: BLE001
            violations.append({"path": path, "message": str(e)[:300]})
    if violations:
        raise ProblemError(422, "publish_rejected", violations=violations)
    db = request.app.state.db
    s3 = request.app.state.s3 if request.app.state.settings.s3_endpoint else None
    async with db.tx(role="svc_cpa_registry") as conn:
        prior = (await conn.execute(text("SELECT results FROM registry.publish_requests WHERE publish_id = :p"),
                                    {"p": body["publish_id"]})).first()
        if prior is not None:
            return {"items": prior.results}
        items = []
        for path, doc, kind, name, ptype, digest in parsed:
            ex = (await conn.execute(text(
                "SELECT version FROM registry.definition_versions WHERE kind = :k AND scope_level = 'global' AND "
                "tenant_id = '' AND name = :n AND digest = :d"), {"k": kind, "n": name, "d": digest})).first()
            created = ex is None
            if created:
                version = (await conn.execute(text(
                    "SELECT coalesce(max(version), 0) + 1 FROM registry.definition_versions WHERE kind = :k AND "
                    "scope_level = 'global' AND tenant_id = '' AND name = :n"), {"k": kind, "n": name})).scalar_one()
            else:
                version = ex.version
            hexd = digest.removeprefix("sha256:")
            if not body["dry_run"]:
                if created:
                    if s3 is not None:
                        w = s3.writer("registry")
                        await w(blob_key(digest), definition_blob(doc)[1])
                        await w(f"index/{kind}/global/{name}/{version}.json", json.dumps(
                            {"digest": digest, "published_at": None, "source_commit": body["commit_sha"]}).encode())
                    await conn.execute(text(
                        "INSERT INTO registry.definition_versions (kind, scope_level, tenant_id, name, version, digest, "
                        "api_version, policy_type, kind_schema_id, document, blob_key, source_commit, source_path, "
                        "published_by) VALUES (:k, 'global', '', :n, :v, :d, :av, :pt, :ks, CAST(:doc AS jsonb), :bk, :sc, "
                        ":sp, :by)"),
                        {"k": kind, "n": name, "v": version, "d": digest, "av": doc["apiVersion"], "pt": ptype,
                         "ks": f"https://schemas.replicant.example.com/kinds/{doc['kind']}/v1.json", "doc": json.dumps(doc),
                         "bk": f"blobs/sha256/{hexd}.json", "sc": body["commit_sha"], "sp": path,
                         "by": principal.principal_id})
                if body["source"] == "config_repo" or kind == "nodecatalog":
                    await conn.execute(text(
                        "INSERT INTO registry.definition_heads (kind, scope_level, tenant_id, name, version, digest, "
                        "source_commit) VALUES (:k, 'global', '', :n, :v, :d, :sc) ON CONFLICT (kind, scope_level, "
                        "tenant_id, name) DO UPDATE SET version = EXCLUDED.version, digest = EXCLUDED.digest, "
                        "source_commit = EXCLUDED.source_commit, updated_at = now(), deleted_at = NULL"),
                        {"k": kind, "n": name, "v": version, "d": digest, "sc": body["commit_sha"]})
            items.append({"path": path, "ref": f"{kind}/{name}@{version}", "digest": digest, "created": created,
                          "head": body["source"] == "config_repo" or kind == "nodecatalog"})
        if body["dry_run"]:
            return {"items": items}
        if body["source"] == "config_repo":
            names = [(k, n) for _, _, k, n, _, _ in parsed]
            await conn.execute(text(
                "UPDATE registry.definition_heads SET deleted_at = now() WHERE scope_level = 'global' AND tenant_id = '' "
                "AND kind <> 'nodecatalog' AND deleted_at IS NULL AND NOT ((kind || '/' || name) = ANY(:names))"),
                {"names": [f"{k}/{n}" for k, n in names]})
        await conn.execute(text(
            "INSERT INTO registry.publish_requests (publish_id, commit_sha, request_digest, source, dry_run, caller, "
            "outcome, results) VALUES (:p, :c, :rd, :s, false, :by, :o, CAST(:r AS jsonb)) ON CONFLICT DO NOTHING"),
            {"p": body["publish_id"], "c": body["commit_sha"], "rd": sha256_digest(jcs(docs)), "s": body["source"],
             "by": principal.principal_id, "o": "published" if any(i["created"] for i in items) else "no_change",
             "r": json.dumps(items)})
        await audit.emit(conn, db, {"event_type": "registry.published", "outcome": "success",
                                    "actor_principal_id": principal.principal_id, "target_kind": "publish",
                                    "target_id": body["publish_id"],
                                    "details": {"documents": len(items), "created": sum(i["created"] for i in items)}})
    return {"items": items}


async def _resolve(conn: Any, selector: str) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    m = REF_RE.match(selector.strip())
    if not m:
        return None, None
    kind, name, ver, dig = m.group(1), m.group(2), m.group(3), m.group(4)
    if ver:
        r = (await conn.execute(text("SELECT version, digest, document, state FROM registry.definition_versions WHERE "
                                     "kind = :k AND scope_level = 'global' AND tenant_id = '' AND name = :n AND "
                                     "version = :v"), {"k": kind, "n": name, "v": int(ver)})).first()
    else:
        r = (await conn.execute(text(
            "SELECT v.version, v.digest, v.document, v.state FROM registry.definition_heads h JOIN "
            "registry.definition_versions v USING (kind, scope_level, tenant_id, name, version) WHERE h.kind = :k AND "
            "h.scope_level = 'global' AND h.tenant_id = '' AND h.name = :n AND h.deleted_at IS NULL"),
            {"k": kind, "n": name})).first()
    if r is None or r.state == "revoked" or (dig and dig != r.digest):
        return None, None
    return {"ref": f"{kind}/{name}@{r.version}", "digest": r.digest}, r.document


def _refs_in(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "agent" and isinstance(v, str) and re.match(r"^[a-z][a-z0-9-]*$", v):
                out.add(f"agent/{v}")
            _refs_in(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _refs_in(v, out)
    elif isinstance(obj, str):
        m = REF_RE.match(obj)
        if m and m.group(1) in ("agent", "environment"):
            out.add(obj.split("#", 1)[0])


@router.post("/v1/registry/bundles:build")
async def build_bundle(request: Request, response: Response, body: dict[str, Any] = Body(...),
                       principal: Principal = require("registry:publish|dev:seed")) -> dict[str, Any]:
    req = body.get("request")
    if not isinstance(req, dict) or req.get("kind") != "ReleaseBundleRequest":
        raise ProblemError(422, "invalid_bundle_request")
    spec = req.get("spec") or {}
    if spec.get("mode") != "full" or "full" not in spec:
        raise ProblemError(422, "unsupported_mode", detail="M1 builds mode: full only")
    full = spec["full"]
    db = request.app.state.db
    settings = request.app.state.settings
    missing: list[str] = []
    async with db.tx(role="svc_cpa_registry") as conn:
        async def pin(sel: str) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
            ref, doc = await _resolve(conn, sel)
            if ref is None:
                missing.append(sel)
            return ref, doc

        wd = full.get("workerDeployment") or {}
        build = wd.get("buildId") or settings.worker_build_id
        router_ref, router_doc = await pin(full["router"])
        graphs: dict[str, Any] = {}
        docs: list[Any] = []
        for wt in WORKFLOW_TYPES:
            ref, doc = await pin(full["graphs"][wt]) if wt in (full.get("graphs") or {}) else (None, None)
            if wt not in (full.get("graphs") or {}):
                missing.append(f"graphs.{wt}")
            graphs[wt] = ref
            docs.append(doc)
        policies = []
        for sel in full.get("policies") or []:
            ref, doc = await pin(sel)
            if ref:
                policies.append(ref)
                docs.append(doc)
        msp_ref, msp_doc = await pin(full["memorySnapshotPolicy"]) if full.get("memorySnapshotPolicy") else (None, None)
        if missing:
            raise ProblemError(422, "unresolvable_selectors", selectors=missing)
        nc, _ = await _resolve(conn, "nodecatalog/replicant-worker")
        closure: set[str] = set()
        for d in docs + [router_doc, msp_doc]:
            _refs_in((d or {}).get("spec"), closure)
        agents, envs, models_used, cli_images = [], [], {}, {}
        for sel in sorted(closure):
            ref, doc = await _resolve(conn, sel)
            if ref is None:
                log.warning("bundle build: closure reference %s not published; skipped (M1)", sel)
                continue
            if sel.startswith("agent/"):
                agents.append(ref)
                a = (doc or {}).get("spec") or {}
                m = a.get("model") or {}
                for mid in [m.get("primary"), m.get("small"), *(m.get("fallbacks") or [])]:
                    if mid:
                        models_used.setdefault(mid, set()).add(doc["metadata"]["name"])
                img = a.get("cliImage") or {}
                if a.get("cli") in ("claude-code", "claude-agent-sdk", "codex") and IMAGE_RE.match(img.get("image", "")):
                    cli_images[a["cli"]] = {"cli": a["cli"], "version": str(img.get("version", "")),
                                            "image": img["image"]}
            else:
                envs.append(ref)
        for m in full.get("inWorkerModels") or []:
            if m.get("gatewayModelId"):
                models_used.setdefault(m["gatewayModelId"], set()).add(m.get("purpose", "worker"))
        parent = (await conn.execute(text("SELECT default_release_bundle_id FROM registry.bundle_assignments WHERE "
                                          "env = :e"), {"e": settings.replicant_env})).scalar()
        bspec: dict[str, Any] = {
            "worker_deployment": {"deployment_name": "replicant-worker", "build_id": build,
                                  "image_digest": wd.get("imageDigest") or "sha256:" + "0" * 64},
            "node_catalog": nc or registry._placeholder_ref("nodecatalog/replicant-worker@1"),
            "router": router_ref, "graphs": graphs,
            "agents": sorted(agents, key=lambda r: r["ref"]), "environments": sorted(envs, key=lambda r: r["ref"]),
            "policies": sorted(policies, key=lambda r: r["ref"]), "prompts": [],
            "cli_images": sorted(cli_images.values(), key=lambda c: c["cli"]),
            "models": [{"gateway_model_id": k, "used_by": sorted(v)} for k, v in sorted(models_used.items())],
            "memory_snapshot_policy": msp_ref or registry._placeholder_ref("policy/memory-snapshot-default@1"),
            "lineage": {"parent_release_bundle_id": parent} if parent else {},
        }
        try:
            doc = registry.bundle_document(bspec, principal.principal_id, None, spec.get("changeSummary"))
        except registry.BundleInvalid as e:
            raise ProblemError(422, "invalid_bundle", errors=e.errors) from None
        blob_writer = request.app.state.s3.writer("registry") if settings.s3_endpoint else None
        rb, digest, created = await registry.register_bundle(conn, db, doc, principal.principal_id, blob_writer)
        await conn.execute(text("UPDATE registry.release_bundles SET request_name = coalesce(request_name, :n) WHERE "
                                "release_bundle_id = :rb"), {"n": (req.get("metadata") or {}).get("name"), "rb": rb})
        row = (await conn.execute(text("SELECT promotion_state, required_tier, require_shadow, change_classes, "
                                       "parent_release_bundle_id FROM registry.release_bundles WHERE "
                                       "release_bundle_id = :rb"), {"rb": rb})).first()
    response.status_code = 201 if created else 200
    out = {"release_bundle_id": rb, "digest": digest, "created": created, "promotion_state": row.promotion_state,
           "required_tier": row.required_tier, "require_shadow": row.require_shadow,
           "change_classes": list(row.change_classes or [])}
    if row.parent_release_bundle_id:
        out["parent_release_bundle_id"] = row.parent_release_bundle_id
    return out


@router.get("/v1/registry/bundles/{release_bundle_id}")
async def get_bundle(release_bundle_id: str, request: Request,
                     principal: Principal = require("release:read|dev:seed")) -> dict[str, Any]:
    async with request.app.state.db.tx(role="svc_cpa_registry") as conn:
        r = (await conn.execute(text("SELECT spec, metadata, promotion_state, state_version, required_tier, "
                                     "change_classes FROM registry.release_bundles WHERE release_bundle_id = :rb"),
                                {"rb": release_bundle_id})).first()
    if r is None:
        raise ProblemError(404, "not_found")
    return {"release_bundle_id": release_bundle_id, "promotion_state": r.promotion_state,
            "state_version": r.state_version, "required_tier": r.required_tier,
            "change_classes": list(r.change_classes or []),
            "document": {"apiVersion": "replicant.example.com/v1", "kind": "ReleaseBundle", "metadata": r.metadata,
                         "spec": r.spec}}
