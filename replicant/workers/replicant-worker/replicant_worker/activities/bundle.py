"""Release Bundle resolution for activities (C-DEF section 6: definitions resolve only through the job's bundle).

Order: the local registry bucket (`bundles/<release_bundle_id>.json` -> bundle blob -> definition blobs, every blob
digest-checked, the same objects runner-local reads, RUNNER-02); when the bundle pointer is absent (M1 before seeding)
the built-in v0 local set (`replicant_worker.local_bundle`) stands in for any `rb_` ID, and a warning is logged.
Cached by digest in-process (content is immutable).
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

import replicant_cdef
from temporalio.exceptions import ApplicationError

from replicant_worker import local_bundle
from replicant_worker.activities import s3

log = logging.getLogger("replicant_worker.bundle")
_lock = threading.Lock()
_cache: dict[str, "Bundle"] = {}
_blob_cache: dict[str, dict[str, Any]] = {}


@dataclass
class Bundle:
    release_bundle_id: str
    source: str  # registry | local
    docs: dict[str, dict[str, Any]] = field(default_factory=dict)  # ref -> envelope
    digests: dict[str, str] = field(default_factory=dict)  # ref -> sha256:<hex>

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
    b = Bundle(release_bundle_id=rb, source="registry")
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
        log.warning("bundle %s not in the local registry; using the built-in v0 local set", release_bundle_id)
        b = _local(release_bundle_id)
    with _lock:
        _cache[release_bundle_id] = b
    return b


def publish_local_set() -> int:
    """Write the v0 definition blobs to the local registry bucket (write-once, content-addressed). Local only."""
    n = 0
    for key, body in local_bundle.blobs().items():
        s3.put_once(s3.ref("registry", key), body, "application/json")  # write-once, replicant_cdef layout
        n += 1
    return n
