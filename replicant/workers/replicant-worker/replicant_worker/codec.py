"""Tenant-routed payload codec (XD-09-06; WS-09 secrets.md section 6.2 owns the KMS key provider).

Problem: WS-09 encrypts every payload with the job's *tenant* key, but a PayloadCodec only sees
payloads, and the workflow ID (= job_id, C-IDS) does not contain the tenant.

Solution (no contract change): the codec implements `WithSerializationContext`, so the SDK hands
it the workflow ID of every operation (`WorkflowSerializationContext.workflow_id`,
`ActivitySerializationContext.workflow_id`; verified in temporalio 1.34.0). A process-local
`TenantDirectory` maps job_id -> tenant. It is filled
  * by `decode`, from the authenticated `replicant-tenant` metadata of any payload of that job
    (every workflow task starts by decoding the start input or history; every activity task starts
    by decoding its input), and
  * explicitly by clients that start or signal a job (control-plane-api, github-integration),
    which know the tenant from the intake record.
`encode` fails closed when the tenant is unknown. The tenant is bound into AES-GCM associated
data, so a payload cannot be relabeled to another tenant.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Sequence
from typing import Protocol

from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec, SerializationContext, WithSerializationContext

ENCODING = b"binary/replicant-encrypted-v1"
SYSTEM_TENANT = "replicant-system"  # sys:* workflows (WS-09)


class KeyProvider(Protocol):
    """WS-09 implements this with KMS GenerateDataKey/Decrypt and the encryption context
    {"replicant:purpose": "temporal-payload", "replicant:tenant": <tenant>}."""

    def encrypt(self, tenant: str, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]: ...  # (key_blob, ciphertext)

    def decrypt(self, tenant: str, key_blob: bytes, ciphertext: bytes, aad: bytes) -> bytes: ...


class TenantDirectory:
    def __init__(self, capacity: int = 200_000) -> None:
        self._m: OrderedDict[str, str] = OrderedDict()
        self._cap = capacity

    @staticmethod
    def job_of(workflow_id: str) -> str:
        return workflow_id.split(":", 1)[0]  # children are <job_id>:<kind>:<n> (C-IDS)

    def register(self, workflow_id: str, tenant: str) -> None:
        k = self.job_of(workflow_id)
        prev = self._m.get(k)
        if prev is not None and prev != tenant:
            raise PermissionError(f"tenant conflict for {k}: {prev} vs {tenant}")
        self._m[k] = tenant
        self._m.move_to_end(k)
        while len(self._m) > self._cap:
            self._m.popitem(last=False)

    def lookup(self, workflow_id: str) -> str | None:
        if workflow_id.startswith("sys:"):
            return SYSTEM_TENANT
        return self._m.get(self.job_of(workflow_id))


class TenantContextCodec(PayloadCodec, WithSerializationContext):
    def __init__(self, keys: KeyProvider, directory: TenantDirectory, workflow_id: str | None = None) -> None:
        self._keys = keys
        self._dir = directory
        self._wid = workflow_id

    def with_context(self, context: SerializationContext) -> "TenantContextCodec":
        return TenantContextCodec(self._keys, self._dir, getattr(context, "workflow_id", None))

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        tenant = self._dir.lookup(self._wid) if self._wid else None
        if tenant is None:
            _unknown_tenant_counter().add(1)  # replicant_codec_unknown_tenant_total (XD-FIX-R1-195)
            raise PermissionError(f"no tenant known for workflow {self._wid!r}; refusing to encode in clear")
        out = []
        for p in payloads:
            key_blob, ct = self._keys.encrypt(tenant, p.SerializeToString(), tenant.encode())
            out.append(Payload(metadata={"encoding": ENCODING, "replicant-tenant": tenant.encode(),
                                         "replicant-wrapped-key": key_blob}, data=ct))
        return out

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out = []
        for p in payloads:
            if p.metadata.get("encoding") != ENCODING:
                out.append(p)  # never encrypted (e.g. written before encryption was enabled)
                continue
            tenant = p.metadata["replicant-tenant"].decode()
            plain = self._keys.decrypt(tenant, p.metadata["replicant-wrapped-key"], p.data, tenant.encode())
            if self._wid:
                self._dir.register(self._wid, tenant)  # AES-GCM authenticated the tenant label
            out.append(Payload.FromString(plain))
        return out


_COUNTER = None


def _unknown_tenant_counter():
    global _COUNTER
    if _COUNTER is None:
        from temporalio.runtime import Runtime
        _COUNTER = Runtime.default().metric_meter.create_counter(
            "replicant_codec_unknown_tenant_total", "encode refused: no tenant known for the workflow")
    return _COUNTER


def aesgcm_encrypt_with_dek(dek: bytes, nonce: bytes, tenant: str, plaintext: bytes) -> bytes:
    """The wire format WS-02's Go encoder must reproduce (XD-FIX-R1-044): data = nonce(12) || ciphertext || tag(16),
    AES-256-GCM, AAD = UTF-8 tenant_id. Used to generate codec/testvectors.json."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return nonce + AESGCM(dek).encrypt(nonce, plaintext, tenant.encode())


class LocalKeyProvider:
    """Local profile and tests: one static AES-256-GCM key per tenant (no KMS). Never used in prod."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self._aes = AESGCM
        self._keys: dict[str, bytes] = {}

    def _key(self, tenant: str) -> bytes:
        return self._keys.setdefault(tenant, self._aes.generate_key(bit_length=256))

    def encrypt(self, tenant: str, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
        nonce = os.urandom(12)
        return b"local-v1", nonce + self._aes(self._key(tenant)).encrypt(nonce, plaintext, aad)

    def decrypt(self, tenant: str, key_blob: bytes, ciphertext: bytes, aad: bytes) -> bytes:
        return self._aes(self._key(tenant)).decrypt(ciphertext[:12], ciphertext[12:], aad)
