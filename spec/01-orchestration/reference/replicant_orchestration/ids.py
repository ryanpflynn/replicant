"""Identifier helpers (C-IDS 2.0.0). Pure functions, safe in workflow code.

The derivation functions reproduce the normative reference implementation in
spec/00-contracts/ids-and-naming.md section 1 and its test vectors.
"""

from __future__ import annotations

import hashlib
import re

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"

LOGICAL_ACTIVITY_ID_RE = re.compile(
    r"^[a-z][a-z0-9_]{0,40}\.c[0-9]{1,3}\.n[0-9]{1,3}\.[a-z][a-z0-9_]{0,30}$"
)
NODE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
EFFECT_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
IDEMPOTENCY_KEY_RE = re.compile(
    r"^ik1:job_[0-9a-hjkmnp-tv-z]{26}(:[a-z][a-z0-9_]{1,30}:[1-9][0-9]*)*:"
    r"[a-z][a-z0-9_]{0,40}\.c[0-9]{1,3}\.n[0-9]{1,3}\.[a-z][a-z0-9_]{0,30}$"
)
JOB_ID_RE = re.compile(r"^job_[0-9a-hjkmnp-tv-z]{26}$")
CHILD_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")


def b32_130(digest: bytes) -> str:
    """First 130 bits of digest as 26 lowercase Crockford base32 characters."""
    n = int.from_bytes(digest[:17], "big") >> (136 - 130)
    return "".join(ALPHABET[(n >> (5 * (25 - i))) & 31] for i in range(26))


def derived(prefix: str, *fields: str) -> str:
    return f"{prefix}_{b32_130(hashlib.sha256(chr(10).join(fields).encode()).digest())}"


def logical_activity_id(node_key: str, cycle: int, ordinal: int, effect: str) -> str:
    lid = f"{node_key}.c{cycle}.n{ordinal}.{effect}"
    if not LOGICAL_ACTIVITY_ID_RE.match(lid) or len(lid) > 120:
        raise ValueError(f"invalid logical activity id {lid!r}")
    return lid


def idempotency_key(workflow_id: str, lid: str) -> str:
    key = f"ik1:{workflow_id}:{lid}"
    if not IDEMPOTENCY_KEY_RE.match(key) or len(key.encode()) > 255:
        raise ValueError(f"invalid idempotency key {key!r}")
    return key


def run_id_for(tenant_id: str, key: str) -> str:
    return derived("run", tenant_id, key)


def gate_id_for(job_id: str, gate_kind: str, cycle: int, ordinal: int) -> str:
    return derived("gate", job_id, gate_kind, f"c{cycle}", f"n{ordinal}")


def takeover_id_for(job_id: str, ordinal: int) -> str:
    return derived("tko", job_id, f"n{ordinal}")


def child_workflow_id(job_id: str, kind: str, n: int) -> str:
    if not CHILD_KIND_RE.match(kind) or n < 1:
        raise ValueError("invalid child workflow kind/counter")
    return f"{job_id}:{kind}:{n}"


def job_trace_id(job_id: str) -> str:
    return hashlib.sha256(("replicant.job.trace.v1:" + job_id).encode()).hexdigest()[:32]


# Contract test vectors (C-IDS section 1). Asserted by tests/test_ids.py.
TEST_VECTORS = [
    (lambda: derived("run", "payments", "ik1:job_01m5p7x7r0bt69j0h0fbxepw90:implementer.c0.n1.run"),
     "run_gj3h3ep3phjfs9e23v768zrca5"),
    (lambda: derived("run", "payments", "ik1:job_01m5p7x7r0bt69j0h0fbxepw90:qa.c0.n1.run"),
     "run_chxtsxqzcpds1fc1c4dvw9e4em"),
    (lambda: gate_id_for("job_01m5p7x7r0bt69j0h0fbxepw90", "plan_approval", 0, 1),
     "gate_r1a2avb51zgbsjy0x4ghyktt2e"),
    (lambda: job_trace_id("job_01m5p7x7r0bt69j0h0fbxepw90"), "fc0575170c8597ce9805552723c08d6e"),
]
