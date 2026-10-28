"""slack-integration's real SlackGateway (slack_sdk AsyncWebClient) against slack-mock served by uvicorn on a free port;
the database is the in-memory FakeRepo."""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from conftest import BOT_TOKEN, CALLER_TOKEN, DELIVERY, PRINCIPAL, SIGNING_SECRET, FakeRepo, deliver_body
from fastapi.testclient import TestClient

from slack_integration.app import create_app
from slack_integration.config import Settings
from slack_integration.slack import SlackGateway

slack_mock_app = pytest.importorskip("slack_mock.app")
uvicorn = pytest.importorskip("uvicorn")
WS_YAML = Path(__file__).resolve().parents[3] / "dev/compose/config/slack-mock/workspace.yaml"
AUTH = {"Authorization": f"Bearer {CALLER_TOKEN}"}


@pytest.fixture(scope="module")
def mock_url() -> Iterator[str]:
    ws = slack_mock_app.Workspace.load(str(WS_YAML))
    app = slack_mock_app.create_app(ws, bot_token=BOT_TOKEN, signing_secret=SIGNING_SECRET)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/healthz").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield base
    server.should_exit = True
    t.join(5)


def test_deliver_through_slack_mock(settings: Settings, mock_url: str) -> None:
    httpx.delete(mock_url + "/_mock/messages")
    s = Settings(**{**settings.__dict__, "slack_api_url": mock_url + "/api/"})
    repo = FakeRepo()
    repo.links[(PRINCIPAL, "T0LOCAL01")] = "U0GRACE01"
    with TestClient(create_app(s, repo=repo, slack=SlackGateway(s))) as c:
        r = c.post("/internal/v1/notifications/deliver", json=deliver_body(), headers=AUTH)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["state"] == "sent" and out["slack_user_id"] == "U0GRACE01"
        assert out["external_ref"].startswith("D0GRACE01:")
        assert c.get("/readyz").json()["slack"]["team_id"] == "T0LOCAL01"

        msgs = httpx.get(mock_url + "/_mock/messages", params={"user": "U0GRACE01", "method": "chat.postMessage"}).json()["messages"]
        assert len(msgs) == 1
        m = msgs[0]
        assert f"{m['channel']}:{m['ts']}" == out["external_ref"]
        assert m["payload"]["unfurl_links"] is False and m["payload"]["unfurl_media"] is False
        assert m["payload"]["metadata"]["event_payload"]["delivery_id"] == DELIVERY
        assert [b["type"] for b in m["blocks"]] == ["header", "section", "section", "context", "actions"]

        # duplicate: no second post
        assert c.post("/internal/v1/notifications/deliver", json=deliver_body(), headers=AUTH).json()["duplicate"] is True
        assert len(httpx.get(mock_url + "/_mock/messages", params={"method": "chat.postMessage"}).json()["messages"]) == 1

        # rate limited by Slack -> 429 + Retry-After
        httpx.post(mock_url + "/_mock/faults", json={"method": "chat.postMessage", "error": "ratelimited", "retry_after": 9})
        r = c.post("/internal/v1/notifications/deliver", json=deliver_body(delivery_id="ntf_" + "1" * 26), headers=AUTH)
        assert r.status_code == 429 and r.headers["retry-after"] == "9"

        # recipient linked to a Slack user the workspace does not know -> permanent
        repo.links[(PRINCIPAL, "T0LOCAL01")] = "U0NOBODY1"
        r = c.post("/internal/v1/notifications/deliver", json=deliver_body(delivery_id="ntf_" + "2" * 26), headers=AUTH)
        assert r.status_code == 422 and r.json() == {"state": "failed", "reason": "slack_user_not_found", "retryable": False}


def test_wrong_bot_token_is_config_error(settings: Settings, mock_url: str, secrets_dir: Path) -> None:
    (secrets_dir / "bot").write_text("xoxb-wrong")
    s = Settings(**{**settings.__dict__, "slack_api_url": mock_url + "/api/"})
    repo = FakeRepo()
    repo.links[(PRINCIPAL, "T0LOCAL01")] = "U0ALICE01"
    with TestClient(create_app(s, repo=repo, slack=SlackGateway(s))) as c:
        r = c.post("/internal/v1/notifications/deliver", json=deliver_body(delivery_id="ntf_" + "3" * 26), headers=AUTH)
        assert r.status_code == 503 and r.json()["reason"] == "slack_invalid_auth"
        assert c.get("/readyz").status_code == 200
