"""Callers (v0, local): static bearer tokens -> service principals (`REPLICANT_MEMORY_CALLER_TOKENS_FILE`, YAML map
`token: principal`, like control-plane-api's internal callers). Admin routes additionally need a caller principal
signed by control-plane-api (D-25 re-check): header `X-Replicant-Caller` = base64url(JSON {principal_id, login,
groups, exp}) and `X-Replicant-Caller-Sig` = hex HMAC-SHA256(bearer token, header value). memory-service verifies the
signature and re-checks the admin group itself; control-plane-api's check is not trusted alone."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import yaml
from fastapi import Request

from memory_service.config import Settings

log = logging.getLogger(__name__)


class HttpError(Exception):
    def __init__(self, status: int, code: str, detail: str = "", **extra: Any) -> None:
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail, self.extra = status, code, detail, extra


@dataclass(frozen=True)
class Caller:
    service: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class AdminPrincipal:
    principal_id: str
    login: str
    groups: tuple[str, ...]


def load_tokens(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f.read())
    except FileNotFoundError:
        log.warning("caller tokens file not found; no callers")
        return {}
    return {str(k): str(v) for k, v in (data or {}).items()} if isinstance(data, dict) else {}


class Auth:
    def __init__(self, settings: Settings, tokens: dict[str, str] | None = None) -> None:
        self.settings = settings
        self.tokens = tokens if tokens is not None else load_tokens(settings.caller_tokens_file)

    def caller(self, request: Request) -> Caller:
        h = request.headers.get("authorization", "")
        tok = h[7:].strip() if h[:7].lower() == "bearer " else ""
        for known, svc in self.tokens.items():
            if tok and hmac.compare_digest(known, tok):
                return Caller(svc, tok)
        raise HttpError(401, "unauthenticated", "bearer token required")

    def admin(self, request: Request) -> AdminPrincipal:
        c = self.caller(request)
        if c.service != "svc_control-plane-api":
            raise HttpError(403, "not_admin_caller", "admin routes are served only through control-plane-api")
        raw = request.headers.get("x-replicant-caller", "")
        sig = request.headers.get("x-replicant-caller-sig", "")
        want = sign(c.token, raw)
        if not raw or not hmac.compare_digest(want, sig):
            raise HttpError(403, "caller_signature_invalid", "signed caller principal required")
        try:
            doc = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except ValueError:
            raise HttpError(403, "caller_signature_invalid", "caller principal unreadable") from None
        if int(doc.get("exp", 0)) < time.time():
            raise HttpError(403, "caller_expired", "signed caller principal expired")
        groups = tuple(doc.get("groups") or ())
        if self.settings.admin_group not in groups:  # D-25: admin group membership, re-checked here
            raise HttpError(403, "not_memory_admin", f"caller is not in {self.settings.admin_group}")
        return AdminPrincipal(str(doc["principal_id"]), str(doc.get("login") or ""), groups)


def sign(token: str, payload: str) -> str:
    return hmac.new(token.encode(), payload.encode(), hashlib.sha256).hexdigest()


def caller_headers(token: str, principal_id: str, login: str, groups: list[str], ttl: int = 60) -> dict[str, str]:
    """Used by control-plane-api (kept here as the reference implementation for tests)."""
    raw = base64.urlsafe_b64encode(json.dumps({"principal_id": principal_id, "login": login, "groups": groups,
                                               "exp": int(time.time()) + ttl}).encode()).decode().rstrip("=")
    return {"Authorization": f"Bearer {token}", "X-Replicant-Caller": raw, "X-Replicant-Caller-Sig": sign(token, raw)}
