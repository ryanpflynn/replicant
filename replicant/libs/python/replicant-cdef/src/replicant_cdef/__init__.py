"""C-DEF 3.0.0 registry digests and S3 layout. Writers and readers MUST use these functions.

Normative sources (spec/00-contracts):
* definitions-contracts.md §4.1 rule 2: a definition's `digest = "sha256:" + hex(sha256(JCS(document)))` over the
  entire authored document (envelope + spec).
* §4.3 step 4 / release-bundle.md §2: a bundle's `digest = sha256(JCS(spec))` (spec only, metadata excluded) and
  `release_bundle_id = "rb_" + b32_130(digest)`.
* §4.1 rule 5 / §4.3 step 5 / C-ART `registry_blob`: the blob is `blobs/sha256/<hex>.json` where `<hex>` is that digest,
  and §6: readers "verify `sha256(body) == digest`" on the raw bytes. Hence the blob body is exactly the JCS bytes of
  the hashed object: the whole document for a definition, **the `spec` object for a bundle** (not the
  ReleaseBundle document, whose metadata is outside the digest).
* §4.3 step 5 / CCR-02-02: pointer `bundles/<release_bundle_id>.json` = `{"release_bundle_id", "digest"}` with the
  bundle digest. Readers: pointer -> digest -> blob -> verify -> the blob IS the spec.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from replicant_ids import b32_130, jcs

__all__ = ["BlobDigestMismatch", "blob_key", "bundle_blob", "bundle_digest", "bundle_pointer", "definition_blob",
           "definition_digest", "digest_of_body", "parse_pointer", "read_bundle_spec", "verify_blob"]


class BlobDigestMismatch(ValueError):
    """sha256(body) != digest (DefinitionDigestMismatch / definition_not_resolvable)."""


def digest_of_body(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def definition_blob(document: dict[str, Any]) -> tuple[str, bytes]:
    """(digest, body) for a registry definition (envelope + spec)."""
    body = jcs(document)
    return digest_of_body(body), body


def definition_digest(document: dict[str, Any]) -> str:
    return definition_blob(document)[0]


def bundle_blob(spec: dict[str, Any]) -> tuple[str, str, bytes]:
    """(release_bundle_id, digest, body) for a release bundle; body = JCS(spec)."""
    body = jcs(spec)
    d = hashlib.sha256(body).digest()
    return f"rb_{b32_130(d)}", "sha256:" + d.hex(), body


def bundle_digest(spec: dict[str, Any]) -> tuple[str, str]:
    rb, digest, _ = bundle_blob(spec)
    return rb, digest


def blob_key(digest: str) -> str:
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise ValueError(f"bad digest {digest!r}")
    return f"blobs/sha256/{digest.removeprefix('sha256:')}.json"


def bundle_pointer(release_bundle_id: str, digest: str) -> tuple[str, bytes]:
    """(key, body) of the write-once pointer."""
    return f"bundles/{release_bundle_id}.json", jcs({"release_bundle_id": release_bundle_id, "digest": digest})


def parse_pointer(body: bytes) -> tuple[str, str]:
    p = json.loads(body)
    return p["release_bundle_id"], p["digest"]


def verify_blob(body: bytes, digest: str) -> bytes:
    if digest_of_body(body) != digest:
        raise BlobDigestMismatch(f"registry blob {digest[7:19]} digest mismatch")
    return body


def read_bundle_spec(pointer_body: bytes, get_blob: Any) -> tuple[str, dict[str, Any]]:
    """Reader helper: pointer bytes + `get_blob(key) -> bytes` -> (release_bundle_id, spec), fully verified
    (the id must also be the one derived from the spec digest)."""
    rb, digest = parse_pointer(pointer_body)
    spec = json.loads(verify_blob(get_blob(blob_key(digest)), digest))
    if bundle_digest(spec)[0] != rb:
        raise BlobDigestMismatch(f"pointer {rb} does not match its spec digest")
    return rb, spec
