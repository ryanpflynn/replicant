"""github-token-broker (WS-07 github-app.md §9, ADR-011 as amended by CCR-09-06): the sole holder of the SCM bot
credential. It mints short-lived, least-privilege tokens for allow-listed callers and audits every mint and refusal.

M1 backend: Gitea (parity.md P-12). The broker holds the `replicant-bot` password (`REPLICANT_BROKER_GITEA_BOT_PASSWORD_FILE`)
and mints Gitea access tokens with the scopes mapped from the requested permissions
(`config/github-token-broker/broker-callers.yaml` `providers.gitea.scope_map`). Gitea tokens are not per-repository
and have no expiry, so the broker deletes each token after `REPLICANT_BROKER_GITEA_TOKEN_TTL` (reaper every 60 s, and on
startup for tokens left by an earlier process). Tokens are cached per (caller, installation, repos, permissions) until
10 minutes before expiry (§9.2). Token values are never logged.

Caller authentication in M1: static bearer tokens (`REPLICANT_BROKER_CALLER_TOKENS_FILE`, YAML `token: caller` or lines
`<token> <caller>`; REPLICANT_ENV=local only). Prod: projected ServiceAccount tokens, audience github-token-broker.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from replicant_scm.gitea import GiteaClient, GiteaError
from replicant_telemetry.common import bearer_token, read_secret, token_match

log = logging.getLogger("github_token_broker")

FORBIDDEN = {"workflows", "administration", "secrets", "actions"}         # CCR-09-06 global rule
KNOWN = {"contents", "metadata", "issues", "pull_requests", "checks", "members"}
TOKEN_PREFIX = "replicant-broker-"
LEVEL = {"read": 1, "write": 2}
DEFAULT_READ_SCOPES = {"contents": ["read:repository"], "issues": ["read:issue"], "pull_requests": ["read:repository",
                       "read:issue"], "checks": ["read:repository"], "metadata": ["read:repository"],
                       "members": ["read:organization"]}


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def parse_token_map(text_: str) -> dict[str, str]:
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


def parse_ttl(v: str | None) -> int:
    v = (v or "3600s").strip()
    mult = {"s": 1, "m": 60, "h": 3600}.get(v[-1], None)
    return int(v[:-1]) * mult if mult else int(v)


@dataclass
class Policy:
    callers: dict[str, dict[str, Any]]
    scope_map: dict[str, list[str]]

    @classmethod
    def load(cls, path: str | None) -> "Policy":
        data: dict[str, Any] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        callers = data.get("callers") or {}
        smap = ((data.get("providers") or {}).get("gitea") or {}).get("scope_map") or {}
        return cls(callers=callers, scope_map=smap)

    def check(self, caller: str, perms: dict[str, str], n_repos: int) -> str | None:
        """Returns a refusal code or None (github-app.md §9.2: subset only)."""
        c = self.callers.get(caller)
        if c is None:
            return "caller_not_allowed"
        mx = c.get("max_permissions") or {}
        for k, lvl in perms.items():
            if k in FORBIDDEN or k not in KNOWN:
                return "permission_not_allowed"
            if k not in mx or LEVEL.get(lvl, 9) > LEVEL.get(mx[k], 0):
                return "permission_not_allowed"
        if n_repos > int(c.get("repos_per_request", 1)):
            return "too_many_repos"
        return None

    def gitea_scopes(self, perms: dict[str, str]) -> list[str]:
        out: set[str] = set()
        for k, lvl in perms.items():
            mapped = self.scope_map.get(f"{k}:{lvl}")
            if mapped is None and lvl == "read":
                mapped = DEFAULT_READ_SCOPES.get(k, [])
            out.update(mapped or [])
        return sorted(out)


@dataclass
class Settings:
    replicant_env: str = "local"
    port: int = 8080
    callers_file: str | None = None
    caller_tokens_file: str | None = None
    gitea_base_url: str = "http://git.replicant.localhost:3300"
    bot_user: str = "replicant-bot"
    bot_password_file: str | None = None
    ttl_s: int = 3600
    db_dsn: str | None = None
    db_password_file: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(replicant_env=env("REPLICANT_ENV", "local") or "local", port=int(env("REPLICANT_HTTP_PORT", "8080") or 8080),
                   callers_file=env("REPLICANT_BROKER_CALLERS_FILE"),
                   caller_tokens_file=env("REPLICANT_BROKER_CALLER_TOKENS_FILE"),
                   gitea_base_url=env("REPLICANT_BROKER_GITEA_BASE_URL", "http://git.replicant.localhost:3300") or "",
                   bot_user=env("REPLICANT_BROKER_GITEA_BOT_USER", "replicant-bot") or "replicant-bot",
                   bot_password_file=env("REPLICANT_BROKER_GITEA_BOT_PASSWORD_FILE"),
                   ttl_s=parse_ttl(env("REPLICANT_BROKER_GITEA_TOKEN_TTL", "3600s")),
                   db_dsn=env("REPLICANT_DB_DSN"), db_password_file=env("REPLICANT_DB_PASSWORD_FILE"))


class Problem(Exception):
    def __init__(self, status: int, code: str, title: str | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.title = status, code, title or code.replace("_", " ")


class Audit:
    """audit.emit (WS-08) through the broker's own login svc_github_token_broker (r_audit_emit). Optional in M1."""

    def __init__(self, s: Settings) -> None:
        self._engine = None
        if s.db_dsn:
            from sqlalchemy.engine import make_url
            from sqlalchemy.ext.asyncio import create_async_engine
            url = make_url(s.db_dsn)
            pw = read_secret(s.db_password_file)
            if pw:
                url = url.set(password=pw)
            self._engine = create_async_engine(url, pool_size=2, max_overflow=2, pool_pre_ping=True)

    async def emit(self, event: dict[str, Any]) -> None:
        ev = {"occurred_at": dt.datetime.now(dt.UTC).isoformat(), "source_service": "github-token-broker",
              "tenant_id": "replicant-system", "channel": "system", **event}
        log.info(json.dumps({"log_type": "audit", **{k: v for k, v in ev.items() if k != "details"},
                             "details": ev.get("details")}))
        if self._engine is None:
            return
        try:
            from sqlalchemy import text
            async with self._engine.begin() as conn:
                await conn.execute(text("SELECT audit.emit(CAST(:e AS jsonb))"), {"e": json.dumps(ev)})
        except Exception as e:  # noqa: BLE001 - M1: audit DDL may be absent; the mint itself is not blocked
            log.warning("audit.emit failed: %s", type(e).__name__)

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()


