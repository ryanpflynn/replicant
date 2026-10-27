"""In-process authorization evaluator with the identity-service interface shape (identity.md §6;
identity-service.openapi.yaml `POST /internal/v1/authorize`: `AuthorizeRequest` -> `AuthzDecision`).

M1 (no identity-service, no Dex): principals and their role bindings come from a static directory (see
`principals.py`); the evaluator applies identity.md §5.2's role -> permission table. The live repository
permission check (identity.md §6.1, via the SCM) is NOT performed in M1; the evidence says so
(`repo_permissions_source: not_checked_m1`). Swapping in identity-service later means replacing `Evaluator.authorize`
with an HTTP call carrying the same request body; callers do not change.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

# identity.md §5.2
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"job:read", "cost:read"}),
    "engineer": frozenset({"job:read", "cost:read", "job:create", "job:control:own", "artifact:download"}),
    "approver": frozenset({"job:read", "cost:read", "job:create", "job:control:own", "artifact:download",
                           "gate:decide", "gate:delegate"}),
    "tenant_admin": frozenset({"job:read", "cost:read", "job:create", "job:control:own", "artifact:download",
                               "gate:decide", "gate:delegate", "job:control:any", "tenant:policy:edit",
                               "tenant:ops:edit", "release:signoff_tenant_layer", "ops:kill-switch", "identity:admin"}),
    # v0 CONFIG (plan §3): the three admin permissions on one project, plus approver rights there
    "project_admin": frozenset({"job:read", "cost:read", "job:create", "job:control:own", "artifact:download",
                                "gate:decide", "gate:delegate", "job:control:any", "tenant:policy:edit",
                                "tenant:ops:edit", "identity:admin"}),
    "operator": frozenset({"release:read", "temporal:ui", "ops:kill-switch", "job:read", "cost:read"}),
    "release_operator": frozenset({"release:read", "temporal:ui", "ops:kill-switch", "job:read", "cost:read",
                                   "release:operate", "release:pin", "eval:run"}),
    "release_manager": frozenset({"release:read", "release:signoff", "release:revoke"}),
    "security_signer": frozenset({"release:read", "release:signoff"}),
    "eval_curator": frozenset({"release:read", "eval:run", "eval:curate"}),
    "identity_admin": frozenset({"identity:admin"}),
    "platform_admin": frozenset({"admin:platform", "audit:read", "ops:kill-switch", "restore:execute"}),
    "security_responder": frozenset({"security:leak", "audit:read"}),
}
PLATFORM_ONLY = {"admin:platform", "audit:read", "restore:execute", "release:read", "release:operate",
                 "release:revoke", "release:pin", "release:signoff", "temporal:ui", "eval:run", "eval:curate",
                 "security:leak", "registry:publish", "dev:seed"}
# Roles that a binding grants on a tenant or project (a platform-scope binding of one applies to every tenant).
TENANT_LEVEL_ROLES = frozenset({"viewer", "engineer", "approver", "tenant_admin", "project_admin"})


@dataclass(frozen=True)
class Principal:
    principal_id: str
    kind: str                                  # user | service
    login: str | None = None
    groups: tuple[str, ...] = ()
    platform_roles: tuple[str, ...] = ()
    tenant_roles: dict[str, tuple[str, ...]] = field(default_factory=dict)   # tenant ("*" = every tenant) -> roles
    permissions: frozenset[str] = frozenset()  # service principals: direct permissions (service:<name>, dev:seed, ...)
    auth_method: str = "none"
    project_roles: dict[str, tuple[str, ...]] = field(default_factory=dict)   # project_id -> roles (DB bindings)
    project_tenants: dict[str, str] = field(default_factory=dict)             # project_id -> tenant_id
    roles_source: str = "static"               # static (dev directory) | db (identity.role_bindings, rbac.py)

    @property
    def is_user(self) -> bool:
        return self.kind == "user"

    def roles_for(self, tenant_id: str | None) -> set[str]:
        roles = set(self.platform_roles)
        if tenant_id is not None:
            roles |= set(self.tenant_roles.get(tenant_id, ())) | set(self.tenant_roles.get("*", ()))
        return roles

    def tenants_with(self, permission: str) -> set[str] | None:
        """Tenants on which the principal holds `permission` (on the tenant or on one of its projects); None means
        all tenants (platform binding)."""
        if any(permission in ROLE_PERMISSIONS.get(r, ()) for r in self.platform_roles):
            return None
        if any(permission in ROLE_PERMISSIONS.get(r, ()) for r in self.tenant_roles.get("*", ())):
            return None
        out = {t for t, rs in self.tenant_roles.items()
               if t != "*" and any(permission in ROLE_PERMISSIONS.get(r, ()) for r in rs)}
        out |= {self.project_tenants[pid] for pid, rs in self.project_roles.items()
                if self.project_tenants.get(pid) and any(permission in ROLE_PERMISSIONS.get(r, ()) for r in rs)}
        return out

    def project_roles_for(self, project_id: str | None) -> set[str]:
        return set(self.project_roles.get(project_id, ())) if project_id else set()


@dataclass(frozen=True)
class AuthorizeRequest:
    principal_id: str
    action: str
    tenant_id: str | None = None
    repo_ids: tuple[str, ...] = ()
    job_id: str | None = None
    freshness: str = "request"
    project_id: str | None = None


@dataclass(frozen=True)
class AuthzDecision:
    decision: str              # allow | deny
    reason: str                # ok | missing_permission | tenant_mismatch | ...
    evaluated_at: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


class Evaluator:
    def __init__(self, directory: "PrincipalLookup", resolver: Any = None) -> None:
        self._dir = directory
        self.resolver = resolver      # rbac.RoleResolver (DB bindings); None = static directory only

    def authorize(self, req: AuthorizeRequest) -> AuthzDecision:
        now = dt.datetime.now(dt.UTC).isoformat()
        p = self._dir.get(req.principal_id)
        if p is None:
            return AuthzDecision("deny", "principal_deprovisioned", now)
        if self.resolver is not None:
            p = self.resolver.effective(p)
        ev = {"groups": list(p.groups),
              "groups_source": "identity.memberships" if p.roles_source == "db" else "static_dev_directory",
              "groups_as_of": now, "repo_permissions_source": "project_bindings" if p.roles_source == "db"
              else "not_checked_m1"}
        if req.action in p.permissions:
            return AuthzDecision("allow", "ok", now, ev)
        if req.action in PLATFORM_ONLY:
            ok = any(req.action in ROLE_PERMISSIONS.get(r, ()) for r in p.platform_roles)
            return AuthzDecision("allow" if ok else "deny", "ok" if ok else "missing_permission", now, ev)
        if req.tenant_id is None:
            ok = any(req.action in ROLE_PERMISSIONS.get(r, ()) for r in p.platform_roles)
            return AuthzDecision("allow" if ok else "deny", "ok" if ok else "missing_permission", now, ev)
        ok = any(req.action in ROLE_PERMISSIONS.get(r, ()) for r in p.roles_for(req.tenant_id))
        if not ok and p.project_roles:
            # project bindings: the request's project, or every repo of the request in a project the principal
            # holds the permission on (a repo outside any project needs a tenant binding)
            projects: set[str | None] = {req.project_id} if req.project_id else set()
            if req.repo_ids and self.resolver is not None:
                projects |= {self.resolver.project_of(r) for r in req.repo_ids}
            if projects and None not in projects:
                ok = all(p.project_tenants.get(pid) == req.tenant_id and
                         any(req.action in ROLE_PERMISSIONS.get(r, ()) for r in p.project_roles_for(pid))
                         for pid in projects)
        return AuthzDecision("allow" if ok else "deny", "ok" if ok else "missing_permission", now, ev)


class PrincipalLookup:
    def __init__(self) -> None:
        self._by_id: dict[str, Principal] = {}

    def add(self, p: Principal) -> None:
        self._by_id[p.principal_id] = p

    def get(self, principal_id: str) -> Principal | None:
        return self._by_id.get(principal_id)

    def all(self) -> list[Principal]:
        return list(self._by_id.values())
