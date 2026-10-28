"""C-IDS helpers, from PLATFORM's `replicant_ids` (the C-IDS 2.0.0 reference implementation)."""
from __future__ import annotations

import hashlib

from replicant_ids import b32_130, derived, jcs, release_bundle_id, ulid  # noqa: F401
from replicant_ids import dev_principal as dev_user_principal  # static M1 dev principal (INTERFACES-LOG CONTROL-02)


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


__all__ = ["b32_130", "derived", "dev_user_principal", "jcs", "release_bundle_id", "sha256_digest", "ulid"]
