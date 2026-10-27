"""§2 identifier catalog: constructors for derived and structured IDs."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from replicant_ids.encoding import b32_130, derived, ulid
from replicant_ids.regex import validate

# Prefixes whose bodies are ULIDs (minted, not derived).
ULID_PREFIXES = frozenset({
    "usr", "jrq", "job", "sch", "dlg", "opr", "lnk", "grp", "rpl", "cmp", "prop", "lbl", "eval", "msg", "act",
    "ntc", "mem", "leak", "del", "mcfl", "mqtn", "mprg", "mexp", "mact", "aud", "gtask", "rtk", "gwu", "rsp",
    "pub", "rec", "rgr",
})


def new_id(prefix: str) -> str:
    """Mint a ULID-bodied ID for one of the catalog's ULID prefixes (`new_id("job")`)."""
    if prefix not in ULID_PREFIXES:
        raise ValueError(f"{prefix!r} is not a ULID prefix in C-IDS §2")
    return ulid(prefix)


_LOGICAL_PART = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_EFFECT = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


def logical_activity_id(node_key: str, cycle: int, ordinal: int, effect: str) -> str:
    """`<node_key>.c<cycle>.n<ordinal>.<effect>`, e.g. `implementer.c0.n1.run`."""
    if not _LOGICAL_PART.match(node_key) or not _EFFECT.match(effect):
        raise ValueError("invalid node_key or effect")
    if not (0 <= cycle <= 999 and 1 <= ordinal <= 999):
        raise ValueError("cycle must be 0..999 and ordinal 1..999")
    return validate("logical_activity_id", f"{node_key}.c{cycle}.n{ordinal}.{effect}")


def child_workflow_id(job_id: str, kind: str, n: int) -> str:
    """Internal child workflow ID `<job_id>:<kind>:<n>`."""
    if n < 1:
        raise ValueError("n must be >= 1")
    return validate("workflow_id_job", f"{validate('job_id', job_id)}:{kind}:{n}")


def idempotency_key(workflow_id: str, logical_activity: str) -> str:
    """`ik1:<workflow_id>:<logical_activity_id>` (<= 255 bytes)."""
    key = f"ik1:{validate('workflow_id_job', workflow_id)}:{validate('logical_activity_id', logical_activity)}"
    if len(key.encode()) > 255:
        raise ValueError("idempotency key longer than 255 bytes")
    return validate("idempotency_key", key)


def run_id(tenant_id: str, idem_key: str) -> str:
    """`run_<derived>` = derived("run", tenant_id, idempotency_key)."""
    return derived("run", validate("tenant_id", tenant_id), validate("idempotency_key", idem_key))


def run_body(rid: str) -> str:
    return validate("run_id", rid)[len("run_"):]


def agentrun_name(rid: str) -> str:
    """Kubernetes form `ar-<26-char body of run_id>`."""
    return f"ar-{run_body(rid)}"


def gate_id(job_id: str, gate_kind: str, cycle: int, ordinal: int) -> str:
    return derived("gate", validate("job_id", job_id), gate_kind, f"c{cycle}", f"n{ordinal}")


def decision_id(channel: str, channel_event_id: str) -> str:
    return derived("dec", channel, channel_event_id)


def takeover_id(job_id: str, ordinal: int) -> str:
    return derived("tko", validate("job_id", job_id), f"n{ordinal}")


def artifact_index_id(bucket_class: str, key: str) -> str:
    return derived("art", bucket_class, key)


def notification_id(notification_dedupe_key: str) -> str:
    return derived("ntf", notification_dedupe_key)


def system_workflow_id(name: str, suffix: str | None = None) -> str:
    return validate("system_workflow_id", f"sys:{name}" + (f":{suffix}" if suffix else ""))


_DEPLOYMENT_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
_BUILD_ID = re.compile(r"^b\d{8}-[0-9a-f]{12}$")


def worker_deployment_version(deployment_name: str, build_id: str) -> str:
    """Search-attribute rendering `replicant-worker:b20261024-3f9a1c2e7d4b`."""
    if not _DEPLOYMENT_NAME.match(deployment_name) or not _BUILD_ID.match(build_id):
        raise ValueError("invalid deployment name or build id (b<YYYYMMDD>-<12 hex>)")
    return f"{deployment_name}:{build_id}"


# --- release_bundle_id: rb_<b32_130(sha256(JCS(spec)))> -----------------------------------------

def _jcs_number(x: float | int) -> str:
    if isinstance(x, bool):
        raise TypeError
    if isinstance(x, int):
        return str(x)
    if not math.isfinite(x):
        raise ValueError("JCS forbids NaN and Infinity")
    if x == int(x) and abs(x) < 1e21:
        return str(int(x))
    # ECMAScript Number.prototype.toString shortest round-trip form (RFC 8785 §3.2.2.3).
    r = repr(x)
    if "e" in r:
        mant, exp = r.split("e")
        e = int(exp)
        if -7 < e < 21:
            return format(x, "f").rstrip("0").rstrip(".")
        return f"{mant}e{'+' if e > 0 else '-'}{abs(e)}"
    return r


def jcs(value: Any) -> bytes:
    """RFC 8785 JSON Canonicalization Scheme (keys sorted by UTF-16 code units)."""
    def enc(v: Any) -> str:
        if v is None:
            return "null"
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, (int, float)):
            return _jcs_number(v)
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, (list, tuple)):
            return "[" + ",".join(enc(i) for i in v) + "]"
        if isinstance(v, dict):
            items = sorted(v.items(), key=lambda kv: kv[0].encode("utf-16-be"))
            return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + enc(val) for k, val in items) + "}"
        raise TypeError(f"not JSON: {type(v).__name__}")
    return enc(value).encode()


def release_bundle_id(spec: Any) -> tuple[str, str]:
    """Returns (`rb_<26>`, `sha256:<64 hex>`) for a bundle spec."""
    d = hashlib.sha256(jcs(spec)).digest()
    return f"rb_{b32_130(d)}", f"sha256:{d.hex()}"
