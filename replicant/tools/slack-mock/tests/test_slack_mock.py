from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from slack_mock.app import Workspace, create_app, sign
from slack_mock.render import mrkdwn

WS_YAML = Path(__file__).resolve().parents[3] / "dev/compose/config/slack-mock/workspace.yaml"
TOKEN = "xoxb-test-token"
SECRET = "test-signing-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client() -> TestClient:
    ws = Workspace.load(str(WS_YAML))
    return TestClient(create_app(ws, bot_token=TOKEN, signing_secret=SECRET))


def test_workspace_has_grace() -> None:
    ws = Workspace.load(str(WS_YAML))
    assert ws.users["U0GRACE01"]["name"] == "grace"
    assert ws.bot_user_id == "U0FACTBOT"


def test_auth(client: TestClient) -> None:
    assert client.post("/api/auth.test").json() == {"ok": False, "error": "not_authed"}
    assert client.post("/api/auth.test", headers={"Authorization": "Bearer nope"}).json()["error"] == "invalid_auth"
    r = client.post("/api/auth.test", headers=AUTH).json()
    assert r["ok"] and r["team_id"] == "T0LOCAL01" and r["user_id"] == "U0FACTBOT"
    # token in a form body works too (legacy clients)
    assert client.post("/api/auth.test", data={"token": TOKEN}).json()["ok"]


def test_unknown_method(client: TestClient) -> None:
    r = client.post("/api/chat.delete", headers=AUTH)
    assert r.status_code == 404 and r.json()["error"] == "unknown_method"


def test_open_dm_and_post_json(client: TestClient) -> None:
    r = client.post("/api/conversations.open", headers=AUTH, data={"users": "U0ALICE01"}).json()
    assert r == {"ok": True, "channel": {"id": "D0ALICE01"}, "already_open": False}
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "PR #8"}}]
    r = client.post("/api/chat.postMessage", headers=AUTH,
                    json={"channel": "D0ALICE01", "text": "PR #8 ready", "blocks": blocks,
                          "metadata": {"event_type": "replicant_notification", "event_payload": {"delivery_id": "x"}}}).json()
    assert r["ok"] and r["channel"] == "D0ALICE01" and r["message"]["blocks"] == blocks
    msgs = client.get("/_mock/messages", params={"user": "U0ALICE01"}).json()["messages"]
    assert len(msgs) == 1
    m = msgs[0]
    assert m["ts"] == r["ts"] and m["user"] == "U0ALICE01" and m["method"] == "chat.postMessage"
    assert m["payload"]["metadata"]["event_type"] == "replicant_notification"
    assert client.get("/_mock/messages", params={"user": "U0BOB0001"}).json()["messages"] == []
    assert client.get("/_mock/messages", params={"since_ts": r["ts"]}).json()["messages"] == []


def test_post_form_body_with_json_blocks(client: TestClient) -> None:
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]
    r = client.post("/api/chat.postMessage", headers=AUTH,
                    data={"channel": "#payments-eng", "text": "hi", "blocks": json.dumps(blocks)}).json()
    assert r["ok"] and r["channel"] == "C0PAYENG01"
    assert client.get("/_mock/messages", params={"channel": "C0PAYENG01"}).json()["messages"][0]["blocks"] == blocks


def test_post_to_user_id_resolves_dm(client: TestClient) -> None:
    r = client.post("/api/chat.postMessage", headers=AUTH, json={"channel": "U0GRACE01", "text": "hi"}).json()
    assert r["channel"] == "D0GRACE01"


def test_validation_errors(client: TestClient) -> None:
    post = lambda **kw: client.post("/api/chat.postMessage", headers=AUTH, json=kw).json()  # noqa: E731
    assert post(channel="C0NOPE", text="x")["error"] == "channel_not_found"
    assert post(channel="D0NOBODY1", text="x")["error"] == "channel_not_found"
    assert post(channel="D0FACTBOT", text="x")["error"] == "channel_not_found"
    assert post(channel="C0PAYENG01")["error"] == "no_text"
    assert post(channel="C0PAYENG01", text="x", blocks=[{"type": "section"}] * 51)["error"] == "invalid_blocks"
    assert post(channel="C0PAYENG01", text="x", blocks="nope")["error"] == "invalid_blocks"
    o = lambda **kw: client.post("/api/conversations.open", headers=AUTH, json=kw).json()  # noqa: E731
    assert o(users="U0NOPE")["error"] == "user_not_found"
    assert o(users="U0FACTBOT")["error"] == "cannot_dm_bot"
    assert o()["error"] == "users_list_not_supplied"
    assert client.post("/api/users.info", headers=AUTH, data={"user": "U0NOPE"}).json()["error"] == "user_not_found"


