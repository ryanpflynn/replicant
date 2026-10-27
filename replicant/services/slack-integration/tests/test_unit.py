from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

import pytest
from fastapi.testclient import TestClient
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from conftest import (CALLER_TOKEN, DELIVERY, PRINCIPAL, SIGNING_SECRET, FakeRepo, FakeSlack, deliver_body)
from slack_integration import signing
from slack_integration.app import NOT_LOCAL_TEXT, create_app
from slack_integration.blocks import DeliverRequest, build_blocks, escape, fallback_text, truncate_mrkdwn
from slack_integration.config import Settings
from slack_integration.slack import SlackFailure, classify

AUTH = {"Authorization": f"Bearer {CALLER_TOKEN}"}


# --- signing -------------------------------------------------------------------------------------------------------
def test_signature_slack_docs_example() -> None:
    body = (b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V&"
            b"channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect&text=&"
            b"response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN&"
            b"trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c")
    sig = "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"
    secret = "8f742231b10e8888abcd99yyyzzz85a5"
    assert signing.compute_signature(secret, "1531420618", body) == sig
    h = {"x-slack-request-timestamp": "1531420618", "x-slack-signature": sig}
    assert signing.verify([secret], h, body, now=1531420618 + 299)
    assert not signing.verify([secret], h, body, now=1531420618 + 301)          # replay window
    assert not signing.verify([secret], h, body, now=1531420618 - 301)          # future-dated
    assert not signing.verify(["other"], h, body, now=1531420618)
    assert signing.verify(["other", secret], h, body, now=1531420618)           # rotation: previous secret accepted
    assert not signing.verify([secret], h, body + b"x", now=1531420618)
    assert not signing.verify([secret], {"x-slack-signature": sig}, body, now=1531420618)
    assert not signing.verify([secret], {**h, "x-slack-request-timestamp": "abc"}, body, now=1531420618)
    assert not signing.verify([], h, body, now=1531420618)


