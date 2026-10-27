"""C-IDS helpers, from PLATFORM's `replicant_ids` (the C-IDS 2.0.0 reference implementation)."""
from __future__ import annotations

import hashlib

from replicant_ids import b32_130, derived, jcs, release_bundle_id, ulid  # noqa: F401


def dev_user_principal(login: str) -> str:
    """Static M1 dev principal (no identity-service): derived("usr", "replicant-dev-user", login). The same rule is used
    by github-integration (INTERFACES-LOG CONTROL-02)."""
    return derived("usr", "replicant-dev-user", login)


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


__all__ = ["b32_130", "derived", "dev_user_principal", "jcs", "release_bundle_id", "sha256_digest", "ulid"]