@dataclass
class Minted:
    token: str
    token_name: str
    expires_at: float
    permissions: dict[str, str]
    repository_ids: list[int]


class Broker:
    def __init__(self, s: Settings, policy: Policy, transport: Any = None) -> None:
        self.s = s
        self.policy = policy
        self._transport = transport
        self._cache: dict[tuple, Minted] = {}
        self._lock = asyncio.Lock()
        self.callers: dict[str, str] = {}
        if s.caller_tokens_file:
            if s.replicant_env != "local":
                raise SystemExit("REPLICANT_BROKER_CALLER_TOKENS_FILE is honoured only when REPLICANT_ENV=local")
            text_ = read_secret(s.caller_tokens_file)
            if text_:
                self.callers = parse_token_map(text_)

    def authenticate(self, request: Request) -> str | None:
        return token_match(bearer_token(request), self.callers)

    def _client(self) -> GiteaClient:
        pw = read_secret(self.s.bot_password_file)
        if not pw:
            raise Problem(503, "bot_credential_unavailable")
        return GiteaClient(self.s.gitea_base_url, basic=(self.s.bot_user, pw), transport=self._transport)

    async def mint(self, caller: str, req: dict[str, Any]) -> tuple[Minted, bool]:
        perms: dict[str, str] = dict(req["permissions"])
        repos = sorted(int(r) for r in req.get("repository_ids") or [])
        key = (caller, int(req["installation_id"]), tuple(repos), tuple(sorted(perms.items())))
        async with self._lock:
            hit = self._cache.get(key)
            if hit and hit.expires_at - time.time() > 600:
                return hit, True
            name = f"{TOKEN_PREFIX}{int(time.time())}-{secrets.token_hex(4)}"
            scopes = self.policy.gitea_scopes(perms)
            async with self._client() as g:
                try:
                    created = await g.create_token(self.s.bot_user, name, scopes)
                except GiteaError as e:
                    raise Problem(502 if e.status >= 500 else 404, "installation_unavailable") from None
            token = created.get("sha1") or created.get("token") or ""
            if not token:
                raise Problem(502, "installation_unavailable")
            m = Minted(token=token, token_name=name, expires_at=time.time() + self.s.ttl_s, permissions=perms,
                       repository_ids=repos)
            self._cache[key] = m
            return m, False

    async def reap(self) -> int:
        """Deletes broker-made tokens older than the TTL (Gitea tokens never expire on their own)."""
        n = 0
        now = time.time()
        async with self._client() as g:
            for t in await g.list_tokens(self.s.bot_user):
                name = t.get("name", "")
                if not name.startswith(TOKEN_PREFIX):
                    continue
                try:
                    created = int(name[len(TOKEN_PREFIX):].split("-", 1)[0])
                except ValueError:
                    continue
                if now - created >= self.s.ttl_s:
                    with contextlib.suppress(GiteaError):
                        await g.delete_token(self.s.bot_user, t.get("id") or name)
                        n += 1
        self._cache = {k: v for k, v in self._cache.items() if v.expires_at > now}
        return n

    async def reaper_loop(self) -> None:
        while True:
            try:
                n = await self.reap()
                if n:
                    log.info("reaped %d expired Gitea tokens", n)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("token reaper: %s", type(e).__name__)
            await asyncio.sleep(60)


