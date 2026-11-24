"""The core process (core.py, SIMPLIFY Phase 2): one listener (no run-grants app since SIMPLIFY-D50), the module ingress routes on
the API listener (webhook HMAC and Slack signatures still enforced), the in-process caller identity, Slack off, and
the projector's sole ownership of jobs_read / approvals. No database, no Temporal."""
import hashlib
import hmac
import json
import pathlib
import re
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from control_plane_api import in_process
from control_plane_api.app import SELF_AUTHENTICATED_PATHS, iter_routes
from control_plane_api.config import Settings
from control_plane_api.core import CoreModules, create_core

DSN = "postgresql+psycopg://nobody@127.0.0.1:1/replicant"   # never connected to in these tests
WEBHOOK_SECRET = "whsec-test"
SLACK_SECRET = "slack-signing-test"


@pytest.fixture
def core(tmp_path, monkeypatch):
    (tmp_path / "wh").write_text(WEBHOOK_SECRET)
    (tmp_path / "ss").write_text(SLACK_SECRET)
    for k, v in {"REPLICANT_GITHUB_INTEGRATION_DB_DSN": DSN, "REPLICANT_SLACK_INTEGRATION_DB_DSN": DSN,
                 "REPLICANT_GITEA_WEBHOOK_SECRET_FILE": str(tmp_path / "wh"),
                 "SLACK_SIGNING_SECRET_FILE": str(tmp_path / "ss")}.items():
        monkeypatch.setenv(k, v)
    s = Settings(background_tasks=False)
    return create_core(s, CoreModules(github=True, slack=True))


def _paths(app):
    return {getattr(r, "path", "") for r in iter_routes(list(app.routes))}


def test_core_has_no_run_grant_listener(core):
    """SIMPLIFY-D50: no JWKS or grant revocation list in core; the Runner's routes are internal and permissioned."""
    app = core
    assert not _paths(app) & {"/.well-known/jwks.json", "/grants/revoked"}
    assert {"/internal/v1/jobs/{job_id}/run-scope", "/internal/v1/revocations"} <= _paths(app)
    assert not hasattr(CoreModules(), "grants")


def test_api_listener_carries_module_ingress_only(core):
    app = core
    paths = _paths(app)
    assert SELF_AUTHENTICATED_PATHS <= paths
    # the modules' internal routes are reachable in process only, never on a listener
    for p in ("/internal/v1/repo-settings", "/internal/v1/inventory:rescan", "/internal/v1/notifications/deliver",
              "/internal/v1/gates/{gate_id}/resolved", "/metrics"):
        assert p not in paths


def test_gitea_webhook_hmac_enforced_on_core(core):
    app = core
    c = TestClient(app)
    body = b'{"action": "opened"}'
    assert c.post("/webhooks/gitea", content=body, headers={"X-Gitea-Delivery": "d1", "X-Gitea-Event": "issues"}
                  ).status_code == 401
    assert c.post("/webhooks/gitea", content=body, headers={"X-Gitea-Signature": "0" * 64, "X-Gitea-Delivery": "d1",
                                                            "X-Gitea-Event": "issues"}).status_code == 401
    good = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    # a valid signature gets past verification (no delivery id -> 400, before any database access)
    assert c.post("/webhooks/gitea", content=body, headers={"X-Gitea-Signature": good}).status_code == 400


def test_slack_signature_enforced_on_core(core):
    app = core
    c = TestClient(app)
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    assert c.post("/slack/events", content=body).status_code == 401
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(SLACK_SECRET.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    r = c.post("/slack/events", content=body, headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig})
    assert r.status_code == 200 and r.json() == {"challenge": "abc"}


async def test_in_process_caller_identity(core):
    app = core
    # github-integration -> control-plane-api: the module's principal comes from the ASGI scope, no token
    async with httpx.AsyncClient(transport=in_process.transport(app, "svc_github-integration"),
                                 base_url="http://control-plane-api") as c:
        r = await c.put("/internal/v1/repos/42/profile", json={})
        assert r.status_code == 422            # authenticated and authorized; the empty profile is invalid
    # the same request without the scope marker (any network client), or from an unregistered principal: 401
    for t in (httpx.ASGITransport(app=app), in_process.transport(app, "svc_someone-else")):
        async with httpx.AsyncClient(transport=t, base_url="http://control-plane-api") as c:
            r = await c.put("/internal/v1/repos/42/profile", json={},
                            headers={"replicant.in_process_caller": "svc_github-integration"})
            assert r.status_code == 401
    # slack-integration's App Home list (service:slack-integration)
    async with httpx.AsyncClient(transport=in_process.transport(app, "svc_slack-integration"),
                                 base_url="http://control-plane-api") as c:
        assert (await c.get("/internal/v1/principals/usr_nobody/waiting")).status_code == 404


