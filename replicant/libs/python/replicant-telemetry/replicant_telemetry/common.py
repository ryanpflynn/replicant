"""Small helpers every Python service shares: secret files, ISO timestamps and static bearer tokens.

Pure stdlib. They live in replicant-telemetry because every service and worker already depends on it.
"""
from __future__ import annotations

import datetime as dt
import hmac
from collections.abc import Iterable, Mapping
from typing import Any, TypeVar

T = TypeVar("T")


def read_secret(path: str | None) -> str | None:
    """The stripped content of a secret file; None when the path is unset, unreadable or the file is empty.
    Never logs the value."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def iso(v: Any, *, utc: bool = False, timespec: str = "auto") -> Any:
    """`2026-10-27T12:00:00Z` for a datetime (or date); any other value is returned unchanged (None stays None).
    `utc=True` converts to UTC first (a naive datetime is taken as UTC)."""
    if not hasattr(v, "isoformat"):
        return v
    if isinstance(v, dt.datetime):
        if utc:
            v = (v if v.tzinfo else v.replace(tzinfo=dt.UTC)).astimezone(dt.UTC)
        return v.isoformat(timespec=timespec).replace("+00:00", "Z")
    return v.isoformat().replace("+00:00", "Z")


def bearer_token(request: Any) -> str:
    """The token of an `Authorization: Bearer <t>` header ("" when absent). `request` has `.headers`."""
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def token_ok(tok: str | None, secrets: str | None | Iterable[str | None]) -> bool:
    """Constant-time check of `tok` against one secret or several (unset secrets never match)."""
    if not tok:
        return False
    cands = [secrets] if secrets is None or isinstance(secrets, str) else list(secrets)
    return any(bool(s) and _same(s, tok) for s in cands)  # type: ignore[arg-type]


def token_match(tok: str | None, known: Mapping[str, T]) -> T | None:
    """The value of the `known` token (constant-time compare) equal to `tok`, else None."""
    if not tok:
        return None
    for k, v in known.items():
        if _same(k, tok):
            return v
    return None


def bearer_ok(request: Any, secrets: str | None | Iterable[str | None]) -> bool:
    """True when the request's bearer token equals one of `secrets`."""
    return token_ok(bearer_token(request), secrets)


__all__ = ["bearer_ok", "bearer_token", "iso", "read_secret", "token_match", "token_ok"]
