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

import logging
from collections.abc import Callable
from typing import Any

import yaml
from fastapi import Depends, Request
from replicant_telemetry.common import bearer_token, token_match

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
        from control_plane_api.rbac import RoleResolver
        self.rbac = RoleResolver(None)           # attach_db() wires the DB snapshot (app factory)
        self.evaluator = Evaluator(self.directory, self.rbac)
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

    def attach_db(self, db: Any) -> None:
        self.rbac.db = db

    def effective(self, principal: Principal) -> Principal:
        return self.rbac.effective(principal)

    def _bearer_internal(self, request: Request) -> Principal | None:
        return token_match(bearer(request), self._internal)

    def authenticate(self, request: Request) -> Principal | None:
        for a in self.authenticators:
            p = a(request)
            if p is not None:
                return p
        return None


def bearer(request: Request) -> str | None:
    return bearer_token(request) or None


def auth_ctx(request: Request) -> AuthContext:
    return request.app.state.auth


async def current_principal(request: Request) -> Principal:
    ctx: AuthContext = request.app.state.auth
    p = ctx.authenticate(request)
    if p is None:
        raise ProblemError(401, "unauthenticated", headers={"WWW-Authenticate": "Bearer"})
    if p.is_user:
        await ctx.rbac.refresh()
        p = ctx.rbac.effective(p)
    request.state.principal = p
    return p


def check(ctx: AuthContext, principal: Principal, permission: str, *, tenant_id: str | None = None,
          repo_ids: tuple[str, ...] = (), job_id: str | None = None, project_id: str | None = None) -> bool:
    """`permission` may list alternatives `a|b` (OpenAPI x-replicant-permission)."""
    for alt in permission.split("|"):
        if alt in ("public", "authenticated"):
            return True
        if alt in principal.permissions:
            return True
        if ctx.evaluator.authorize(AuthorizeRequest(principal.principal_id, alt, tenant_id, repo_ids, job_id,
                                                    project_id=project_id)).allowed:
            return True
    return False


def require(permission: str) -> Any:
    """Route dependency. Tenant/repo scoping for job- and tenant-scoped routes happens in the handler (it needs the
    resource), through `check()`; this dependency enforces that the principal holds the permission somewhere."""

    async def dep(request: Request, principal: Principal = Depends(current_principal)) -> Principal:
        ctx: AuthContext = request.app.state.auth
        if not holds_somewhere(ctx, principal, permission):
            # read routes answer 404 like a resource the caller cannot see (no existence leak, v0 SEC-D2)
            raise ProblemError(404 if "job:read" in permission.split("|") else 403, "missing_permission")
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


def scope_filter(ctx: AuthContext, principal: Principal, permission: str, alias: str,
                 params: dict[str, Any]) -> str | None:
    """SQL condition limiting `alias` rows (tenant_id, repo_ids) to what `principal` may see with `permission`:
    whole tenants granted by a platform or tenant role, plus the repos of projects granted by a project role.
    Returns "TRUE" for everything and None when nothing is visible (callers return an empty or 404 result)."""
    full = principal.tenants_with(permission, include_projects=False)
    if full is None:
        return "TRUE"
    projects = principal.projects_with(permission)
    repo_of = ctx.rbac.snap.project_of_repo
    repos = sorted(r for r, pid in repo_of.items() if pid in projects)
    parts = []
    if full:
        params["scope_tenants"] = sorted(full)
        parts.append(f"{alias}.tenant_id = ANY(:scope_tenants)")
    if repos:
        params["scope_repos"] = repos
        params["scope_project_tenants"] = sorted({principal.project_tenants[pid] for pid in projects
                                                  if principal.project_tenants.get(pid)})
        parts.append(f"({alias}.tenant_id = ANY(:scope_project_tenants) AND {alias}.repo_ids && "
                     f"CAST(:scope_repos AS text[]))")
    return "(" + " OR ".join(parts) + ")" if parts else None
