"""Authentication and the per-route permission dependency (architecture.md §2.2, identity.md §8).

M1 authentication sources, in order:
1. Internal caller tokens (`REPLICANT_INTERNAL_CALLER_TOKENS_FILE`, REPLICANT_ENV=local only; XD-W3-R1V-067): static
   bearer token -> service principal (`svc_replicant-gateway-shim`, `svc_github-integration`, ...), which holds
   permission `service:<name>`.
2. Authenticators registered by the dev-only package `replicant_dev_auth` (dev image target only, identity.md §9.3 L-1):
   the dev seed token (`svc_dev-seeder`, `dev:seed`) and the static dev users (`X-Replicant-Dev-User`).
Prod OIDC/BFF sessions and device-grant JWTs are not implemented in M1 (no Dex/Okta); every other request is 401.
"""
from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from typing import Any

import yaml
from fastapi import Depends, Request

from control_plane_api.authz import AuthorizeRequest, Evaluator, Principal, PrincipalLookup
from control_plane_api.config import Settings
from control_plane_api.problems import ProblemError

log = logging.getLogger(__name__)

Authenticator = Callable[[Request], Principal | None]


def parse_token_map(text_: str) -> dict[str, str]:
    """`<token> <principal>` lines (spec XD-W3-R1V-067) or a YAML map `token: principal` (PLATFORM-02)."""
    try:
        data = yaml.safe_load(text_)
    except yaml.YAMLError:
        data = None
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items() if v is not None}
    out: dict[str, str] = {}
    for ln in text_.splitlines():
        parts = ln.split()
        if len(parts) >= 2 and not parts[0].startswith("#"):
            out[parts[0].strip("'\"")] = parts[1].strip("'\"")
    return out


def service_principal(principal_id: str, *extra_perms: str, method: str = "static_token") -> Principal:
    name = principal_id.removeprefix("svc_")
    return Principal(principal_id=principal_id, kind="service", login=name,
                     permissions=frozenset({f"service:{name}", *extra_perms}), auth_method=method)


class AuthContext:
    """Holds the principal directory, the evaluator and the authenticator chain."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.directory = PrincipalLookup()
        self.evaluator = Evaluator(self.directory)
        self.authenticators: list[Authenticator] = []
        self._internal: dict[str, Principal] = {}
        if settings.internal_caller_tokens_file:
            if not settings.is_local:
                raise SystemExit("REPLICANT_INTERNAL_CALLER_TOKENS_FILE is honoured only when REPLICANT_ENV=local")
            try:
                with open(settings.internal_caller_tokens_file, encoding="utf-8") as f:
                    for tok, pid in parse_token_map(f.read()).items():
                        p = service_principal(pid)
                        self._internal[tok] = p
                        self.directory.add(p)
            except FileNotFoundError:
                log.warning("internal caller tokens file not found; no internal callers")
        self.authenticators.append(self._bearer_internal)

    def _bearer_internal(self, request: Request) -> Principal | None:
        tok = bearer(request)
        if not tok:
            return None
        for known, p in self._internal.items():
            if hmac.compare_digest(known, tok):
                return p
        return None

    def authenticate(self, request: Request) -> Principal | None:
        for a in self.authenticators:
            p = a(request)
            if p is not None:
                return p
        return None


def bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    if h[:7].lower() == "bearer ":
        return h[7:].strip() or None
    return None


def auth_ctx(request: Request) -> AuthContext:
    return request.app.state.auth


async def current_principal(request: Request) -> Principal:
    ctx: AuthContext = request.app.state.auth
    p = ctx.authenticate(request)
    if p is None:
        raise ProblemError(401, "unauthenticated", headers={"WWW-Authenticate": "Bearer"})
    request.state.principal = p
    return p


def check(ctx: AuthContext, principal: Principal, permission: str, *, tenant_id: str | None = None,
          repo_ids: tuple[str, ...] = (), job_id: str | None = None) -> bool:
    """`permission` may list alternatives `a|b` (OpenAPI x-replicant-permission)."""
    for alt in permission.split("|"):
        if alt in ("public", "authenticated"):
            return True
        if ctx.evaluator.authorize(AuthorizeRequest(principal.principal_id, alt, tenant_id, repo_ids, job_id)).allowed:
            return True
    return False


def require(permission: str) -> Any:
    """Route dependency. Tenant/repo scoping for job- and tenant-scoped routes happens in the handler (it needs the
    resource), through `check()`; this dependency enforces that the principal holds the permission somewhere."""

    async def dep(request: Request, principal: Principal = Depends(current_principal)) -> Principal:
        ctx: AuthContext = request.app.state.auth
        if not holds_somewhere(ctx, principal, permission):
            raise ProblemError(404 if permission == "job:read" else 403, "missing_permission")
        return principal

    dep.__replicant_permission__ = permission  # type: ignore[attr-defined]
    return Depends(dep)


def holds_somewhere(ctx: AuthContext, principal: Principal, permission: str) -> bool:
    from control_plane_api.authz import PLATFORM_ONLY
    for alt in permission.split("|"):
        if alt in ("public", "authenticated") or alt in principal.permissions:
            return True
        if alt in PLATFORM_ONLY or alt.startswith("service:"):
            if check(ctx, principal, alt):
                return True
        elif principal.tenants_with(alt) != set():
            return True
    return False
