"""Gate decisions from Slack (NOTIFY-D12), threads (NOTIFY-D13) and App Home (NOTIFY-D15) with fakes."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from conftest import FakeRepo, FakeSlack
from slack_integration.blocks import DeliverRequest, build_blocks, home_view, resolved_blocks
from slack_integration.config import Settings
from slack_integration.decisions import Decisions
from slack_integration.repo import MessageRef, Post

GATE = "gate_" + "c" * 26
DIG = "sha256:" + "a" * 64
BOB = "usr_" + "b" * 26
VALUE = json.dumps({"j": "job_" + "d" * 26, "g": GATE, "r": 1, "d": DIG})


def _setup(cp: Any) -> tuple[Decisions, FakeRepo, FakeSlack, list[dict[str, Any]]]:
    repo, slack, sent = FakeRepo(), FakeSlack(), []
    repo._p2()
    repo.principals["U0BOB0001"] = (BOB, "bob")

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        sent.append({"path": req.url.path, "body": body, "idem": req.headers.get("idempotency-key")})
        return cp(body)

    d = Decisions(Settings(control_plane_url="http://cpa"), repo, slack, lambda: "T0LOCAL01",
                  transport=httpx.MockTransport(handler))
    req = DeliverRequest(delivery_id="ntf_" + "0123456789abcdefghjkmnpqrs", event_key="gate.requested",
                         recipient=BOB, title="Publish approval", text="x",
                         actions=[{"action_id": "gate_approve", "label": "Approve", "style": "primary", "value": VALUE},
                                  {"action_id": "gate_reject", "label": "Reject", "style": "danger", "value": VALUE}],
                         gate={"gate_id": GATE, "revision": 1, "presented_digest": DIG},
                         links=[{"label": "Review in Replicant", "url": "http://127.0.0.1:8400/approvals/" + GATE}])
    blocks = build_blocks(req)
    repo.posts[req.delivery_id] = Post(req.delivery_id, BOB, "U0BOB0001", "D0BOB0001", "1.000001", "gate.requested", blocks)
    repo.refs.append(MessageRef(GATE, "D0BOB0001", "1.000001", "gate_dm", 1, DIG))
    return d, repo, slack, sent


def _click(action_id: str, ts: str = "1792851601.000100") -> dict[str, Any]:
    return {"type": "block_actions", "team": {"id": "T0LOCAL01"}, "user": {"id": "U0BOB0001"},
            "container": {"channel_id": "D0BOB0001", "message_ts": "1.000001"}, "trigger_id": "t.1",
            "actions": [{"action_id": action_id, "value": VALUE, "action_ts": ts}]}


def test_gate_buttons_render_with_confirm_and_block_id() -> None:
    req = DeliverRequest(delivery_id="ntf_" + "0123456789abcdefghjkmnpqrs", event_key="gate.requested", recipient=BOB,
                         title="t", actions=[{"action_id": "gate_approve", "label": "Approve", "style": "primary",
                                              "value": VALUE, "confirm": {"title": "Approve?", "text": "x"}}],
                         gate={"gate_id": GATE, "revision": 3, "presented_digest": DIG})
    act = [b for b in build_blocks(req) if b["type"] == "actions"][0]
    assert act["block_id"] == "decide-r3" and act["elements"][0]["confirm"]["confirm"]["text"] == "Confirm"
    out = resolved_blocks(build_blocks(req), "✓ Approved by Bob in Slack")
    assert not any(b["type"] == "actions" for b in out) and out[-1]["block_id"] == "outcome"


@pytest.mark.asyncio
async def test_approve_accepted_updates_message_in_place() -> None:
    d, repo, slack, sent = _setup(lambda b: httpx.Response(200, json={"outcome": "accepted", "gate_id": GATE,
                                                                      "decision_id": b["decision_id"]}))
    await d.handle(_click("gate_approve"))
    assert sent[0]["path"] == f"/internal/v1/gates/{GATE}/decisions"
    b = sent[0]["body"]
    assert b["channel"] == "slack" and b["actor"] == {"principal_id": BOB, "channel_identity":
                                                      {"kind": "slack", "id": "U0BOB0001", "login": "bob"}}
    assert b["presented_digest"] == DIG and sent[0]["idem"] == f'"{b["decision_id"]}"'
    upd = slack.updates[0]
    assert upd["ts"] == "1.000001" and "Approved by Bob in Slack" in upd["text"]
    assert not [x for x in upd["blocks"] if x["type"] == "actions" and any(
        e.get("action_id", "").startswith("gate_") for e in x["elements"])]
    assert repo.refs[0].final_state == "approved"
    await d.handle(_click("gate_approve"))                     # duplicate delivery of the same click
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_already_decided_and_refused_and_unlinked() -> None:
    d, repo, slack, sent = _setup(lambda b: httpx.Response(409, json={
        "code": "gate_already_decided", "winning_decision": {"decision": "approve", "channel": "ui"}}))
    await d.handle(_click("gate_approve"))
    assert "Already approved in the UI" in slack.updates[0]["text"]
    d2, _, slack2, _ = _setup(lambda b: httpx.Response(403, json={"code": "not_in_approver_group"}))
    await d2.handle(_click("gate_approve"))
    assert not getattr(slack2, "updates", []) and "not an eligible approver" in slack2.ephemerals[0]["text"]
    d3, repo3, slack3, sent3 = _setup(lambda b: httpx.Response(500))
    repo3.principals.clear()
    await d3.handle(_click("gate_approve"))
    assert not sent3 and "Link your Slack account" in slack3.ephemerals[0]["text"]


@pytest.mark.asyncio
async def test_reject_opens_modal_then_submission_decides_with_comment() -> None:
    d, repo, slack, sent = _setup(lambda b: httpx.Response(200, json={"outcome": "accepted"}))
    await d.handle(_click("gate_reject"))
    view = slack.views[0]["view"]
    assert view["callback_id"] == "gate_reject_modal" and not sent
    sub = {"type": "view_submission", "user": {"id": "U0BOB0001"}, "team": {"id": "T0LOCAL01"},
           "view": {"id": "V1", "callback_id": "gate_reject_modal", "private_metadata": view["private_metadata"],
                    "state": {"values": {"comment": {"text": {"type": "plain_text_input", "value": "wrong fix"}}}}}}
    await d.handle(sub)
    assert sent[0]["body"]["decision"] == "reject" and sent[0]["body"]["comment"] == "wrong fix"
    assert "Rejected by Bob in Slack" in slack.updates[0]["text"]


@pytest.mark.asyncio
async def test_recording_is_retried_and_gate_resolved_updates_every_ref() -> None:
    calls = {"n": 0}

    def cp(b: dict[str, Any]) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(202, json={"state": "recording"}) if calls["n"] == 1 else \
            httpx.Response(200, json={"outcome": "accepted"})
    d, repo, slack, sent = _setup(cp)
    await d.handle(_click("gate_approve"))
    assert "Recording" in slack.ephemerals[0]["text"]
    assert await d.retry_due() == 1 and "Approved by Bob" in slack.updates[0]["text"]
    d2, repo2, slack2, _ = _setup(lambda b: httpx.Response(200))
    assert await d2.gate_resolved(GATE, "approved", "✓ Approved by Grace in the UI") == 1
    assert await d2.gate_resolved(GATE, "approved", "again") == 0          # idempotent per message


def test_home_view_lists_waiting_items() -> None:
    doc = {"items": [{"kind": "gate", "gate_kind": "publish_approval", "repo": "h/o/ledger-api", "title": "Fix",
                      "risk_level": "low", "href": "/approvals/" + GATE},
                     {"kind": "pr", "repo": "h/o/ledger-api", "title": "Fix", "role": "reviewer",
                      "pr": {"number": 5, "url": "http://127.0.0.1:3300/o/ledger-api/pulls/5"}}],
           "counts": {"gates": 1, "prs": 1, "total": 2}}
    v = home_view(doc, "http://127.0.0.1:8400", linked=True)
    txt = json.dumps(v)
    assert v["type"] == "home" and "PR #5" in txt and "Publish approval" in txt and "1 approvals" in txt
    assert "Link your Slack" in json.dumps(home_view(None, "http://x", linked=False))
