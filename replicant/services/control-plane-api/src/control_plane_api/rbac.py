"""DB-backed role resolver (v0 CONFIG, plan §3; DEVIATIONS CONFIG-D3).

A principal's roles come from `identity.role_bindings` (the current `identity.role_binding_versions` version) joined
with the principal's groups in `identity.memberships`. The dev header (or, later, the OIDC session) supplies the
identity only. Bindings are group -> role on the platform, a tenant or a project (`project_id`). Platform-scope
bindings of a tenant role (e.g. `approver`) apply to every tenant (`tenant_roles["*"]`).

The whole binding set is small, so it is loaded as one snapshot and refreshed every few seconds (TTL); authorization
stays synchronous (`Evaluator.authorize`) and reads the snapshot. Until a binding version exists (fresh database,
unit tests) the static directory roles stand (`loaded = False`).

Projects: `tenancy.projects` and `tenancy.project_repos` give repo -> project -> tenant; `Evaluator.authorize` uses
them to evaluate project bindings for a request's `repo_ids` (closing the `not_checked_m1` gap for project scope).
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from control_plane_api.authz import ROLE_PERMISSIONS, TENANT_LEVEL_ROLES, Principal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Binding:
    group: str
    scope: str                 # platform | tenant | project
    role: str
    tenant_id: str | None = None
    project_id: str | None = None
    repo_globs: tuple[str, ...] = ()


@dataclass
class Snapshot:
    loaded: bool = False
    version: int = 0
    bindings: list[Binding] = field(default_factory=list)
    groups_of: dict[str, frozenset[str]] = field(default_factory=dict)        # principal -> group names
    project_of_repo: dict[str, str] = field(default_factory=dict)            # repo_id -> project_id
    tenant_of_project: dict[str, str] = field(default_factory=dict)          # project_id -> tenant_id
    project_names: dict[str, str] = field(default_factory=dict)
    loaded_at: float = 0.0


class RoleResolver:
    def __init__(self, db: Any = None, ttl_s: float = 5.0) -> None:
        self.db = db
        self.ttl_s = ttl_s
        self.snap = Snapshot()
        self._lock = asyncio.Lock()
        self._cache: dict[tuple[str, int, float], Principal] = {}

    # ------------------------------------------------------------------ loading
    async def refresh(self, force: bool = False) -> Snapshot:
        if self.db is None:
            return self.snap
        if not force and time.monotonic() - self.snap.loaded_at < self.ttl_s:
            return self.snap
        async with self._lock:
            if not force and time.monotonic() - self.snap.loaded_at < self.ttl_s:
                return self.snap
            try:
                self.snap = await self._load()
                self._cache.clear()
            except Exception as e:  # noqa: BLE001 - keep the last snapshot; retry on the next request
                log.warning("role resolver refresh failed: %s", f"{type(e).__name__}: {str(e)[:200]}")
                self.snap.loaded_at = time.monotonic()
        return self.snap

    async def _load(self) -> Snapshot:
        from control_plane_api.db import OPERATOR_SCOPE
        s = Snapshot(loaded_at=time.monotonic())
        if not await self.db.has_relation("tenancy.projects"):
            return s
        async with self.db.tx(scope=OPERATOR_SCOPE) as conn:
            v = (await conn.execute(text("SELECT version FROM identity.role_binding_versions WHERE is_current"))).scalar()
            if v is not None:
                s.loaded, s.version = True, int(v)
                for r in (await conn.execute(text(
                        "SELECT group_name, scope, role, tenant_id, project_id, repo_globs FROM identity.role_bindings "
                        "WHERE version = :v ORDER BY binding_id"), {"v": v})).all():
                    s.bindings.append(Binding(r.group_name, r.scope, r.role, r.tenant_id, r.project_id,
                                              tuple(r.repo_globs or ())))
                groups: dict[str, set[str]] = {}
                for r in (await conn.execute(text(
                        "SELECT m.principal_id, g.display_name FROM identity.memberships m JOIN identity.groups g "
                        "ON g.group_id = m.group_id WHERE g.deleted_at IS NULL"))).all():
                    groups.setdefault(r.principal_id, set()).add(r.display_name)
                s.groups_of = {k: frozenset(v) for k, v in groups.items()}
            for r in (await conn.execute(text(
                    "SELECT project_id, tenant_id, name FROM tenancy.projects WHERE archived_at IS NULL"))).all():
                s.tenant_of_project[r.project_id] = r.tenant_id
                s.project_names[r.project_id] = r.name
            for r in (await conn.execute(text("SELECT repo_id, project_id FROM tenancy.project_repos"))).all():
                s.project_of_repo[r.repo_id] = r.project_id
        return s

    async def loop(self, interval_s: float = 5.0) -> None:
        while True:
            try:
                await self.refresh(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(interval_s)

    # ------------------------------------------------------------------ resolution (sync, from the snapshot)
    def project_of(self, repo_id: str) -> str | None:
        return self.snap.project_of_repo.get(str(repo_id))

    def effective(self, p: Principal) -> Principal:
        """The principal with roles from the DB bindings (users only). Static tenant roles stay in effect until the
        current binding version holds tenant- or platform-scope bindings (seed step 09-projects-rbac)."""
        s = self.snap
        if not s.loaded or not p.is_user or p.roles_source == "db":
            return p
        key = (p.principal_id, s.version, s.loaded_at)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        # Until the seed imports memberships and tenant-level bindings, keep the directory's groups and static tenant
        # roles: a first project-only access change must not switch everyone (including its author) to no roles.
        groups = s.groups_of.get(p.principal_id, frozenset()) if s.groups_of else frozenset(p.groups)
        tenant_from_db = any(b.scope in ("tenant", "platform") for b in s.bindings)
        platform: set[str] = set(p.platform_roles)
        tenant: dict[str, set[str]] = {} if tenant_from_db else {t: set(r) for t, r in p.tenant_roles.items()}
        project: dict[str, set[str]] = {}
        for b in s.bindings:
            if b.group not in groups:
                continue
            if b.scope == "platform":
                if b.role in TENANT_LEVEL_ROLES:
                    tenant.setdefault("*", set()).add(b.role)
                else:
                    platform.add(b.role)
            elif b.scope == "tenant" and b.tenant_id:
                tenant.setdefault(b.tenant_id, set()).add(b.role)
            elif b.scope == "project" and b.project_id:
                project.setdefault(b.project_id, set()).add(b.role)
        out = dataclasses.replace(
            p, groups=tuple(sorted(groups)), platform_roles=tuple(sorted(platform)),
            tenant_roles={t: tuple(sorted(r)) for t, r in tenant.items()},
            project_roles={k: tuple(sorted(r)) for k, r in project.items()},
            project_tenants={k: s.tenant_of_project.get(k, "") for k in project}, roles_source="db")
        self._cache[key] = out
        return out


def project_permissions(p: Principal, project_id: str, tenant_id: str) -> set[str]:
    """Permissions `p` holds on a project: platform roles, tenant roles of the project's tenant, the project's roles."""
    perms: set[str] = set(p.permissions)
    roles = set(p.project_roles.get(project_id, ())) | set(p.platform_roles) | set(p.roles_for(tenant_id))
    for r in roles:
        perms |= ROLE_PERMISSIONS.get(r, frozenset())
    return perms
