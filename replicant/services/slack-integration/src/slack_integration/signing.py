"""Slack request verification (approvals.md §4.2): `v0=` + hex HMAC-SHA256(secret, `v0:{ts}:{raw body}`), timestamp
within 300 s, constant-time compare, any of the configured secrets (rotation)."""
from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterable, Mapping

MAX_SKEW_S = 300


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def verify(secrets: Iterable[str], headers: Mapping[str, str], body: bytes, now: float | None = None) -> bool:
    ts = (headers.get("x-slack-request-timestamp") or "").strip()
    sig = (headers.get("x-slack-signature") or "").strip()
    if not ts or not sig or not ts.isdigit():
        return False
    now = time.time() if now is None else now
    if abs(now - int(ts)) > MAX_SKEW_S:
        return False
    ok = False
    for secret in secrets:
        # no early exit: every configured secret is compared
        ok |= hmac.compare_digest(compute_signature(secret, ts, body).encode(), sig.encode())
    return ok
