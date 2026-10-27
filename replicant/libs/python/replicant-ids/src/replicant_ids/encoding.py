"""§1 encoding rules: lowercase Crockford base32, 26-char bodies, ULID and derived bodies."""
from __future__ import annotations

import hashlib
import os
import threading
import time

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_DECODE = {c: i for i, c in enumerate(ALPHABET)}
_MAX_RANDOM = (1 << 80) - 1


def _encode130(n: int) -> str:
    return "".join(ALPHABET[(n >> (5 * (25 - i))) & 31] for i in range(26))


def b32_130(digest: bytes) -> str:
    """First 130 bits of digest as 26 lowercase Crockford base32 characters (normative)."""
    if len(digest) < 17:
        raise ValueError("digest must be at least 17 bytes")
    n = int.from_bytes(digest[:17], "big") >> (136 - 130)
    return _encode130(n)


def derived(prefix: str, *fields: str) -> str:
    """`<prefix>_<b32_130(sha256(fields joined by \\n))>` (normative)."""
    for f in fields:
        if not isinstance(f, str):
            raise TypeError(f"derived() fields must be str, got {type(f).__name__}")
    return f"{prefix}_{b32_130(hashlib.sha256(chr(10).join(fields).encode()).digest())}"


class UlidGenerator:
    """Monotonic within a millisecond per process (C-IDS §1): same ms -> previous random + 1."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ms = -1
        self._last_rand = 0

    def body(self, ms: int | None = None) -> str:
        with self._lock:
            now = int(time.time() * 1000) if ms is None else ms
            if now < 0 or now >= 1 << 48:
                raise ValueError("timestamp out of ULID range")
            if now <= self._last_ms:
                now = self._last_ms
                if self._last_rand >= _MAX_RANDOM:
                    raise OverflowError("ULID random component overflow within one millisecond")
                rand = self._last_rand + 1
            else:
                rand = int.from_bytes(os.urandom(10), "big")
            self._last_ms, self._last_rand = now, rand
            return _encode130((now << 80) | rand)


_GEN = UlidGenerator()


def ulid(prefix: str, ms: int | None = None) -> str:
    """Time-ordered `<prefix>_<ulid>` (monotonic within a millisecond in this process)."""
    return f"{prefix}_{_GEN.body(ms)}"


def ulid_timestamp_ms(id_or_body: str) -> int:
    """Unix-ms timestamp of a ULID-bodied ID (`job_…` or a bare 26-char body)."""
    body = id_or_body.rsplit("_", 1)[-1]
    if len(body) != 26:
        raise ValueError("not a 26-char body")
    n = 0
    for c in body:
        n = (n << 5) | _DECODE[c]
    return n >> 80
