"""Projects and team-owned configuration (v0 CONFIG, plan §1-§2; INTERFACES-LOG CONFIG-01).

Two kinds of configuration, both versioned rows in `tenancy.project_config_versions`:
* live   (triggers, approvals): read at use time (github-integration repo settings are pushed on write; approver
  groups and the Slack channel are read by gates/notify through `LiveConfig`, TTL a few seconds);
* pinned (gates, risk, workflows): compiled into a derived Release Bundle at intake (`derived.py`) and pinned on the
  job, so a change applies to new jobs only and never needs a rebuild.

Effective config = DEFAULTS <- project-level version <- repo override (section-wise merge; `workflows` per type).
"""
from __future__ import annotations

import copy
import json
import time
from typing import Any

from sqlalchemy import text

from control_plane_api import contracts
from control_plane_api.db import OPERATOR_SCOPE

LIVE_SECTIONS = ("triggers", "approvals")
PINNED_SECTIONS = ("gates", "risk", "workflows")
WORKFLOW_TYPES = ("bugfix", "feature", "api_change", "docs", "hotfix", "maintenance", "pr_repair", "qa_only")
PROJECT_ROLES = ("viewer", "engineer", "approver", "project_admin")

DEFAULTS: dict[str, Any] = {
    # github-integration's register_repo defaults (consumer.py) and the v0 local bundle's gate/risk behaviour
    "triggers": {"enabled": True, "label": "replicant", "modes": ["label", "command"],
                 "allowed_workflow_types": ["auto", "bugfix", "feature", "docs", "api_change"],
                 "default_priority": "normal", "min_trigger_role": "engineer"},
    "approvals": {"approver_groups": [], "slack_channel": None},
    "gates": {"plan": "off", "publish": "always", "requester_may_decide": True},
    "risk": {"large_change_files": 20, "sensitive_paths": [], "forbidden_paths": []},
    "workflows": {},
}


def merge(*layers: dict[str, Any] | None) -> dict[str, Any]:
    out = copy.deepcopy(DEFAULTS)
    for layer in layers:
        for sec, val in (layer or {}).items():
            if sec == "workflows":
                for wt, wc in (val or {}).items():
                    cur = out["workflows"].setdefault(wt, {})
                    for k, v in (wc or {}).items():
                        if isinstance(v, dict) and isinstance(cur.get(k), dict):
                            cur[k] = {**cur[k], **v}
                        else:
                            cur[k] = copy.deepcopy(v)
            elif isinstance(val, dict):
                out.setdefault(sec, {}).update(copy.deepcopy(val))
    # drop empty workflow entries (a cleared override equals the default)
    out["workflows"] = {wt: wc for wt, wc in out["workflows"].items()
                        if wc.get("graph_yaml") or wc.get("params") or wc.get("max_visits")}
    return out


def pinned_part(eff: dict[str, Any]) -> dict[str, Any]:
    return {k: eff[k] for k in PINNED_SECTIONS}


def is_default_pinned(eff: dict[str, Any]) -> bool:
    return pinned_part(eff) == pinned_part(DEFAULTS)


def schema_errors(cfg: Any) -> list[dict[str, str]]:
    return contracts.errors("project-config", cfg)


