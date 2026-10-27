"""Run-grant signer (C-RUNNER 3.0.0 RunGrantClaims, CCR-09-02). Locally a file key (ES256, P-256, PKCS#8 PEM,
kid rg-local-1, issuer replicant-local-core); production uses KMS key class `run-grant` (not in v0)."""
from __future__ import annotations

import base64
import json
import time
from functools import lru_cache
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from replicant_worker import ids
from replicant_worker.activities import jcs
from replicant_worker.activities.config import settings


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class GrantSigner:
    def __init__(self, private_key: ec.EllipticCurvePrivateKey, kid: str, issuer: str) -> None:
        if not isinstance(private_key.curve, ec.SECP256R1):
            raise ValueError("run-grant key must be P-256")
        self._key, self.kid, self.issuer = private_key, kid, issuer

    @classmethod
    def from_pem(cls, pem: bytes, kid: str, issuer: str) -> "GrantSigner":
        k = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(k, ec.EllipticCurvePrivateKey):
            raise ValueError("run-grant key must be an EC key")
        return cls(k, kid, issuer)

    def public_jwk(self) -> dict[str, Any]:
        n = self._key.public_key().public_numbers()
        return {"kty": "EC", "crv": "P-256", "kid": self.kid, "use": "sig", "alg": "ES256",
                "x": b64u(n.x.to_bytes(32, "big")), "y": b64u(n.y.to_bytes(32, "big"))}

    def sign(self, claims: dict[str, Any]) -> str:
        header = {"alg": "ES256", "typ": "replicant-run-grant+jwt", "kid": self.kid}
        signing_input = f"{b64u(json.dumps(header, separators=(',', ':')).encode())}." \
                        f"{b64u(json.dumps(claims, separators=(',', ':')).encode())}"
        der = self._key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return f"{signing_input}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


@lru_cache(maxsize=1)
def signer() -> GrantSigner:
    s = settings()
    if not s.grant_key_file:
        raise RuntimeError("REPLICANT_RUN_GRANT_KEY_FILE is not set")
    with open(s.grant_key_file, "rb") as f:
        return GrantSigner.from_pem(f.read(), s.grant_kid, s.grant_issuer)


def spec_digest(run_spec: dict[str, Any]) -> str:
    """sha256(JCS(RunSpec without run_grant and trace_context)) (C-RUNNER 2.4.1)."""
    return jcs.digest({k: v for k, v in run_spec.items() if k not in ("run_grant", "trace_context")})


def grant_claims(*, run_spec: dict[str, Any], run_id: str, grant: dict[str, Any], issuer: str,
                 now: int | None = None) -> dict[str, Any]:
    iat = int(now if now is not None else time.time())
    lifetime = int(grant.get("lifetime_seconds") or 0) or (
        sum(int(run_spec["timeouts"][k]) for k in ("startup_seconds", "active_deadline_seconds", "finalize_seconds")) + 300)
    return {"iss": issuer, "typ": "replicant-run-grant+jwt", "jti": _jti(), "iat": iat, "exp": iat + lifetime, "tenant_id": run_spec["tenant_id"], "job_id": run_spec["job_id"],
            "run_id": run_id, "role": run_spec["role"], "repo_ids": list(grant.get("repo_ids") or [])[:5],
            "audiences": sorted(set(grant.get("audiences") or [])), "scopes": sorted(set(grant.get("scopes") or [])),
            "budget_usd": float(grant.get("budget_usd", run_spec["limits"]["max_cost_usd"])),
            "acting_for": grant["acting_for"], "spec_digest": spec_digest(run_spec)}


def _jti() -> str:
    import os
    return ids.derived("rgr", "jti", os.urandom(16).hex())
