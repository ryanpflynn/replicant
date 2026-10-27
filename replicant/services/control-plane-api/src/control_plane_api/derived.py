"""Derived Release Bundles from pinned project configuration (v0 CONFIG, plan §1 "Pinned"; DEVIATIONS CONFIG-D4).

At intake the project's effective pinned config (gates, risk, workflows) is compiled onto the job's base bundle:
* a `gate_rules` policy replacing the base one (plan/publish gate conditions, requester_may_decide),
* a `risk_rules` policy (large-change file count, sensitive and forbidden paths; read by risk.classify and
  verifier.check),
* tuned or custom graph definitions replacing `spec.graphs[<type>]` (params and maxVisits are compiled into the
  definition here, so the worker only follows the bundle's graph mapping).
The definitions and the bundle are written to the registry bucket with the shared C-DEF layout (`replicant_cdef`) and
registered with `registry.register_bundle`; the row records `base_release_bundle_id`, `request_name =
'project-config'` and `request_digest = sha256(JCS({base, pinned}))`, so the same (base, config) reuses one `rb_`.
The worker sees only a bundle ID: no config change touches an image.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from sqlalchemy import text

from control_plane_api import graph_view, projects as P, registry

log = logging.getLogger(__name__)

REQUEST_NAME = "project-config"
PUBLISH_ALWAYS_FOR = ["bugfix", "feature", "api_change", "hotfix"]
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class CompileError(Exception):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("; ".join(e["message"] for e in errors)[:500])
        self.errors = errors


def config_digest(base_rb: str, pinned: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(P.jcs({"base": base_rb, "pinned": pinned, "v": 1})).hexdigest()


# ------------------------------------------------------------------------------------------------ registry I/O
class Registry:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.bucket = state.settings.bucket("registry")

    async def get_json(self, key: str) -> Any:
        s3 = self.state.s3.client

        def _get() -> Any:
            from botocore.exceptions import ClientError
            try:
                return json.loads(s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())
            except ClientError:
                return None
        return await asyncio.to_thread(_get)

    async def blob(self, digest: str) -> dict[str, Any]:
        import replicant_cdef
        s3 = self.state.s3.client

        def _get() -> dict[str, Any]:
            body = s3.get_object(Bucket=self.bucket, Key=replicant_cdef.blob_key(digest))["Body"].read()
            return json.loads(replicant_cdef.verify_blob(body, digest))
        try:
            return await asyncio.to_thread(_get)
        except Exception as e:  # noqa: BLE001
            raise CompileError([{"path": "/", "message": f"definition {digest[:19]} unreadable: {type(e).__name__}"}]) \
                from None

    async def put_definition(self, doc: dict[str, Any]) -> str:
        import replicant_cdef
        digest, body = replicant_cdef.definition_blob(doc)
        await self.state.s3.put_once(self.bucket, replicant_cdef.blob_key(digest), body)
        return digest

    async def node_catalog(self, build_id: str) -> dict[str, dict[str, Any]] | None:
        """nodeType -> configSchema from the worker's published NodeCatalog (WORKER-D2, CONFIG-D5), or None."""
        doc = await self.get_json(f"nodecatalogs/replicant-worker/{build_id}.json")
        if not doc:
            return None
        return {n["nodeType"]: (n.get("configSchema") or {}) for n in (doc.get("spec") or {}).get("nodes") or []}


async def default_base(state: Any) -> tuple[str, dict[str, Any]]:
    async with state.db.tx(role="svc_cpa_registry") as conn:
        r = (await conn.execute(text(
            "SELECT b.release_bundle_id, b.spec FROM registry.bundle_assignments a JOIN registry.release_bundles b ON "
            "b.release_bundle_id = a.default_release_bundle_id WHERE a.env = :e"),
            {"e": state.settings.replicant_env})).first()
    if r is None:
        raise CompileError([{"path": "/", "message": "no default release bundle for this environment"}])
    return r.release_bundle_id, r.spec