def _problem(status: int, code: str, title: str | None = None, audit_id: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"title": title or code.replace("_", " "), "status": status, "code": code}
    if audit_id:
        body["audit_id"] = audit_id
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def validate_request(body: Any) -> str | None:
    if not isinstance(body, dict):
        return "invalid"
    if set(body) - {"installation_id", "repository_ids", "permissions", "purpose", "job_id", "activity_id"}:
        return "invalid"
    if not isinstance(body.get("installation_id"), int) or body["installation_id"] < 1:
        return "invalid"
    perms = body.get("permissions")
    if not isinstance(perms, dict) or not perms:
        return "invalid"
    if set(perms) & FORBIDDEN:
        return "permission_not_allowed"
    for k, v in perms.items():
        if k not in KNOWN or v not in ("read", "write") or (k in ("metadata", "members") and v != "read"):
            return "permission_not_allowed"
    repos = body.get("repository_ids", [])
    if not isinstance(repos, list) or len(repos) > 500 or not all(isinstance(r, int) and r >= 1 for r in repos):
        return "invalid"
    import re
    if not isinstance(body.get("purpose"), str) or not re.match(r"^[a-z][a-z0-9_.]{2,80}$", body["purpose"]):
        return "invalid"
    if "job_id" in body and not re.match(r"^job_[0-9a-hjkmnp-tv-z]{26}$", str(body["job_id"])):
        return "invalid"
    return None


def create_app(settings: Settings | None = None, *, transport: Any = None) -> FastAPI:
    s = settings or Settings.from_env()
    policy = Policy.load(s.callers_file)
    broker = Broker(s, policy, transport=transport)
    audit = Audit(s)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        task = asyncio.create_task(broker.reaper_loop()) if env("REPLICANT_BROKER_REAPER", "true") == "true" else None
        yield
        if task:
            task.cancel()
        await audit.dispose()

    app = FastAPI(title="github-token-broker", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.broker = broker

    @app.post("/v1/installation-tokens")
    async def mint(request: Request) -> JSONResponse:
        caller = broker.authenticate(request)
        if caller is None:
            return _problem(401, "caller_not_allowed", "unauthenticated")
        try:
            body = await request.json()
        except ValueError:
            body = None
        err = validate_request(body)
        if err == "invalid":
            return JSONResponse({"title": "invalid request", "status": 422, "code": "invalid_request"}, status_code=422,
                                media_type="application/problem+json")
        refusal = err or policy.check(caller, body["permissions"], len(body.get("repository_ids") or []))
        base_ev = {"actor_principal_id": f"svc_{caller}", "job_id": body.get("job_id"),
                   "target_kind": "scm_installation", "target_id": str(body["installation_id"]),
                   "details": {"purpose": body["purpose"], "permissions": sorted(f"{k}:{v}" for k, v in
                                                                                  body["permissions"].items()),
                               "repository_ids": [str(r) for r in body.get("repository_ids") or []][:20],
                               "provider": "gitea"}}
        if refusal:
            await audit.emit({"event_type": "github.token_minted", "outcome": "denied", "reason": refusal,
                              **{k: v for k, v in base_ev.items() if v is not None}})
            return _problem(403, refusal)
        try:
            m, cached = await broker.mint(caller, body)
        except Problem as p:
            return _problem(p.status, p.code, p.title)
        await audit.emit({"event_type": "github.token_minted", "outcome": "success",
                          **{k: v for k, v in base_ev.items() if v is not None}})
        return JSONResponse({"token": m.token,
                             "expires_at": dt.datetime.fromtimestamp(m.expires_at, dt.UTC).isoformat().replace(
                                 "+00:00", "Z"),
                             "permissions": m.permissions, "repository_ids": m.repository_ids, "cached": cached})

    @app.post("/v1/app/hook-deliveries:list")
    @app.get("/v1/app/installations")
    @app.patch("/v1/app/hook-config")
    async def github_only() -> JSONResponse:
        return _problem(501, "github_backend_not_configured", "GitHub App operations need the github backend (M3)")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ok_cred = bool(read_secret(s.bot_password_file))
        ok_gitea = False
        try:
            async with GiteaClient(s.gitea_base_url, transport=transport, timeout=3) as g:
                ok_gitea = await g.healthy()
        except Exception:  # noqa: BLE001
            ok_gitea = False
        ok = ok_cred and ok_gitea and bool(policy.callers)
        return JSONResponse({"status": "ok" if ok else "unavailable", "bot_credential": ok_cred, "gitea": ok_gitea,
                             "callers": len(broker.callers)}, status_code=200 if ok else 503)

    return app
