"""Webhook signature checks over the raw body, before any parsing (github-app.md §3.2). Constant-time compare."""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable


def _secrets(secrets: bytes | Iterable[bytes]) -> list[bytes]:
    return [secrets] if isinstance(secrets, (bytes, bytearray)) else [bytes(s) for s in secrets]


def verify_github_signature(header: str | None, body: bytes, secrets: bytes | Iterable[bytes]) -> bool:
    """`X-Hub-Signature-256: sha256=<hex>`; tries current then previous secret (rotation window)."""
    if not header or not header.startswith("sha256="):
        return False
    given = header.removeprefix("sha256=").strip().lower()
    return any(hmac.compare_digest(hmac.new(s, body, hashlib.sha256).hexdigest(), given) for s in _secrets(secrets) if s)


def verify_gitea_signature(header: str | None, body: bytes, secrets: bytes | Iterable[bytes]) -> bool:
    """`X-Gitea-Signature: <hex>` (HMAC-SHA256 of the raw body; ADR-11-002). Gitea also sends the GitHub-compatible
    `X-Hub-Signature-256: sha256=<hex>`; callers may pass either header value (the prefix is tolerated)."""
    if not header:
        return False
    given = header.strip().lower().removeprefix("sha256=")
    if len(given) != 64:
        return False
    return any(hmac.compare_digest(hmac.new(s, body, hashlib.sha256).hexdigest(), given) for s in _secrets(secrets) if s)