# ------------------------------------------------------------------------------------------------ compilers (pure)
def compile_gate_rules(base: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    """base: the gate_rules policy spec (WS-01 fixture shape, policyType removed)."""
    g = copy.deepcopy(base)
    plan, pub = g["gates"]["plan_approval"], g["gates"]["publish_approval"]
    mode = gates.get("plan", "off")
    if mode == "off":
        plan.update(enabled=False, always_for=[], risk_at_least="critical")
    elif mode == "risk_medium":
        plan.update(enabled=True, always_for=[], risk_at_least="medium")
    else:
        plan.update(enabled=True, always_for=list(P.WORKFLOW_TYPES), risk_at_least="low")
    plan["channels"] = list(pub.get("channels") or plan.get("channels") or ["ui", "cli"])
    if gates.get("publish", "always") == "always":
        pub.update(always_for=list(PUBLISH_ALWAYS_FOR))
    else:
        pub.update(always_for=[], risk_at_least="medium")
    may = bool(gates.get("requester_may_decide", True))
    plan["requester_may_decide"] = may
    pub["requester_may_decide"] = may
    return g


def risk_rules_spec(risk: dict[str, Any]) -> dict[str, Any]:
    return {"policyType": "risk_rules", "large_change_files": int(risk.get("large_change_files", 20)),
            "sensitive_paths": list(risk.get("sensitive_paths") or []),
            "forbidden_paths": list(risk.get("forbidden_paths") or [])}


def policy_doc(name: str, description: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {"apiVersion": "replicant.example.com/v1", "kind": "Policy",
            "metadata": {"name": name, "scope": {"level": "tenant"}, "owners": ["replicant-platform"],
                         "description": description, "labels": {"replicant.config": "project"}},
            "spec": spec}


def tune_graph(base_doc: dict[str, Any], wcfg: dict[str, Any]) -> dict[str, Any]:
    if wcfg.get("graph_yaml"):
        try:
            doc = yaml.safe_load(wcfg["graph_yaml"])
        except yaml.YAMLError as e:
            raise CompileError([{"path": "/graph_yaml", "message": f"YAML: {str(e)[:200]}"}]) from None
        if not isinstance(doc, dict):
            raise CompileError([{"path": "/graph_yaml", "message": "not a GraphDefinition document"}])
    else:
        doc = copy.deepcopy(base_doc)
    spec = doc.setdefault("spec", {})
    params = (wcfg.get("params") or {})
    if params:
        spec["params"] = {**(spec.get("params") or {}), **params}
        rounds = params.get("max_review_rounds")
        if rounds is not None:
            for n in spec.get("nodes") or []:
                if n.get("type") == "review_run":
                    n["maxVisits"] = int(rounds)
    for key, mv in (wcfg.get("max_visits") or {}).items():
        node = next((n for n in spec.get("nodes") or [] if n.get("key") == key), None)
        if node is None:
            raise CompileError([{"path": f"/max_visits/{key}", "message": f"no node {key} in the graph"}])
        node["maxVisits"] = int(mv)
    return doc


def validate_graph(doc: dict[str, Any], wt: str, catalog: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """Structural checks before the worker's own compose/validate: kind, workflow type, node types in the worker's
    catalog with their config schemas, unique keys, edges to known nodes or `end:*`, entry, publish gate (I-3)."""
    errs: list[dict[str, str]] = []

    def e(path: str, msg: str) -> None:
        errs.append({"path": path, "message": msg})
    if doc.get("kind") != "GraphDefinition":
        e("/kind", "kind must be GraphDefinition")
    spec = doc.get("spec") or {}
    if spec.get("workflowType") != wt:
        e("/spec/workflowType", f"workflowType must be {wt}")
    nodes = spec.get("nodes") or []
    if not isinstance(nodes, list) or not nodes:
        e("/spec/nodes", "nodes are required")
        return errs
    keys: set[str] = set()
    for i, n in enumerate(nodes):
        k, t = (n or {}).get("key"), (n or {}).get("type")
        if not isinstance(k, str) or not KEY_RE.match(k):
            e(f"/spec/nodes/{i}/key", "key must match ^[a-z][a-z0-9_]*$")
        elif k in keys:
            e(f"/spec/nodes/{i}/key", f"duplicate key {k}")
        else:
            keys.add(k)
        if t not in catalog:
            e(f"/spec/nodes/{i}/type", f"unknown node type {t!r} (not in the worker's node catalog)")
        elif catalog[t]:
            for ve in Draft202012Validator(catalog[t]).iter_errors(n.get("config") or {}):
                e(f"/spec/nodes/{i}/config", f"{k}: {ve.message[:200]}")
        mv = n.get("maxVisits")
        if mv is not None and (not isinstance(mv, int) or not 1 <= mv <= 50):
            e(f"/spec/nodes/{i}/maxVisits", "maxVisits must be 1..50")
    if spec.get("entry") not in keys:
        e("/spec/entry", "entry must name a node")

    def target(path: str, to: Any) -> None:
        if not isinstance(to, str) or not (to in keys or to.startswith("end:")):
            e(path, f"edge target {to!r} is neither a node nor end:<outcome>")
    for i, ed in enumerate(spec.get("edges") or []):
        if ed.get("from") not in keys:
            e(f"/spec/edges/{i}/from", f"unknown node {ed.get('from')!r}")
        if "to" in ed:
            target(f"/spec/edges/{i}/to", ed["to"])
        for j, sw in enumerate(ed.get("switch") or []):
            target(f"/spec/edges/{i}/switch/{j}/to", sw.get("to"))
        if "default" in ed:
            target(f"/spec/edges/{i}/default", ed["default"])
    types = {n.get("type") for n in nodes}
    if "publisher" in types and not any(n.get("type") == "human_gate" and (n.get("config") or {}).get("gateKind") ==
                                        "publish_approval" for n in nodes):
        e("/spec/nodes", "a graph that publishes needs a publish_approval human_gate (I-3)")
    return errs


# ------------------------------------------------------------------------------------------------ compile
async def _catalog(reg: Registry, base_spec: dict[str, Any], build_id: str) -> dict[str, dict[str, Any]]:
    cat = await reg.node_catalog(build_id)
    if cat is not None:
        return cat
    # worker has not published its catalog: the node types the base bundle's graphs already use (no config schemas)
    out: dict[str, dict[str, Any]] = {}
    for pin in (base_spec.get("graphs") or {}).values():
        try:
            doc = await reg.blob(pin["digest"])
        except CompileError:
            continue
        for n in (doc.get("spec") or {}).get("nodes") or []:
            out.setdefault(n.get("type"), {})
    return out


async def compile_graphs(reg: Registry, base_spec: dict[str, Any], eff: dict[str, Any]
                         ) -> dict[str, tuple[str, dict[str, Any]]]:
    """workflow type -> (ref name, definition document), validated."""
    build = base_spec["worker_deployment"]["build_id"]
    catalog = await _catalog(reg, base_spec, build)
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    errs: list[dict[str, str]] = []
    for wt, wcfg in sorted(eff["workflows"].items()):
        pin = (base_spec.get("graphs") or {}).get(wt)
        if pin is None:
            errs.append({"path": f"/workflows/{wt}", "message": f"the base bundle has no {wt} graph"})
            continue
        base_doc = await reg.blob(pin["digest"])
        try:
            doc = tune_graph(base_doc, wcfg)
        except CompileError as ce:
            errs += [{**x, "path": f"/workflows/{wt}{x['path']}"} for x in ce.errors]
            continue
        verrs = validate_graph(doc, wt, catalog)
        if verrs:
            errs += [{**x, "path": f"/workflows/{wt}{x['path']}"} for x in verrs]
            continue
        base_name = pin["ref"].split("/", 1)[1].split("@", 1)[0]
        name = f"{base_name}-c{hashlib.sha256(P.jcs(doc)).hexdigest()[:10]}"
        doc = copy.deepcopy(doc)
        md = doc.setdefault("metadata", {})
        md["name"] = name
        md.setdefault("scope", {"level": "global"})
        md.setdefault("owners", ["replicant-platform"])
        md.setdefault("labels", {})["replicant.config"] = "project"
        md["labels"]["workflow-type"] = wt
        out[wt] = (name, doc)
    if errs:
        raise CompileError(errs)
    return out


async def preview_graphs(state: Any, eff: dict[str, Any]) -> dict[str, Any]:
    """config:validate: the compiled graphs in the shape of GET /v1/jobs/{id}/graph (for lib/graph.ts)."""
    _, base_spec = await default_base(state)
    out = {}
    for wt, (name, doc) in (await compile_graphs(Registry(state), base_spec, eff)).items():
        out[wt] = {"workflow_type": wt, "definition": {"ref": f"graph/{name}@1", "name": name},
                   "graph": graph_view.build_view(doc.get("spec") or {}, [], {"status": "queued"}, [])}
    return out


async def _policy_types(reg: Registry, base_spec: dict[str, Any]) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """policyType -> (pinned ref, document) of the base bundle's policies."""
    out = {}
    for pr in base_spec.get("policies") or []:
        doc = await reg.blob(pr["digest"])
        out[str((doc.get("spec") or {}).get("policyType"))] = (pr, doc)
    return out


async def ensure_derived(state: Any, conn: Any, db: Any, base_rb: str, tenant_id: str, project_id: str,
                         eff: dict[str, Any], actor: str) -> str:
    """The derived bundle for (base, pinned config): reused when it exists, else compiled and registered in `conn`
    (a transaction with the svc_cpa_registry role). Returns its release_bundle_id."""
    pinned = P.pinned_part(eff)
    dg = config_digest(base_rb, pinned)
    hit = (await conn.execute(text(
        "SELECT release_bundle_id FROM registry.release_bundles WHERE base_release_bundle_id = :b AND request_name = "
        ":n AND request_digest = :d"), {"b": base_rb, "n": REQUEST_NAME, "d": dg})).scalar()
    if hit:
        return hit
    base_spec = (await conn.execute(text("SELECT spec FROM registry.release_bundles WHERE release_bundle_id = :b"),
                                    {"b": base_rb})).scalar()
    if base_spec is None:
        raise CompileError([{"path": "/", "message": f"base bundle {base_rb} not in the registry"}])
    reg = Registry(state)
    spec = copy.deepcopy(base_spec)
    pols = await _policy_types(reg, base_spec)
    if "gate_rules" not in pols:
        raise CompileError([{"path": "/gates", "message": "the base bundle has no gate_rules policy"}])
    gate_ref, gate_doc = pols["gate_rules"]
    gate_spec = {k: v for k, v in gate_doc["spec"].items() if k != "policyType"}
    short = dg.split(":")[1][:10]
    new_pols = [p for p in spec.get("policies") or [] if p["ref"] != gate_ref["ref"] and
                not (pols.get("risk_rules") and p["ref"] == pols["risk_rules"][0]["ref"])]
    gdoc = policy_doc(f"gate-rules-c{short}", f"gate rules of project {project_id}",
                      {"policyType": "gate_rules", **compile_gate_rules(gate_spec, pinned["gates"])})
    rdoc = policy_doc(f"risk-rules-c{short}", f"risk rules of project {project_id}", risk_rules_spec(pinned["risk"]))
    for d in (gdoc, rdoc):
        new_pols.append({"ref": f"policy/{d['metadata']['name']}@1", "digest": await reg.put_definition(d)})
    spec["policies"] = sorted(new_pols, key=lambda r: r["ref"])
    for wt, (name, doc) in (await compile_graphs(reg, base_spec, eff)).items():
        spec["graphs"][wt] = {"ref": f"graph/{name}@1", "digest": await reg.put_definition(doc)}
    spec["lineage"] = {"parent_release_bundle_id": base_rb}
    doc = registry.bundle_document(spec, actor, None, f"project {project_id} configuration on {base_rb}")
    rb, _digest, _created = await registry.register_bundle(conn, db, doc, actor, state.s3.writer("registry"))
    await conn.execute(text(
        "UPDATE registry.release_bundles SET base_release_bundle_id = :b, tenant_id = :t, request_name = :n, "
        "request_digest = :d WHERE release_bundle_id = :rb AND request_digest IS NULL"),
        {"b": base_rb, "t": tenant_id, "n": REQUEST_NAME, "d": dg, "rb": rb})
    # a derived bundle inherits its base's promotion (no eval of team configuration in v0; DEVIATIONS CONFIG-D4)
    why = f"derived from promoted {base_rb} (project {project_id} configuration)"
    state = (await conn.execute(text("SELECT promotion_state FROM registry.release_bundles WHERE release_bundle_id = "
                                     ":rb"), {"rb": rb})).scalar()
    for nxt in {"built": ("evaluating", "eval_passed", "promoted"), "evaluating": ("eval_passed", "promoted"),
                "eval_passed": ("promoted",)}.get(state, ()):
        await registry.transition(conn, db, rb, nxt, actor, why, tier="T1", evidence={"derived_config": dg})
    log.info("derived bundle %s for project %s on %s (%s)", rb, project_id, base_rb, dg[:19])
    return rb