def _flat(d: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(d, dict) and not d:
        return {}
    if isinstance(d, dict) and not (prefix.count(".") >= 2 and prefix.startswith("workflows.")
                                          and prefix.split(".")[2] in ("params", "max_visits")):
        out: dict[str, Any] = {}
        for k, v in d.items():
            out.update(_flat(v, f"{prefix}.{k}" if prefix else k))
        return out
    return {prefix: d}


def changes(old_eff: dict[str, Any], new_eff: dict[str, Any]) -> list[dict[str, Any]]:
    """What a config change does, each item labelled `live` (applies immediately) or `new_jobs`."""
    a, b = _flat(old_eff), _flat(new_eff)
    out = []
    for path in sorted(set(a) | set(b)):
        if a.get(path) != b.get(path):
            sec = path.split(".", 1)[0]
            out.append({"path": path, "from": a.get(path), "to": b.get(path),
                        "applies": "live" if sec in LIVE_SECTIONS else "new_jobs"})
    return out


def permissions_needed(chs: list[dict[str, Any]]) -> set[str]:
    need = set()
    for c in chs:
        need.add("tenant:ops:edit" if c["applies"] == "live" else "tenant:policy:edit")
    return need


# ------------------------------------------------------------------------------------------------ data access
async def project_row(db: Any, project_id: str) -> Any:
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        return (await conn.execute(text(
            "SELECT project_id, tenant_id, name, owners_group, created_at, created_by FROM tenancy.projects "
            "WHERE project_id = :p AND archived_at IS NULL"), {"p": project_id})).first()


async def project_repos(db: Any, project_id: str) -> list[dict[str, Any]]:
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        rows = (await conn.execute(text(
            "SELECT repo_id, provider, host, full_name, added_at FROM tenancy.project_repos WHERE project_id = :p "
            "ORDER BY full_name"), {"p": project_id})).all()
    return [{"repo_id": r.repo_id, "provider": r.provider, "host": r.host, "full_name": r.full_name,
             "added_at": r.added_at.isoformat().replace("+00:00", "Z")} for r in rows]


async def list_projects(db: Any, tenant_id: str | None = None) -> list[Any]:
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        q = ("SELECT p.project_id, p.tenant_id, p.name, p.owners_group, (SELECT count(*) FROM tenancy.project_repos r "
             "WHERE r.project_id = p.project_id) AS repos FROM tenancy.projects p WHERE p.archived_at IS NULL")
        if tenant_id:
            return (await conn.execute(text(q + " AND p.tenant_id = :t ORDER BY p.name"), {"t": tenant_id})).all()
        return (await conn.execute(text(q + " ORDER BY p.tenant_id, p.name"))).all()


async def config_versions(db: Any, project_id: str, scope_key: str = "", limit: int = 100) -> list[dict[str, Any]]:
    async with db.tx(scope=OPERATOR_SCOPE) as conn:
        rows = (await conn.execute(text(
            "SELECT version, config, is_current, actor, reason, rolled_back_from, created_at FROM "
            "tenancy.project_config_versions WHERE project_id = :p AND scope_key = :s ORDER BY version DESC LIMIT :n"),
            {"p": project_id, "s": scope_key, "n": limit})).all()
    return [{"version": r.version, "config": r.config, "is_current": r.is_current, "actor": r.actor,
             "reason": r.reason, "rolled_back_from": r.rolled_back_from,
             "created_at": r.created_at.isoformat().replace("+00:00", "Z")} for r in rows]


async def current_configs(conn: Any, project_id: str) -> dict[str, tuple[int, dict[str, Any]]]:
    """scope_key -> (version, config) of the current rows."""
    rows = (await conn.execute(text(
        "SELECT scope_key, version, config FROM tenancy.project_config_versions WHERE project_id = :p AND is_current"),
        {"p": project_id})).all()
    return {r.scope_key: (r.version, r.config) for r in rows}


async def effective_for_repo_conn(conn: Any, repo_id: str) -> tuple[str, str, dict[str, Any]] | None:
    """(project_id, tenant_id, effective config) of a repo, read in `conn` (fresh, e.g. at intake)."""
    r = (await conn.execute(text("SELECT project_id, tenant_id FROM tenancy.project_repos WHERE repo_id = :r"),
                            {"r": str(repo_id)})).first()
    if r is None:
        return None
    cur = await current_configs(conn, r.project_id)
    return r.project_id, r.tenant_id, merge(cur.get("", (0, {}))[1], cur.get(str(repo_id), (0, {}))[1])


async def write_config(db: Any, project_id: str, tenant_id: str, scope_key: str, config: dict[str, Any],
                       if_match: int, actor: str, reason: str, rolled_back_from: int | None = None) -> int:
    """New current version; `if_match` is the current version (0 = none yet). Raises VersionConflict."""
    async with db.tx(role="svc_cpa_tenancy", tenant=tenant_id) as conn:
        cur = (await conn.execute(text(
            "SELECT version FROM tenancy.project_config_versions WHERE project_id = :p AND scope_key = :s AND "
            "is_current FOR UPDATE"), {"p": project_id, "s": scope_key})).scalar()
        if int(cur or 0) != int(if_match):
            raise VersionConflict(int(cur or 0))
        top = (await conn.execute(text(
            "SELECT coalesce(max(version), 0) FROM tenancy.project_config_versions WHERE project_id = :p AND "
            "scope_key = :s"), {"p": project_id, "s": scope_key})).scalar_one()
        v = int(top) + 1
        await conn.execute(text("UPDATE tenancy.project_config_versions SET is_current = false WHERE project_id = :p "
                                "AND scope_key = :s AND is_current"), {"p": project_id, "s": scope_key})
        await conn.execute(text(
            "INSERT INTO tenancy.project_config_versions (project_id, scope_key, tenant_id, version, config, is_current, "
            "actor, reason, rolled_back_from) VALUES (:p, :s, :t, :v, CAST(:c AS jsonb), true, :a, :r, :rb)"),
            {"p": project_id, "s": scope_key, "t": tenant_id, "v": v, "c": json.dumps(config), "a": actor,
             "r": reason[:500], "rb": rolled_back_from})
        from control_plane_api import audit
        await audit.emit(conn, db, {"event_type": "admin.project_config_changed", "outcome": "success",
                                    "tenant_id": tenant_id, "actor_principal_id": actor, "target_kind": "project",
                                    "target_id": project_id,
                                    "details": {"reason": reason[:200], "scope": scope_key or "project", "version": v,
                                                "rolled_back_from": rolled_back_from}})
    return v


class VersionConflict(Exception):
    def __init__(self, current: int) -> None:
        super().__init__(f"current version is {current}")
        self.current = current


# ------------------------------------------------------------------------------------------------ live reads (cached)
class LiveConfig:
    """Effective config by repo for use-time reads (approver groups, Slack channel). TTL keeps the DB load flat."""

    def __init__(self, db: Any, ttl_s: float = 3.0) -> None:
        self.db = db
        self.ttl_s = ttl_s
        self._c: dict[str, tuple[float, Any]] = {}

    async def for_repo(self, repo_id: str | None) -> tuple[str, str, dict[str, Any]] | None:
        if not repo_id:
            return None
        hit = self._c.get(str(repo_id))
        if hit and time.monotonic() - hit[0] < self.ttl_s:
            return hit[1]
        try:
            if not await self.db.has_relation("tenancy.project_repos"):
                return None
            async with self.db.tx(scope=OPERATOR_SCOPE) as conn:
                v = await effective_for_repo_conn(conn, str(repo_id))
        except Exception:  # noqa: BLE001 - fall back to tenant defaults
            v = None
        self._c[str(repo_id)] = (time.monotonic(), v)
        return v

    def invalidate(self) -> None:
        self._c.clear()


def approver_ok(principal: Any, eff: dict[str, Any] | None) -> bool:
    """Live approver groups: empty = anyone with gate:decide on the project."""
    groups = ((eff or {}).get("approvals") or {}).get("approver_groups") or []
    return not groups or bool(set(groups) & set(principal.groups))
