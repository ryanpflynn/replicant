"""KMS-backed KeyProvider for the tenant-routed Temporal payload codec (R1-214, XD-01-08, XD-09-06).

Ownership (decided in the fix round): WS-09 owns this module, including the data-key cache. WS-01's
`replicant_orchestration/codec.py` owns payload framing (metadata, AAD, tenant routing) and consumes the
`KeyProvider` protocol it defines:

    encrypt(tenant, plaintext, aad) -> (key_blob, ciphertext)
    decrypt(tenant, key_blob, ciphertext, aad) -> plaintext

Every Temporal client and worker in the core account uses `KmsKeyProvider` (control-plane-api,
github-integration, replicant-worker, publisher-worker, replicant-projector, eval-worker, and the codec
server, which uses decrypt only). The exec-side run-notifier uses the Go `EncryptOnlyKeyProvider`
in kms_key_provider.go (C-ARCH 1.4.0: GenerateDataKey only, no Decrypt).

Key resolution: alias/replicant/<env>/tenant/<tenant_id>; `sys:*` workflows use tenant `replicant-system`
(alias/replicant/<env>/tenant/replicant-system). Encryption context on every call:
{"replicant:purpose": "temporal-payload", "replicant:tenant": <tenant>}, which the tenant key policy
requires (policies/iam/kms-key-policy.tenant.json).
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PURPOSE = "temporal-payload"
SYSTEM_TENANT = "replicant-system"
NONCE_BYTES = 12


def encryption_context(tenant: str) -> dict[str, str]:
    return {"replicant:purpose": PURPOSE, "replicant:tenant": tenant}


@dataclass
class _DataKey:
    plaintext: bytearray
    wrapped: bytes
    created: float
    uses: int = 0


class DataKeyCache:
    """Per-tenant encryption data keys and an LRU of unwrapped keys for decryption.

    Limits: an encryption data key is used for at most `ttl_seconds` (300) and `max_uses` (10,000)
    payloads; decrypted data keys are cached for `ttl_seconds` keyed by sha256(tenant, wrapped blob), at most
    `max_unwrapped` entries. Evicted plaintext keys are zeroed (best effort in Python).
    """

    def __init__(self, ttl_seconds: float = 300.0, max_uses: int = 10_000, max_unwrapped: int = 4_096) -> None:
        self._ttl = ttl_seconds
        self._max_uses = max_uses
        self._max_unwrapped = max_unwrapped
        self._enc: dict[str, _DataKey] = {}
        self._dec: OrderedDict[bytes, tuple[bytearray, float]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _zero(buf: bytearray) -> None:
        for i in range(len(buf)):
            buf[i] = 0

    def encryption_key(self, tenant: str, create) -> tuple[bytes, bytes]:  # noqa: ANN001
        now = time.monotonic()
        with self._lock:
            k = self._enc.get(tenant)
            if k is None or now - k.created > self._ttl or k.uses >= self._max_uses:
                if k is not None:
                    self._zero(k.plaintext)
                plaintext, wrapped = create()
                k = _DataKey(bytearray(plaintext), wrapped, now)
                self._enc[tenant] = k
            k.uses += 1
            return bytes(k.plaintext), k.wrapped

    def decryption_key(self, tenant: str, wrapped: bytes, unwrap) -> bytes:  # noqa: ANN001
        # The tenant is part of the cache key: a cached unwrap must never satisfy a payload relabelled
        # with another tenant (KMS would have rejected that context on a cache miss).
        h = hashlib.sha256(tenant.encode() + b"\x00" + wrapped).digest()
        now = time.monotonic()
        with self._lock:
            hit = self._dec.get(h)
            if hit is not None and now - hit[1] <= self._ttl:
                self._dec.move_to_end(h)
                return bytes(hit[0])
        plaintext = bytearray(unwrap(wrapped))
        with self._lock:
            self._dec[h] = (plaintext, now)
            while len(self._dec) > self._max_unwrapped:
                _, (old, _) = self._dec.popitem(last=False)
                self._zero(old)
        return bytes(plaintext)


class KmsKeyProvider:
    """Implements WS-01's KeyProvider protocol with AWS KMS envelope encryption (AES-256-GCM)."""

    def __init__(self, env: str, kms_client, cache: DataKeyCache | None = None, decrypt_only: bool = False) -> None:  # noqa: ANN001
        self._env = env
        self._kms = kms_client
        self._cache = cache or DataKeyCache()
        self._decrypt_only = decrypt_only

    def _alias(self, tenant: str) -> str:
        return f"alias/replicant/{self._env}/tenant/{tenant}"

    def encrypt(self, tenant: str, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
        if self._decrypt_only:
            raise PermissionError("this process may only decode (codec server)")

        def create() -> tuple[bytes, bytes]:
            r = self._kms.generate_data_key(
                KeyId=self._alias(tenant), KeySpec="AES_256", EncryptionContext=encryption_context(tenant)
            )
            return r["Plaintext"], r["CiphertextBlob"]

        key, wrapped = self._cache.encryption_key(tenant, create)
        nonce = os.urandom(NONCE_BYTES)
        return wrapped, nonce + AESGCM(key).encrypt(nonce, plaintext, aad)

    def decrypt(self, tenant: str, key_blob: bytes, ciphertext: bytes, aad: bytes) -> bytes:
        def unwrap(blob: bytes) -> bytes:
            # KMS rejects the call unless the blob was created under this tenant's context, so a payload
            # relabelled with another tenant fails here (fail closed).
            return self._kms.decrypt(CiphertextBlob=blob, EncryptionContext=encryption_context(tenant))["Plaintext"]

        key = self._cache.decryption_key(tenant, key_blob, unwrap)
        return AESGCM(key).decrypt(ciphertext[:NONCE_BYTES], ciphertext[NONCE_BYTES:], aad)
