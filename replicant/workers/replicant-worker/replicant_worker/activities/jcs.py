"""RFC 8785 JSON Canonicalization Scheme (the subset Replicant documents use: objects, arrays, strings, booleans,
null, integers and finite IEEE-754 doubles)."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _num(x: float | int) -> str:
    if isinstance(x, bool):
        raise TypeError("bool")
    if isinstance(x, int):
        return str(x)
    if not math.isfinite(x):
        raise ValueError("JCS forbids NaN and Infinity")
    if x == 0:
        return "0"
    if x.is_integer() and abs(x) < 1e21:
        return str(int(x))
    r = repr(x)  # shortest round-trip repr, as ECMAScript Number.prototype.toString
    if "e" in r or "E" in r:
        mant, _, exp = r.lower().partition("e")
        e = int(exp)
        if -7 < e < 21:
            return format(x, "f").rstrip("0").rstrip(".")
        return f"{mant}e{'+' if e > 0 else '-'}{abs(e)}"
    return r


def dumps(v: Any) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return _num(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(dumps(x) for x in v) + "]"
    if isinstance(v, dict):
        items = sorted(v.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + dumps(x) for k, x in items) + "}"
    raise TypeError(f"not JSON: {type(v).__name__}")


def canonical(v: Any) -> bytes:
    return dumps(v).encode("utf-8")


def digest(v: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(v)).hexdigest()