def test_update_ephemeral_info_views(client: TestClient) -> None:
    ts = client.post("/api/chat.postMessage", headers=AUTH, json={"channel": "C0PAYAPPR1", "text": "gate"}).json()["ts"]
    r = client.post("/api/chat.update", headers=AUTH, json={"channel": "C0PAYAPPR1", "ts": ts, "text": "approved"}).json()
    assert r["ok"] and r["ts"] == ts
    assert client.post("/api/chat.update", headers=AUTH,
                       json={"channel": "C0PAYAPPR1", "ts": "1.000001", "text": "x"}).json()["error"] == "message_not_found"
    r = client.post("/api/chat.postEphemeral", headers=AUTH, json={"channel": "C0PAYAPPR1", "user": "U0BOB0001", "text": "e"})
    assert r.json()["ok"] and r.json()["message_ts"]
    u = client.get("/api/users.info", headers=AUTH, params={"user": "U0BOB0001"}).json()
    assert u["user"]["name"] == "bob" and u["user"]["profile"]["email"] == "bob@acme.test"
    c = client.post("/api/conversations.info", headers=AUTH, data={"channel": "C0PAYAPPR1"}).json()
    assert c["channel"]["name"] == "payments-approvals"
    c = client.post("/api/conversations.info", headers=AUTH, data={"channel": "D0BOB0001"}).json()
    assert c["channel"]["is_im"] and c["channel"]["user"] == "U0BOB0001"
    v = client.post("/api/views.open", headers=AUTH, json={"trigger_id": "1.2.3", "view": {
        "type": "modal", "title": {"type": "plain_text", "text": "Reject"}, "blocks": []}}).json()
    assert v["ok"] and v["view"]["id"].startswith("V")
    assert client.post("/api/views.open", headers=AUTH, json={"view": {"type": "modal"}}).json()["error"] == "invalid_arguments"
    methods = [m["method"] for m in client.get("/_mock/messages").json()["messages"]]
    assert methods == ["chat.postMessage", "chat.update", "chat.postEphemeral", "views.open"]
    assert [m["method"] for m in client.get("/_mock/messages", params={"since_ts": ts}).json()["messages"]] == \
        ["chat.update", "chat.postEphemeral", "views.open"]
    assert client.delete("/_mock/messages").status_code == 204
    assert client.get("/_mock/messages").json()["messages"] == []


def test_faults(client: TestClient) -> None:
    client.post("/_mock/faults", json={"method": "chat.postMessage", "error": "ratelimited", "retry_after": 7})
    r = client.post("/api/chat.postMessage", headers=AUTH, json={"channel": "C0PAYENG01", "text": "x"})
    assert r.status_code == 429 and r.headers["retry-after"] == "7" and r.json()["error"] == "ratelimited"
    assert client.post("/api/chat.postMessage", headers=AUTH, json={"channel": "C0PAYENG01", "text": "x"}).json()["ok"]


def test_viewer_page_renders(client: TestClient) -> None:
    client.post("/api/chat.postMessage", headers=AUTH, json={
        "channel": "D0ALICE01", "text": "fallback <b>",
        "blocks": [{"type": "header", "text": {"type": "plain_text", "text": "PR <#8> ready"}},
                   {"type": "section", "text": {"type": "mrkdwn", "text": "*Bold* &lt;x&gt; <http://a.test/?q=1&amp;r=2|link>"}},
                   {"type": "section", "fields": [{"type": "mrkdwn", "text": "*Diff*\n3 files"}]},
                   {"type": "context", "elements": [{"type": "mrkdwn", "text": "ctx"}]},
                   {"type": "actions", "elements": [{"type": "button", "action_id": "open_0", "style": "primary",
                                                     "text": {"type": "plain_text", "text": "Review"},
                                                     "url": "http://a.test/pr/8"}]}]})
    page = client.get("/").text
    assert "PR &lt;#8&gt; ready" in page and "<b>Bold</b>" in page and "&lt;x&gt;" in page
    assert 'href="http://a.test/?q=1&amp;r=2"' in page and "DM @alice" in page and "<script" not in page