async def test_control_plane_reaches_modules_in_process(core):
    app = core
    gi = app.state.in_process["github-integration"].app
    # the URL of the old service resolves to the module app of this process
    async with in_process.client(app.state.in_process, "http://github-integration:8080") as c:
        r = await c.post("http://github-integration:8080/internal/v1/inventory:rescan", json={"repo_ids": ["x"]})
        assert r.status_code == 422            # past the caller check (no token), stopped by validation
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gi), base_url="http://gi") as c:
        assert (await c.post("/internal/v1/inventory:rescan", json={"repo_ids": ["x"]})).status_code == 401
    async with in_process.client(app.state.in_process, "http://slack-integration:8080") as c:
        r = await c.post("http://slack-integration:8080/internal/v1/notifications/deliver", json={})
        assert r.status_code == 422
    # github-integration's own control-plane client is wired in process
    cp = gi.state.consumer.cp
    assert await cp.put_profile("42", {}) == 422


async def test_slack_off_suppresses_slack_deliveries(monkeypatch):
    monkeypatch.delenv("REPLICANT_SLACK_ENABLED", raising=False)
    assert CoreModules.from_env().slack is False
    app = create_core(Settings(background_tasks=False), CoreModules())
    assert "/slack/events" not in _paths(app) and "/webhooks/gitea" not in _paths(app)
    loops = app.state.notify_loops
    for row in (SimpleNamespace(payload={}, channel="slack_dm"),
                SimpleNamespace(payload={"op": {"gate_id": "gate_x"}}, channel="slack_dm")):
        assert await loops.send(row) == ("suppressed", None, "slack_disabled")


def test_module_switches(monkeypatch):
    for k in ("REPLICANT_GITHUB_INTEGRATION_DB_DSN", "REPLICANT_PROJECTOR_DB_DSN",
              "REPLICANT_GITHUB_INTEGRATION_ENABLED", "REPLICANT_PROJECTOR_ENABLED", "REPLICANT_SLACK_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    assert CoreModules.from_env() == CoreModules()
    monkeypatch.setenv("REPLICANT_GITHUB_INTEGRATION_DB_DSN", DSN)
    monkeypatch.setenv("REPLICANT_PROJECTOR_DB_DSN", DSN)
    monkeypatch.setenv("REPLICANT_SLACK_ENABLED", "true")
    m = CoreModules.from_env()
    assert m.github and m.projector and m.slack
    monkeypatch.setenv("REPLICANT_PROJECTOR_ENABLED", "false")
    assert CoreModules.from_env().projector is False


def test_only_the_projector_writes_jobs_read_and_approvals():
    """Module ownership (postgres-ownership.md): with the projector in the core process, no other module's code writes
    its schemas. The database enforces it too: only the projector's login holds the write grants."""
    root = pathlib.Path(__file__).resolve().parents[3]
    write = re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(jobs_read|approvals)\.", re.I)
    offenders = []
    for d in ("services/control-plane-api/src", "services/github-integration/src", "services/slack-integration/src"):
        for f in (root / d).rglob("*.py"):
            if write.search(f.read_text()):
                offenders.append(str(f.relative_to(root)))
    assert offenders == []
    assert write.search((root / "workers/replicant-projector/replicant_projector/db.py").read_text())


def test_core_lifespan_runs_module_loops_and_projector(core, monkeypatch):
    """The API listener's lifespan also starts the modules' loops and the projector worker, and stops them."""
    import asyncio

    import replicant_projector.cli as projector_cli
    events = []

    async def fake_projector():
        events.append("projector started")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("projector stopped")

    monkeypatch.setattr(projector_cli, "serve_in_process", fake_projector)
    monkeypatch.setenv("SLACK_API_URL", "http://127.0.0.1:1/api/")
    for k in ("REPLICANT_READINESS_LOOP", "REPLICANT_NOTIFY_LOOPS"):
        monkeypatch.setenv(k, "false")
    app = create_core(Settings(background_tasks=True), CoreModules(github=True, slack=True, projector=True))
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        assert events == ["projector started"]
    assert events == ["projector started", "projector stopped"]
