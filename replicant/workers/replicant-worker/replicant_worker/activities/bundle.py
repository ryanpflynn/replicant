"""Release Bundle resolution for activities (C-DEF section 6: definitions resolve only through the job's bundle).

Order: the local registry bucket (`bundles/<release_bundle_id>.json` -> bundle blob -> definition blobs, every blob
digest-checked, the same objects runner-local reads, RUNNER-02); when the bundle pointer is absent (M1 before seeding)
the built-in v0 local set (`replicant_worker.local_bundle`) stands in for any `rb_` ID, and a warning is logged.
Cached by digest in-process (content is immutable).
"""
from __future__ import annotations

import fnmatch
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from temporalio.exceptions import ApplicationError

import replicant_cdef
from replicant_worker import local_bundle
from replicant_worker.activities import s3

log = logging.getLogger("replicant_worker.bundle")
_lock = threading.Lock()
RISK_DEFAULTS: dict[str, Any] = {"large_change_files": 20, "sensitive_paths": [], "forbidden_paths": []}
_cache: dict[str, "Bundle"] = {}
_blob_cache: dict[str, dict[str, Any]] = {}


@dataclass
class Bundle:
    release_bundle_id: str
    source: str  # registry | local
    docs: dict[str, dict[str, Any]] = field(default_factory=dict)  # ref -> envelope
    digests: dict[str, str] = field(default_factory=dict)  # ref -> sha256:<hex>
    graph_refs: dict[str, str] = field(default_factory=dict)  # workflow type -> graph ref (spec.graphs; CONFIG)

    def _by(self, kind: str, name: str) -> tuple[str, dict[str, Any]]:
        for ref, doc in self.docs.items():
            if ref.startswith(f"{kind}/{name}@"):
                return ref, doc
        raise ApplicationError(f"{kind}/{name} is not in bundle {self.release_bundle_id}", type="NotInBundle",
                               non_retryable=True)

    def agent(self, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        ref, doc = self._by("agent", name)
        return self.pinned(ref), doc

    def environment(self, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        ref, doc = self._by("environment", name)
        return self.pinned(ref), doc

    def graph(self, file_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        ref, doc = self._by("graph", file_name)
        return self.pinned(ref), doc

    def graph_for(self, workflow_type: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """The bundle's graph for a workflow type through `spec.graphs` (derived bundles map a type to a tuned or
        custom definition, CONFIG-D4); GRAPH_FILES is only the default for bundles without the mapping."""
        ref = self.graph_refs.get(workflow_type)
        if ref and ref in self.docs:
            return self.pinned(ref), self.docs[ref]
        return self.graph(local_bundle.GRAPH_FILES[workflow_type])

    def graph_variant(self, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """A label-selected graph (MEM repo scan, DEVIATIONS MEM-D7): from the bundle when it pins one, else the
        definition shipped with this worker build (its digest is still recorded in the job's graph pin)."""
        for ref, doc in self.docs.items():
            if ref.startswith(f"graph/{name}@"):
                return self.pinned(ref), doc
        ref, doc = local_bundle.find("graph", name)
        return local_bundle.pinned(ref), doc

    def has(self, kind: str, name: str) -> bool:
        return any(ref.startswith(f"{kind}/{name}@") for ref in self.docs)

    def binding_for(self, repo: dict[str, Any] | None) -> dict[str, Any] | None:
        """The codebase_bindings binding that matches a repo (policy-codebase_bindings: highest priority, then the more
        specific selector), or None when the bundle has no bindings policy or only the default applies. Catalog
        selectors are not evaluated (no catalog in v0). `repo`: {host, owner, name, repo_id}."""
        pol = self.policies().get("codebase_bindings") or {}
        if not repo or not pol.get("bindings"):
            return None
        full = f"{repo.get('host', '')}/{repo.get('owner', '')}/{repo.get('name', '')}"
        best: tuple[tuple[int, int, str], dict[str, Any]] | None = None
        for b in pol["bindings"]:
            m = b.get("match") or {}
            spec = 0
            if str(repo.get("repo_id") or "") in [str(x) for x in m.get("repoIds") or []]:
                spec = 4
            else:
                for pat in m.get("repos") or []:
                    if pat == full:
                        spec = max(spec, 3)
                    elif fnmatch.fnmatchcase(full, pat):
                        spec = max(spec, 2)
            if not spec:
                continue
            key = (int(b.get("priority", 0)), spec, str(b.get("name", "")))
            if best is None or key[:2] > best[0][:2] or (key[:2] == best[0][:2] and key[2] < best[0][2]):
                best = (key, b)
        return best[1] if best else None

    def risk_rules(self) -> dict[str, Any]:
        """The bundle's risk_rules policy (CONFIG); today's constants are the defaults."""
        r = dict(RISK_DEFAULTS)
        r.update({k: v for k, v in (self.policies().get("risk_rules") or {}).items() if k in RISK_DEFAULTS})
        return r

    def pinned(self, ref: str) -> dict[str, Any]:
        kn, _, ver = ref.partition("@")
        return {"name": kn.split("/", 1)[1], "version": int(ver), "digest": self.digests[ref]}

    def policies(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for ref, doc in self.docs.items():
            if ref.startswith("policy/"):
                spec = dict(doc["spec"])
                out[str(spec.pop("policyType"))] = spec
        return out


def _get(key: str) -> bytes:
    return s3.get_bytes(s3.ref("registry", key), max_bytes=8 << 20)


def _blob(digest: str) -> dict[str, Any]:
    """A definition blob, verified on its raw bytes (C-DEF §6, replicant_cdef.verify_blob)."""
    if digest in _blob_cache:
        return _blob_cache[digest]
    try:
        doc = json.loads(replicant_cdef.verify_blob(_get(replicant_cdef.blob_key(digest)), digest))
    except replicant_cdef.BlobDigestMismatch as e:
        raise ApplicationError(str(e), type="DefinitionDigestMismatch", non_retryable=True) from None
    _blob_cache[digest] = doc
    return doc


def _from_registry(rb: str) -> Bundle | None:
    """pointer `bundles/<rb>.json` -> bundle blob (= the spec, C-DEF §4.3) -> definition blobs, all digest-checked
    with the shared replicant_cdef reader (the same objects runner-local reads, RUNNER-06)."""
    try:
        ptr = _get(f"bundles/{rb}.json")
    except ApplicationError as e:
        if e.type == "ObjectNotFound":
            return None
        raise
    try:
        got_rb, spec = replicant_cdef.read_bundle_spec(ptr, _get)
    except replicant_cdef.BlobDigestMismatch as e:
        raise ApplicationError(str(e), type="DefinitionDigestMismatch", non_retryable=True) from None
    if got_rb != rb:
        raise ApplicationError(f"pointer {rb} names {got_rb}", type="DefinitionDigestMismatch", non_retryable=True)
    refs: list[dict[str, Any]] = [*spec.get("agents", []), *spec.get("environments", []), *spec.get("policies", []),
                                  *spec.get("graphs", {}).values()]
    b = Bundle(release_bundle_id=rb, source="registry",
               graph_refs={wt: g["ref"] for wt, g in (spec.get("graphs") or {}).items() if isinstance(g, dict)})
    local = local_bundle.documents()
    for r in refs:
        ref, dg = r["ref"], r["digest"]
        try:
            b.docs[ref] = _blob(dg)
        except ApplicationError as e:
            if e.type != "ObjectNotFound":
                raise
            lref = next((k for k, v in local.items() if replicant_cdef.definition_digest(v) == dg), None)
            if lref is None:
                raise ApplicationError(f"{ref} ({dg[:19]}) is in neither the registry nor the local set",
                                       type="DefinitionDigestMismatch", non_retryable=True) from None
            b.docs[ref] = local[lref]
        b.digests[ref] = dg
    if not any(k.startswith("policy/") and v["spec"].get("policyType") == "gate_rules" for k, v in b.docs.items()):
        # M1: a bundle registered without WS-01-shaped gate rules gets the local ones (logged)
        log.warning("bundle %s has no gate_rules policy; using the v0 local gate rules", rb)
        ref = f"policy/gate-rules-local@{local_bundle.DEFINITION_VERSION}"
        b.docs[ref], b.digests[ref] = local[ref], replicant_cdef.definition_digest(local[ref])
    for ref, ldoc in local.items():  # fill policy types the registered bundle lacks (M1 leniency, logged)
        if ref.startswith("policy/") and ldoc["spec"]["policyType"] not in b.policies():
            log.warning("bundle %s has no %s policy; using the v0 local one", rb, ldoc["spec"]["policyType"])
            b.docs[ref], b.digests[ref] = ldoc, replicant_cdef.definition_digest(ldoc)
    return b


def _local(rb: str) -> Bundle:
    b = Bundle(release_bundle_id=rb, source="local")
    for ref, doc in local_bundle.documents().items():
        b.docs[ref], b.digests[ref] = doc, replicant_cdef.definition_digest(doc)
    return b


def load(release_bundle_id: str) -> Bundle:
    with _lock:
        if release_bundle_id in _cache:
            return _cache[release_bundle_id]
    b = None
    try:
        b = _from_registry(release_bundle_id)
    except ApplicationError:
        raise
    except Exception as e:  # noqa: BLE001 - S3 down: retryable
        raise ApplicationError(f"registry read failed: {type(e).__name__}", type="RegistryUnavailable") from e
    if b is None:
        # CONFIG (fallback gap): only the base dev bundle of this build may fall back to the built-in set; any other
        # rb_ (a derived bundle, a bundle of another build) must resolve from the registry, or a rebuilt worker would
        # silently run different definitions under an existing rb_ (retryable: the pointer may not be written yet).
        if not _fallback_allowed(release_bundle_id):
            raise ApplicationError(f"bundle {release_bundle_id} is not in the registry", type="RegistryUnavailable")
        log.warning("bundle %s not in the local registry; using the built-in v0 local set", release_bundle_id)
        b = _local(release_bundle_id)
    with _lock:
        _cache[release_bundle_id] = b
    return b


def _fallback_allowed(rb: str) -> bool:
    import os
    if os.environ.get("REPLICANT_BUNDLE_FALLBACK_ANY", "").lower() == "true":   # unit tests / stub harness
        return True
    build = os.environ.get("REPLICANT_WORKER_BUILD_ID")
    if not build:
        return True     # no build identity (tests): keep the M1 behaviour
    try:
        return rb == local_bundle.bundle_registry_objects(build)[0]
    except Exception:  # noqa: BLE001
        return False


def publish_node_catalog(catalog: dict[str, Any], build_id: str) -> str:
    """The worker's NodeCatalog next to the definitions (WORKER-D2): control-plane-api's config:validate rejects
    graphs with node types this build does not have (CONFIG-D5). Overwritten per start (same build, same content)."""
    key = f"nodecatalogs/replicant-worker/{build_id}.json"
    r = s3.ref("registry", key)
    s3.client().put_object(Bucket=r.bucket, Key=r.key, Body=json.dumps(catalog, sort_keys=True).encode(),
                           ContentType="application/json")
    return key


def publish_local_set() -> int:
    """Write the v0 definition blobs to the local registry bucket (write-once, content-addressed). Local only."""
    n = 0
    for key, body in local_bundle.blobs().items():
        s3.put_once(s3.ref("registry", key), body, "application/json")  # write-once, replicant_cdef layout
        n += 1
    return n