def test_mrkdwn_mentions() -> None:
    out = mrkdwn("hi <@U1> in <#C1|eng> <javascript:alert(1)|x>", {"U1": {"name": "alice"}})
    assert "@alice" in out and "#eng" in out and 'href="#"' in out


class _Sink(BaseHTTPRequestHandler):
    got: list[tuple[dict[str, str], bytes]] = []

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Sink.got.append((dict(self.headers), body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"text":"ack"}')

    def log_message(self, *a: object) -> None:
        pass


def test_signed_click_forwarding() -> None:
    srv = HTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/slack/interactions"
    try:
        c = TestClient(create_app(Workspace.load(str(WS_YAML)), bot_token=TOKEN, signing_secret=SECRET,
                                  interactivity_url=url))
        c.post("/api/chat.postMessage", headers=AUTH, json={"channel": "D0BOB0001", "text": "gate", "blocks": [
            {"type": "actions", "block_id": "decide", "elements": [
                {"type": "button", "action_id": "gate_approve", "value": "v1", "text": {"type": "plain_text", "text": "Approve"}}]}]})
        rec = c.get("/_mock/messages").json()["messages"][0]
        assert "/_mock/click" in c.get("/").text
        r = c.post("/_mock/click", data={"record_id": rec["id"], "action_id": "gate_approve"}, follow_redirects=False)
        assert r.status_code == 303
        headers, body = _Sink.got[-1]
        ts = headers["X-Slack-Request-Timestamp"]
        expect = "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
        assert headers["X-Slack-Signature"] == expect
        payload = json.loads(urllib.parse.parse_qs(body.decode())["payload"][0])
        assert payload["type"] == "block_actions" and payload["user"]["id"] == "U0BOB0001"
        assert payload["actions"][0]["value"] == "v1" and payload["container"]["message_ts"] == rec["ts"]
        assert "HTTP 200" in c.get("/").text
    finally:
        srv.shutdown()


def test_sign_matches_slack_example() -> None:
    # Example from Slack's "Verifying requests" docs.
    body = (b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V&"
            b"channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect&text=&"
            b"response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN&"
            b"trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c")
    h = sign("8f742231b10e8888abcd99yyyzzz85a5", body, 1531420618)
    assert h["X-Slack-Signature"] == "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"


def test_cli_build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "tok").write_text(TOKEN + "\n")
    monkeypatch.setenv("SLACK_MOCK_CONFIG", str(WS_YAML))
    monkeypatch.setenv("SLACK_MOCK_BOT_TOKEN_FILE", str(tmp_path / "tok"))
    monkeypatch.delenv("SLACK_MOCK_SIGNING_SECRET_FILE", raising=False)
    from slack_mock.cli import build_app
    c = TestClient(build_app())
    assert c.get("/healthz").json()["status"] == "ok"
    assert c.post("/api/auth.test", headers=AUTH).json()["ok"]
    assert os.environ["SLACK_MOCK_CONFIG"]


def test_views_publish_and_home(client: TestClient) -> None:
    h = {"Authorization": f"Bearer {TOKEN}"}
    view = {"type": "home", "blocks": [{"type": "header", "text": {"type": "plain_text", "text": "Waiting on you"}}]}
    assert client.post("/api/views.publish", json={"user_id": "U0ALICE01", "view": view}, headers=h).json()["ok"]
    assert client.post("/api/views.publish", json={"user_id": "U0NOPE", "view": view}, headers=h).json()["error"] == \
        "user_not_found"
    got = client.get("/_mock/home/U0ALICE01").json()
    assert got["published"] == 1 and got["view"]["blocks"][0]["text"]["text"] == "Waiting on you"


def test_click_json_without_target_reports_error(client: TestClient) -> None:
    assert client.post("/_mock/click", json={"channel": "D0ALICE01", "ts": "1.0", "action_id": "x"}).status_code == 404
