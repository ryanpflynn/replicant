"""Shared C-DEF vectors (src/replicant_cdef/vectors.json). WORKER's reader tests should load the same file:
`importlib.resources.files("replicant_cdef").joinpath("vectors.json")`."""
import json
from importlib import resources

import pytest

from replicant_cdef import (BlobDigestMismatch, blob_key, bundle_blob, bundle_pointer, definition_blob, read_bundle_spec,
                          verify_blob)

V = json.loads(resources.files("replicant_cdef").joinpath("vectors.json").read_text("utf-8"))


def test_definition_vector():
    d, body = definition_blob(V["definition"]["document"])
    assert d == V["definition"]["digest"] and body.decode() == V["definition"]["body_utf8"]
    assert blob_key(d) == V["definition"]["blob_key"]
    verify_blob(body, d)


def test_bundle_vector_and_reader():
    rb, d, body = bundle_blob(V["bundle"]["spec"])
    assert (rb, d) == (V["bundle"]["release_bundle_id"], V["bundle"]["digest"])
    assert body.decode() == V["bundle"]["body_utf8"] and blob_key(d) == V["bundle"]["blob_key"]
    pk, pbody = bundle_pointer(rb, d)
    assert (pk, pbody.decode()) == (V["bundle"]["pointer_key"], V["bundle"]["pointer_body_utf8"])
    store = {blob_key(d): body}
    assert read_bundle_spec(pbody, store.__getitem__) == (rb, V["bundle"]["spec"])
    # the pre-fix layout (whole document stored under the spec digest) must be rejected
    doc = json.dumps({"apiVersion": "replicant.example.com/v1", "kind": "ReleaseBundle", "metadata": {},
                      "spec": V["bundle"]["spec"]}).encode()
    with pytest.raises(BlobDigestMismatch):
        read_bundle_spec(pbody, {blob_key(d): doc}.__getitem__)
