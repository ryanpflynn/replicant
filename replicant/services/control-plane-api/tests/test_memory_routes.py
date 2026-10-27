"""MEM: /v1/admin/memory/** is memory:admin (replicant-admins, D-25): erin (tenant admin) gets 403 and the denial is
audited; carol is forwarded to memory-service with a signed caller principal (no database or memory-service)."""
import base64
import contextlib
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from control_plane_api import routes_memory
from control_plane_api.app import create_app
from control_plane_api.config import Settings


@pytest.fixture
def client(monkeypatch, tmp_path):  # noqa: ANN001, ANN201
    tok = tmp_path / "memory_cpa_token"
    tok.write_text("tok-cpa")
    monkeypatch.setenv("REPLICANT_MEMORY_TOKEN_FILE", str(tok))
    app = create_app(Settings(background_tasks=False))
    audited: list[dict] = []

    @contextlib.asynccontextmanager
    async def tx(**kw):  # noqa: ANN003, ANN202
        yield None

    async def emit(conn, db, ev):  # noqa: ANN001, ANN202
        audited.append(ev)
        return "aud_" + "1" * 26

    async def refresh(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None
    monkeypatch.setattr(app.state.db, "tx", tx)
    monkeypatch.setattr(app.state.auth.rbac, "refresh", refresh)
    monkeypatch.setattr(routes_memory.audit, "emit", emit)
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"items": []})
    real = httpx.AsyncClient

    def fake(*a, **k):  # noqa: ANN002, ANN003, ANN202
        k.pop("timeout", None)
        return real(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(routes_memory.httpx, "AsyncClient", fake)
    return TestClient(app), audited, seen


def test_erin_denied_and_audited_carol_forwarded_signed(client):  # noqa: ANN001
    c, audited, seen = client
    r = c.get("/v1/admin/memory/candidates", headers={"X-Replicant-Dev-User": "erin"})
    assert r.status_code == 403 and r.json()["code"] == "not_memory_admin" and r.json()["audit_id"].startswith("aud_")
    assert audited[0]["event_type"] == "memory.admin_denied" and audited[0]["outcome"] == "denied" and not seen
    r = c.post("/v1/admin/memory/entries/mem_x:approve", headers={"X-Replicant-Dev-User": "carol", "If-Match": "3",
                                                                   "Idempotency-Key": "abcdefgh12"}, json={})
    assert r.status_code == 200 and len(seen) == 1
    req = seen[0]
    assert req.url.path == "/internal/v1/admin/entries/mem_x:approve" and req.headers["if-match"] == "3"
    raw = req.headers["x-replicant-caller"]
    assert hmac.compare_digest(req.headers["x-replicant-caller-sig"],
                               hmac.new(b"tok-cpa", raw.encode(), hashlib.sha256).hexdigest())
    doc = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert doc["login"] == "carol" and "replicant-admins" in doc["groups"]


def test_scan_request_validates_against_c_jobreq():
    from control_plane_api import contracts
    from control_plane_api.routes_memory import scan_request
    jr = scan_request("payments", "prj_ledger", {"repo_id": "1", "full_name": "acme-payments/ledger-api",
                                                  "host": "git.replicant.localhost:3300", "provider": "gitea"},
                      "usr_" + "a" * 26)
    assert contracts.errors("job-request", jr) == []
    assert jr["workflow_type"] == "docs" and jr["constraints"]["labels"] == ["replicant-repo-scan"]
