"""LOCAL ONLY (identity.md §9.3, L-1..L-7). Installed only in the `dev` image target of control-plane-api.

* Dev seed token: `Authorization: Bearer <dev_seed_token>` -> principal `svc_dev-seeder` with permission `dev:seed`
  (scope L-5: seed status, tenants, settings, platform caps, local bundle registration and bootstrap).
* Static dev users (M1 stand-in for Dex + identity-service): `X-Replicant-Dev-User: <login>` -> `usr_` principal
  `derived("usr", "replicant-dev-user", login)` with the roles below (or `REPLICANT_DEV_PRINCIPALS_FILE`, YAML
  `{login: {groups: [...], platform_roles: [...], tenant_roles: {tenant: [roles]}}}`).
* Routes `/v1/admin/dev/**`.

Every credential here is honoured only when REPLICANT_ENV=local; `install()` refuses otherwise.
"""
from __future__ import annotations

import hmac
import logging
from typing import Any

log = logging.getLogger(__name__)

DEV_USERS: dict[str, dict[str, Any]] = {
    # The M1 operator/approver persona: platform admin plus approver on every tenant (brief: "grace").
    "grace": {"groups": ["replicant-operators", "replicant-release-managers", "replicant-platform-admins"],
              "platform_roles": ["platform_admin", "operator", "release_operator", "release_manager"],
              "tenant_roles": {"*": ["approver"]}},
    "carol": {"groups": ["replicant-admins", "replicant-operators"], "platform_roles": ["operator"], "tenant_roles": {}},
    "alice": {"groups": ["payments-engineers"], "platform_roles": [], "tenant_roles": {"payments": ["engineer"]}},
    "bob": {"groups": ["payments-engineers", "payments-approvers"], "platform_roles": [],
            "tenant_roles": {"payments": ["approver"]}},
    "dave": {"groups": ["payments-engineers"], "platform_roles": [], "tenant_roles": {"payments": ["viewer"]}},
    "erin": {"groups": ["payments-engineers", "payments-admins"], "platform_roles": [],
             "tenant_roles": {"payments": ["tenant_admin"]}},
    # CONFIG: project admin of prj_ledger only (role from the DB binding ledger-admins -> project_admin)
    "frank": {"groups": ["ledger-admins"], "platform_roles": [], "tenant_roles": {}},
}
DEV_USER_HEADER = "x-replicant-dev-user"


def install(app: Any, ctx: Any, settings: Any) -> None:
    """Called by control-plane-api's app factory when this package is importable (dev image)."""
    from control_plane_api.authz import Principal
    from control_plane_api.auth import bearer, service_principal
    from control_plane_api.config import read_secret
    from control_plane_api.ids import dev_user_principal

    if not settings.is_local:
        raise SystemExit("P1 security: replicant_dev_auth is installed but REPLICANT_ENV != local")

    users = dict(DEV_USERS)
    if settings.dev_principals_file:
        import yaml
        with open(settings.dev_principals_file, encoding="utf-8") as f:
            users.update(yaml.safe_load(f) or {})
    by_login: dict[str, Principal] = {}
    for login, u in users.items():
        p = Principal(principal_id=dev_user_principal(login), kind="user", login=login,
                      groups=tuple(u.get("groups", ())), platform_roles=tuple(u.get("platform_roles", ())),
                      tenant_roles={t: tuple(r) for t, r in (u.get("tenant_roles") or {}).items()},
                      auth_method="dev_user_header")
        by_login[login] = p
        ctx.directory.add(p)

    seed_token = read_secret(settings.dev_seed_token_file)
    seeder = service_principal("svc_dev-seeder", "dev:seed", method="dev_seed_token")
    ctx.directory.add(seeder)

    def seed_auth(request: Any) -> Any:
        tok = bearer(request)
        if seed_token and tok and hmac.compare_digest(tok, seed_token):
            return seeder
        return None

    def user_auth(request: Any) -> Any:
        login = request.headers.get(DEV_USER_HEADER)
        if not login:
            return None
        return by_login.get(login.strip().lower())

    ctx.authenticators.extend([seed_auth, user_auth])

    from replicant_dev_auth.routes import router
    app.include_router(router)
    log.warning("replicant_dev_auth installed: dev seed token %s, %d static dev users (LOCAL ONLY)",
                "configured" if seed_token else "absent", len(by_login))
