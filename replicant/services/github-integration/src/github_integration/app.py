"""github-integration ASGI app (github-app.md §3): webhook ingress, admin installation registration, consumer.

Routes:
* `POST /webhooks/gitea` - verifies `X-Gitea-Signature` (HMAC-SHA256 of the raw body, `gitea_webhook_secret`) before
  any parsing, persists the delivery keyed by `X-Gitea-Delivery` (ON CONFLICT DO NOTHING) and answers 202.
* `POST /github/webhook` - the GitHub App route (G-9); M1 has no dev App secret, so it answers 503.
* `POST /v1/admin/scm-installations` `{provider: gitea, host, org, tenant_id}` - registers a Gitea org as an
  installation (synthetic installation ids 1, 2, ...); seed step 6. M1 auth: the dev seed token (local only).
* `GET /healthz`, `GET /readyz`.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from replicant_scm import canonical_host, verify_gitea_signature
from replicant_telemetry.common import bearer_ok, bearer_token, token_ok
from github_integration.clients import Broker, ControlPlane, S3, Temporal
from github_integration.config import Settings, read_secret
from github_integration.consumer import Consumer, register_repo

log = logging.getLogger("github_integration")
MAX_BODY = 26 * 1024 * 1024
ORG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-_.]{0,99}$")
TENANT_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
COUNTS: dict[str, int] = {}


def _count(outcome: str) -> None:
    """replicant_github_deliveries_total{outcome} (XD-FIX-R1-195); exported at /metrics."""
    COUNTS[outcome] = COUNTS.get(outcome, 0) + 1


def make_engine(s: Settings) -> AsyncEngine:
    if not s.db_dsn:
        raise SystemExit("REPLICANT_DB_DSN is required")
    url = make_url(s.db_dsn)
    pw = read_secret(s.db_password_file)
    if pw:
        url = url.set(password=pw)
    return create_async_engine(url, pool_size=8, max_overflow=4, pool_pre_ping=True,
                               connect_args={"application_name": "github-integration"})


def _problem(status: int, code: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"title": code.replace("_", " "), "status": status, "code": code, **extra}, status_code=status,
                        media_type="application/problem+json")


def create_app(settings: Settings | None = None, *, engine: AsyncEngine | None = None, cp: ControlPlane | None = None,
               broker: Broker | None = None, temporal: Temporal | None = None, gitea_transport: Any = None,
               run_consumer: bool | None = None) -> FastAPI:
    s = settings or Settings.from_env()
    if not s.is_local and s.dev_seed_token_file:
        raise SystemExit("P1 security: REPLICANT_DEV_SEED_TOKEN_FILE is honoured only when REPLICANT_ENV=local")
    engine = engine or make_engine(s)
    consumer = Consumer(s, engine, cp or ControlPlane(s), broker or Broker(s), S3(s), temporal or Temporal(s),
                        gitea_transport=gitea_transport)
    run_consumer = (s.role in ("all", "consumer")) if run_consumer is None else run_consumer

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        tasks = [asyncio.create_task(consumer.loop()) for _ in range(max(1, s.consumer_workers))] if run_consumer else []
        yield
        for t in tasks:
            t.cancel()
        await engine.dispose()

    app = FastAPI(title="github-integration", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.consumer = consumer
    app.state.engine = engine

    @app.post("/webhooks/gitea")
    async def gitea_webhook(request: Request) -> Response:
        body = await request.body()                       # raw bytes, before any JSON parsing
        if len(body) > MAX_BODY:
            return Response(status_code=413)
        secret = read_secret(s.gitea_webhook_secret_file)
        sig = request.headers.get("x-gitea-signature") or request.headers.get("x-hub-signature-256")
        if not secret or not verify_gitea_signature(sig, body, secret.encode()):
            _count("bad_signature")
            return Response(status_code=401)
        guid = (request.headers.get("x-gitea-delivery") or request.headers.get("x-github-delivery") or "").strip()
        event = (request.headers.get("x-gitea-event") or request.headers.get("x-github-event") or "").strip()
        etype = (request.headers.get("x-gitea-event-type") or event).strip()
        if not guid or not event or len(guid) > 128:
            _count("error")
            return Response(status_code=400)
        async with engine.begin() as conn:
            res = await conn.execute(text(
                "INSERT INTO github_app.inbound_deliveries (delivery_guid, provider, event, target_type, source, payload, "
                "payload_sha256) VALUES (:g, 'gitea', :e, :t, 'webhook', :p, :h) ON CONFLICT (delivery_guid) DO NOTHING"),
                {"g": guid, "e": event[:100], "t": etype[:100], "p": body, "h": hashlib.sha256(body).hexdigest()})
        _count("accepted" if res.rowcount else "duplicate")
        return Response(status_code=202)

    @app.post("/github/webhook")
    async def github_webhook() -> Response:
        # The dev GitHub App webhook secret comes from `replicant secret-agent` (M3); without it nothing can verify.
        return _problem(503, "github_app_not_registered")

    @app.post("/v1/admin/scm-installations")
    async def register_installation(request: Request) -> JSONResponse:
        seed = read_secret(s.dev_seed_token_file) if s.is_local else None
        cpa = read_secret(s.notify_caller_token_file)   # CONFIG: control-plane-api's service token (live trigger sync)
        if not bearer_ok(request, (seed, cpa)):
            return _problem(401, "unauthenticated")
        try:
            body = await request.json()
        except ValueError:
            return _problem(422, "invalid_request")
        if not isinstance(body, dict) or body.get("provider") != "gitea" or not ORG_RE.match(str(body.get("org", ""))) \
                or not TENANT_RE.match(str(body.get("tenant_id", ""))) or not body.get("host"):
            return _problem(422, "invalid_request", detail="M1 supports provider gitea with host, org, tenant_id")
        host = canonical_host(str(body["host"]))
        org, tenant = body["org"], body["tenant_id"]
        async with engine.begin() as conn:
            ex = (await conn.execute(text("SELECT installation_id, tenant_id FROM github_app.installations WHERE "
                                          "provider = 'gitea' AND host = :h AND lower(account_login) = lower(:o)"),
                                     {"h": host, "o": org})).first()
            if ex is not None:
                if ex.tenant_id != tenant:
                    return _problem(409, "installation_claimed_by_other_tenant")
                inst, created = ex.installation_id, False
            else:
                await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('github_app.installations:gitea'))"))
                inst = (await conn.execute(text("SELECT coalesce(max(installation_id), 0) + 1 FROM "
                                                "github_app.installations WHERE provider = 'gitea' AND host = :h"),
                                           {"h": host})).scalar_one()
                await conn.execute(text(
                    "INSERT INTO github_app.installations (provider, host, installation_id, registration, account_login, "
                    "tenant_id, status, permissions) VALUES ('gitea', :h, :i, 'dev', :o, :t, 'active', "
                    "CAST(:p AS jsonb))"),
                    {"h": host, "i": inst, "o": org, "t": tenant,
                     "p": json.dumps({"issues": "write", "pull_requests": "write", "contents": "write"})})
                created = True
        repos = await _sync_repos(s, consumer, engine, host, org, int(inst), tenant)
        return JSONResponse({"provider": "gitea", "host": host, "org": org, "tenant_id": tenant,
                             "installation_id": int(inst), "created": created, "repos_registered": repos},
                            status_code=201 if created else 200)

    @app.put("/internal/v1/repo-settings")
    async def put_repo_settings(request: Request) -> JSONResponse:
        """Writer side of control-plane-api `PUT /v1/tenants/{t}/github/repos/{repo_id}/settings` (github_app is
        github-integration's schema). Registers the repo's Gitea org as an installation for the tenant when needed,
        the repo, and a new current settings version. M1 auth: the dev seed token forwarded by control-plane-api."""
        seed = read_secret(s.dev_seed_token_file) if s.is_local else None
        cpa = read_secret(s.notify_caller_token_file)   # CONFIG: control-plane-api's service token (live trigger sync)
        tok = bearer_token(request)
        by_seed = token_ok(tok, seed)
        if not (by_seed or token_ok(tok, cpa)):
            return _problem(401, "unauthenticated")
        try:
            body = await request.json()
        except ValueError:
            return _problem(422, "invalid_request")
        st = body.get("settings") or {}
        full_name = str(st.get("full_name", ""))
        owner, _, name = full_name.partition("/")
        if body.get("provider") != "gitea" or not ORG_RE.match(owner) or not name or \
                not TENANT_RE.match(str(body.get("tenant_id", ""))) or not str(body.get("repo_id", "")).isdigit():
            return _problem(422, "invalid_request")
        host, tenant, repo_id = canonical_host(str(body["host"])), body["tenant_id"], str(body["repo_id"])
        allowed = {"repo_id", "full_name", "enabled", "trigger_modes", "trigger_label", "auto_open_filter",
                   "allowed_workflow_types", "default_priority", "ack_comment", "status_label_prefix"}
        if set(st) - allowed:
            return _problem(422, "invalid_request", detail=f"unknown settings {sorted(set(st) - allowed)}")
        actor = str(body.get("actor") or "")
        updated_by = actor if not by_seed and re.match(r"^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$",
                                                       actor) else ("svc_control-plane-api" if not by_seed
                                                                    else "svc_dev-seeder")
        settings_doc = {"trigger_modes": ["label", "command"], "trigger_label": "replicant", "ack_comment": True,
                        **{k: v for k, v in st.items() if k not in ("repo_id", "full_name")}}
        async with engine.begin() as conn:
            inst_row = (await conn.execute(text("SELECT installation_id, tenant_id FROM github_app.installations WHERE "
                                                "provider = 'gitea' AND host = :h AND lower(account_login) = lower(:o)"),
                                           {"h": host, "o": owner})).first()
            if inst_row is None:
                await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('github_app.installations:gitea'))"))
                inst = (await conn.execute(text("SELECT coalesce(max(installation_id), 0) + 1 FROM "
                                                "github_app.installations WHERE provider = 'gitea' AND host = :h"),
                                           {"h": host})).scalar_one()
                await conn.execute(text(
                    "INSERT INTO github_app.installations (provider, host, installation_id, registration, account_login, "
                    "tenant_id, status) VALUES ('gitea', :h, :i, 'dev', :o, :t, 'active')"),
                    {"h": host, "i": inst, "o": owner, "t": tenant})
            elif inst_row.tenant_id != tenant:
                return _problem(409, "installation_claimed_by_other_tenant")
            else:
                inst = inst_row.installation_id
            await register_repo(conn, host, repo_id, int(inst), full_name, tenant, None, "svc_github-integration")
            cur = (await conn.execute(text("SELECT version, settings FROM github_app.repo_settings WHERE provider = "
                                           "'gitea' AND host = :h AND repo_id = :r AND is_current FOR UPDATE"),
                                      {"h": host, "r": repo_id})).first()
            if cur is None or cur.settings != settings_doc:
                v = (cur.version + 1) if cur else 1
                await conn.execute(text("UPDATE github_app.repo_settings SET is_current = false WHERE provider = 'gitea' "
                                        "AND host = :h AND repo_id = :r AND is_current"), {"h": host, "r": repo_id})
                await conn.execute(text(
                    "INSERT INTO github_app.repo_settings (provider, host, repo_id, version, settings, is_current, "
                    "updated_by) VALUES ('gitea', :h, :r, :v, CAST(:s AS jsonb), true, :by)"),
                    {"h": host, "r": repo_id, "v": v, "s": json.dumps(settings_doc), "by": updated_by})
        return JSONResponse({"repo_id": repo_id, "full_name": full_name, **settings_doc,
                             "installation_id": int(inst), "tenant_id": tenant})

    @app.post("/internal/v1/notifications/deliver")
    async def deliver_notification(request: Request) -> JSONResponse:
        """control-plane-api's notify dispatcher -> issue comment as replicant-bot (`github_comment` channel).
        Auth: bearer = the notify delivery token (local static caller token; prod: projected SA token)."""
        want = read_secret(s.notify_caller_token_file)
        if not bearer_ok(request, want):
            return _problem(401, "unauthenticated")
        try:
            b = await request.json()
            did, repo, number, body = b["delivery_id"], b["repo"] or {}, int(b["issue_number"]), str(b["body"])
        except (ValueError, KeyError, TypeError):
            return _problem(422, "invalid_request")
        if not re.match(r"^ntf_[0-9a-hjkmnp-tv-z]{26}$", str(did)) or not body or len(body) > 60000:
            return _problem(422, "invalid_request")
        try:
            out = await consumer.post_notification(did, repo, number, body, b.get("job_id"))
        except Exception as e:  # noqa: BLE001 - broker / Gitea down: the dispatcher retries
            log.warning("notification comment failed: %s", type(e).__name__)
            return _problem(503, "comment_failed", detail=type(e).__name__)
        _count(f"notification_{out['state']}")
        return JSONResponse(out)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1 FROM github_app.inbound_deliveries LIMIT 1"))
            ok_db = True
        except Exception:  # noqa: BLE001
            ok_db = False
        ok_secret = bool(read_secret(s.gitea_webhook_secret_file))
        ok = ok_db and ok_secret
        return JSONResponse({"status": "ok" if ok else "unavailable", "db": ok_db, "webhook_secret": ok_secret},
                            status_code=200 if ok else 503)

    @app.get("/metrics")
    async def metrics() -> Response:
        lines = ["# TYPE replicant_github_deliveries_total counter"]
        lines += [f'replicant_github_deliveries_total{{outcome="{k}"}} {v}' for k, v in sorted(COUNTS.items())]
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    return app


async def _sync_repos(s: Settings, consumer: Consumer, engine: AsyncEngine, host: str, org: str, inst: int,
                      tenant: str) -> int:
    """Best effort: list the org's repos with a broker token. Repos are also registered lazily on first webhook."""
    try:
        from replicant_scm.gitea import GiteaClient
        # the broker needs one repo id to scope a token; without one we cannot ask yet -> lazy registration
        async with engine.begin() as conn:
            any_repo = (await conn.execute(text("SELECT repo_id FROM github_app.repos WHERE provider = 'gitea' AND "
                                                "host = :h AND installation_id = :i LIMIT 1"), {"h": host, "i": inst})
                        ).scalar()
        if any_repo is None:
            return 0
        tok = await consumer.broker.token(inst, any_repo, "github.installation_sync")
        async with GiteaClient(s.gitea_base_url, token=tok) as g:
            repos = await g.list_org_repos(org)
        async with engine.begin() as conn:
            for r in repos:
                await register_repo(conn, host, str(r["id"]), inst, r["full_name"], tenant, r.get("default_branch"),
                                    "svc_github-integration")
        return len(repos)
    except Exception as e:  # noqa: BLE001
        log.info("repo sync deferred to first webhook: %s", type(e).__name__)
        return 0