def _signed(body: bytes, secret: str = SIGNING_SECRET, ts: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if ts is None else ts
    return {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": signing.compute_signature(secret, str(ts), body)}


# --- blocks --------------------------------------------------------------------------------------------------------
def test_blocks_shape() -> None:
    req = DeliverRequest.model_validate(deliver_body())
    blocks = build_blocks(req)
    assert [b["type"] for b in blocks] == ["header", "section", "section", "context", "actions"]
    assert blocks[0]["text"] == {"type": "plain_text", "text": "PR #8 is ready for review", "emoji": True}
    assert blocks[1]["text"]["type"] == "mrkdwn" and blocks[1]["text"]["text"] == req.text      # passed through
    assert blocks[2]["fields"][0]["text"] == "*Diff*\n3 files · +41 −12"
    btns = blocks[4]["elements"]
    assert [b["action_id"] for b in btns] == ["open_0", "open_1"]
    assert btns[0]["style"] == "primary" and "style" not in btns[1]
    assert fallback_text(req).startswith("PR #8 is ready for review\n*<http://")


def test_blocks_minimal() -> None:
    req = DeliverRequest.model_validate({"delivery_id": DELIVERY, "event_key": "job.succeeded", "recipient": PRINCIPAL,
                                         "title": "Done"})
    assert [b["type"] for b in build_blocks(req)] == ["header"]
    assert fallback_text(req) == "Done"


def test_blocks_within_block_kit_limits() -> None:
    long = "x" * 5000
    link = "<http://example.test/" + "p" * 80 + "|label>"
    req = DeliverRequest.model_validate({
        "delivery_id": DELIVERY, "event_key": "gate.reminder", "recipient": PRINCIPAL,
        "title": "T & <b> " + long[:1900], "text": (link + " ") * 200,
        "fields": [{"label": "L<&>" + long[:400], "value": long} for _ in range(30)],
        "context": long * 8,
        "links": [{"label": f"Button {i} " + long[:200], "url": f"https://example.test/{i}", "primary": True}
                  for i in range(20)] + [{"label": "bad", "url": "javascript:alert(1)"}],
    })
    blocks = build_blocks(req)
    assert len(blocks) <= 50
    for b in blocks:
        assert len(b.get("block_id", "")) <= 255
        if b["type"] == "header":
            assert b["text"]["type"] == "plain_text" and 1 <= len(b["text"]["text"]) <= 150
        if b["type"] == "section":
            assert "text" in b or "fields" in b
            if "text" in b:
                assert 1 <= len(b["text"]["text"]) <= 3000
                # truncation never leaves a dangling link
                assert b["text"]["text"].count("<") == b["text"]["text"].count(">")
            if "fields" in b:
                assert 1 <= len(b["fields"]) <= 10
                assert all(1 <= len(f["text"]) <= 2000 for f in b["fields"])
                assert all("<&>" not in f["text"] and "L&lt;&amp;&gt;" in f["text"] for f in b["fields"])
        if b["type"] == "context":
            assert 1 <= len(b["elements"]) <= 10 and all(len(e["text"]) <= 3000 for e in b["elements"])
        if b["type"] == "actions":
            els = b["elements"]
            assert 1 <= len(els) <= 5
            assert len({e["action_id"] for e in els}) == len(els)
            assert all(len(e["action_id"]) <= 255 and len(e["text"]["text"]) <= 75 and len(e["url"]) <= 3000 for e in els)
            assert all(e["url"].startswith("https://") for e in els)
            assert sum(1 for e in els if e.get("style") == "primary") == 1
    fb = fallback_text(req)
    assert len(fb) <= 3000 and fb.startswith("T &amp; &lt;b&gt; ")
    assert len(json.dumps(blocks)) < 50_000


def test_escape_and_truncate() -> None:
    assert escape("a & b <c> &amp; &lt;") == "a &amp; b &lt;c&gt; &amp; &lt;"
    assert truncate_mrkdwn("abc", 3) == "abc"
    assert truncate_mrkdwn("hello <http://x.test|link> end", 12) == "hello…"
    assert truncate_mrkdwn("a &amp; b", 5) == "a…"


# --- error classification ------------------------------------------------------------------------------------------
def _api_error(status: int, data: dict[str, Any], headers: dict[str, str] | None = None) -> SlackApiError:
    resp = AsyncSlackResponse(client=None, http_verb="POST", api_url="http://x/api/chat.postMessage", req_args={},
                              data=data, headers=headers or {}, status_code=status)
    return SlackApiError("err", resp)


@pytest.mark.parametrize(("status", "data", "headers", "kind", "code", "retry"), [
    (429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "30"}, "ratelimited", "ratelimited", 30),
    (429, {}, {}, "ratelimited", "ratelimited", 1),
    (200, {"ok": False, "error": "channel_not_found"}, None, "permanent", "channel_not_found", None),
    (200, {"ok": False, "error": "user_not_found"}, None, "permanent", "user_not_found", None),
    (200, {"ok": False, "error": "invalid_auth"}, None, "config", "invalid_auth", None),
    (200, {"ok": False, "error": "missing_scope"}, None, "config", "missing_scope", None),
    (200, {"ok": False, "error": "internal_error"}, None, "retryable", "internal_error", None),
    (503, {}, None, "retryable", "http_503", None),
])
def test_classify(status: int, data: dict[str, Any], headers: Any, kind: str, code: str, retry: Any) -> None:
    f = classify(_api_error(status, data, headers))
    assert (f.kind, f.code) == (kind, code)
    if retry is not None:
        assert f.retry_after == retry


def test_classify_connection_errors() -> None:
    import aiohttp
    assert classify(aiohttp.ClientConnectionError("boom")).code == "slack_unreachable"
    assert classify(TimeoutError()).kind == "retryable"


# --- deliver -------------------------------------------------------------------------------------------------------
@pytest.fixture()
def env(settings: Settings) -> tuple[TestClient, FakeRepo, FakeSlack]:
    repo, slack = FakeRepo(), FakeSlack()
    repo.links[(PRINCIPAL, "T0LOCAL01")] = "U0ALICE01"
    app = create_app(settings, repo=repo, slack=slack)
    with TestClient(app) as c:
        yield c, repo, slack


def test_deliver_auth(env: Any, settings: Settings, secrets_dir: Any) -> None:
    c, _, _ = env
    assert c.post("/internal/v1/notifications/deliver", json=deliver_body()).status_code == 401
    assert c.post("/internal/v1/notifications/deliver", json=deliver_body(),
                  headers={"Authorization": "Bearer wrong"}).status_code == 401
    (secrets_dir / "caller").unlink()
    r = c.post("/internal/v1/notifications/deliver", json=deliver_body(), headers=AUTH)
    assert r.status_code == 503 and r.json()["reason"] == "caller_token_not_configured"


def test_deliver_sent_then_duplicate(env: Any) -> None:
    c, repo, slack = env
    r = c.post("/internal/v1/notifications/deliver", json=deliver_body(), headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"state": "sent", "external_ref": "D0ALICE01:1792851600.000001", "slack_user_id": "U0ALICE01"}
    post = slack.posts[0]
    assert post["channel"] == "D0ALICE01" and post["blocks"][0]["type"] == "header"
    assert post["metadata"] == {"event_type": "replicant_notification", "event_payload": {
        "delivery_id": DELIVERY, "event_key": "job.pr_opened", "job_id": "job_" + "b" * 26}}
    assert repo.posts[DELIVERY].message_ts == "1792851600.000001"
    assert repo.link_calls[-1] == (PRINCIPAL, "T0LOCAL01")
    r = c.post("/internal/v1/notifications/deliver", json=deliver_body(), headers=AUTH)
    assert r.json() == {"state": "sent", "external_ref": "D0ALICE01:1792851600.000001", "slack_user_id": "U0ALICE01",
                        "duplicate": True}
    assert len(slack.posts) == 1


def test_deliver_suppressed_without_link(env: Any) -> None:
    c, repo, slack = env
    repo.links.clear()
    r = c.post("/internal/v1/notifications/deliver", json=deliver_body(), headers=AUTH)
    assert r.json() == {"state": "suppressed", "reason": "no_slack_link"} and not slack.posts


def test_deliver_falls_back_to_other_namespace(env: Any) -> None:
    c, repo, _ = env
    repo.links = {(PRINCIPAL, "T0OTHER01"): "U0OTHER01"}
    r = c.post("/internal/v1/notifications/deliver", json=deliver_body(), headers=AUTH)
    assert r.json()["slack_user_id"] == "U0OTHER01"


@pytest.mark.parametrize("body", [
    {"delivery_id": "ntf_bad"}, deliver_body(delivery_id="ntf_" + "I" * 26), deliver_body(title=""),
    deliver_body(fields="x"), deliver_body(links=[{"label": "x"}]), {k: v for k, v in deliver_body().items() if k != "recipient"},
])
def test_deliver_malformed(env: Any, body: dict[str, Any]) -> None:
    c, _, slack = env
    r = c.post("/internal/v1/notifications/deliver", json=body, headers=AUTH)
    assert r.status_code == 422 and r.json()["reason"] == "invalid_request" and not slack.posts


def test_deliver_not_json(env: Any) -> None:
    c, _, _ = env
    r = c.post("/internal/v1/notifications/deliver", content=b"{nope", headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 422


@pytest.mark.parametrize(("failure", "status", "reason", "retryable"), [
    (SlackFailure("ratelimited", "ratelimited", 12), 429, "slack_ratelimited", True),
    (SlackFailure("permanent", "user_not_found"), 422, "slack_user_not_found", False),
    (SlackFailure("config", "invalid_auth"), 503, "slack_invalid_auth", True),
    (SlackFailure("retryable", "slack_unreachable"), 502, "slack_slack_unreachable", True),
])
def test_deliver_slack_errors(env: Any, failure: SlackFailure, status: int, reason: str, retryable: bool) -> None:
    c, repo, slack = env
    slack.fail = failure
    r = c.post("/internal/v1/notifications/deliver", json=deliver_body(), headers=AUTH)
    assert r.status_code == status and r.json()["reason"] == reason and r.json()["retryable"] is retryable
    if status == 429:
        assert r.headers["retry-after"] == "12" and r.json()["retry_after"] == 12
    assert not repo.posts


def test_deliver_db_errors(env: Any) -> None:
    c, repo, slack = env
    for mode in ("get", "link"):
        repo.fail = mode
        r = c.post("/internal/v1/notifications/deliver", json=deliver_body(), headers=AUTH)
        assert r.status_code == 503 and r.json()["reason"] == "db_unavailable"
    assert not slack.posts
    repo.fail = "record"
    r = c.post("/internal/v1/notifications/deliver", json=deliver_body(), headers=AUTH)
    assert r.status_code == 200 and r.json()["state"] == "sent" and r.json()["recorded"] is False


# --- readiness -----------------------------------------------------------------------------------------------------
def test_health_and_ready(env: Any) -> None:
    c, repo, _ = env
    assert c.get("/healthz").json() == {"status": "ok"}
    r = c.get("/readyz")
    assert r.status_code == 200 and r.json()["db"] == "ok"
    assert r.json()["slack"] == {"status": "ok", "team_id": "T0LOCAL01"}
    repo.fail = "ping"
    assert c.get("/readyz").status_code == 503


def test_ready_when_slack_down(settings: Settings) -> None:
    slack = FakeSlack()
    slack.auth_fail = SlackFailure("retryable", "slack_unreachable")
    with TestClient(create_app(settings, repo=FakeRepo(), slack=slack)) as c:
        r = c.get("/readyz")
        assert r.status_code == 200 and r.json()["slack"]["status"] in ("unknown", "unreachable")


# --- Slack ingress -------------------------------------------------------------------------------------------------
def test_events(env: Any) -> None:
    c, _, _ = env
    body = json.dumps({"type": "url_verification", "challenge": "abc123", "token": "x"}).encode()
    r = c.post("/slack/events", content=body, headers={**_signed(body), "Content-Type": "application/json"})
    assert r.status_code == 200 and r.json() == {"challenge": "abc123"}
    body = json.dumps({"type": "event_callback", "event": {"type": "app_home_opened"}}).encode()
    assert c.post("/slack/events", content=body, headers=_signed(body)).status_code == 200
    assert c.post("/slack/events", content=body, headers=_signed(body, secret="wrong")).status_code == 401
    assert c.post("/slack/events", content=body, headers=_signed(body, ts=int(time.time()) - 301)).status_code == 401
    assert c.post("/slack/events", content=body).status_code == 401


def test_interactions_and_commands(env: Any) -> None:
    c, _, _ = env
    payload = {"type": "shortcut", "user": {"id": "U0BOB0001"}, "callback_id": "replicant_job_new"}
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    hdr = {"Content-Type": "application/x-www-form-urlencoded"}
    r = c.post("/slack/interactions", content=body, headers={**_signed(body), **hdr})
    assert r.status_code == 200 and r.json()["response_type"] == "ephemeral" and r.json()["text"] == NOT_LOCAL_TEXT
    assert c.post("/slack/interactions", content=body, headers=hdr).status_code == 401
    body = urllib.parse.urlencode({"command": "/replicant", "text": "status", "user_id": "U0BOB0001"}).encode()
    r = c.post("/slack/commands", content=body, headers={**_signed(body), **hdr})
    assert r.status_code == 200 and "Open the approval in Replicant" in r.json()["text"]


def test_ingress_without_secret_configured(env: Any, secrets_dir: Any) -> None:
    c, _, _ = env
    (secrets_dir / "signing").unlink()
    body = b"{}"
    assert c.post("/slack/events", content=body, headers=_signed(body)).status_code == 401
