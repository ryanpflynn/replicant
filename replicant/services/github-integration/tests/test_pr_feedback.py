"""PR review feedback ingestion (D-15): who counts, commands, payload and signal. No DB: the DB lookups are stubbed."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from replicant_scm.gitea import normalize
from github_integration.config import Settings
from github_integration.consumer import Consumer, parse_commands

JOB = "job_01m5z3xz399c11r2jh39kz69de"
REPO = {"id": 1, "name": "ledger-api", "full_name": "acme-payments/ledger-api", "owner": {"login": "acme-payments"}}
REPO_ROW = {"repo_id": "1", "installation_id": 1, "tenant_id": "payments", "full_name": "acme-payments/ledger-api",
            "settings": {}}
USERS = {"carol": 5, "zed": 8, "alice": 3, "replicant-bot": 2}


class FakeBroker:
    async def token(self, *a: Any, **kw: Any) -> str:
        return "t"


class FakeS3:
    def __init__(self) -> None:
        self.puts: dict[str, Any] = {}

    async def put(self, bucket: str, key: str, body: bytes) -> None:
        self.puts[key] = json.loads(body)


class FakeTemporal:
    def __init__(self) -> None:
        self.signals: list[tuple[str, str, dict]] = []

    async def signal(self, wf: str, name: str, arg: dict) -> None:
        self.signals.append((wf, name, arg))


def gitea_api(req: httpx.Request) -> httpx.Response:
    p = req.url.path.removeprefix("/api/v1")
    if p.endswith("/pulls/7/reviews"):
        return httpx.Response(200, json=[{"id": 4, "user": {"id": 5}, "body": "please fix", "comments_count": 1,
                                          "html_url": "u"}, {"id": 3, "user": {"id": 5}, "comments_count": 0}])
    if p.endswith("/reviews/4/comments"):
        return httpx.Response(200, json=[{"id": 91, "body": "use Prorate here\n/replicant no-memory", "path": "a.go",
                                          "position": 12, "html_url": "c"}])
    if p.endswith("/repos/acme-payments/ledger-api/teams"):
        return httpx.Response(200, json=[{"id": 2, "units_map": {"repo.code": "write"}, "permission": "none"}])
    if p.startswith("/teams/2/members/"):
        return httpx.Response(204 if p.endswith("/carol") else 404)
    if "/collaborators/" in p:
        return httpx.Response(404)
    if p.endswith("/comments") and req.method == "POST":
        return httpx.Response(201, json={"id": 500})
    return httpx.Response(404)


@pytest.fixture
def consumer() -> Consumer:
    c = Consumer(Settings(s3_endpoint="http://s3"), engine=None, cp=None, broker=FakeBroker(), s3=FakeS3(),  # type: ignore[arg-type]
                 temporal=FakeTemporal(), gitea_transport=httpx.MockTransport(gitea_api))
    links = {5: "usr_carol", 8: None}

    async def job_for_pr(repo: dict, number: int, head_ref: str | None) -> str | None:
        return JOB if number == 7 else None

    async def linked(uid: int) -> str | None:
        return links.get(uid)

    replies: list[str] = []

    async def unlinked(ev: Any, repo: dict, number: int, job_id: str) -> None:
        replies.append(ev.sender.login)

    c._job_for_pr = job_for_pr  # type: ignore[method-assign]
    c.linked_principal = linked  # type: ignore[method-assign]
    c._pr_unlinked_reply = unlinked  # type: ignore[method-assign]
    c.unlinked_replies = replies  # type: ignore[attr-defined]
    return c


def review_ev(login: str, body: str = "please fix", number: int = 7):  # noqa: ANN201
    return normalize({"X-Gitea-Event": "pull_request_comment", "X-Gitea-Event-Type": "pull_request_review_comment",
                      "X-Gitea-Delivery": f"d-{login}-{number}"},
                     {"action": "reviewed", "repository": REPO, "sender": {"id": USERS.get(login, 99), "login": login},
                      "review": {"content": body}, "pull_request": {"number": number, "state": "open",
                                                                     "head": {"ref": f"replicant/{JOB}"}, "base": {}}},
                     host="git.replicant.localhost:3300")


def comment_ev(login: str, body: str):  # noqa: ANN201
    return normalize({"X-Gitea-Event": "issue_comment", "X-Gitea-Event-Type": "pull_request_comment",
                      "X-Gitea-Delivery": f"c-{login}"},
                     {"action": "created", "repository": REPO, "sender": {"id": USERS.get(login, 99), "login": login},
                      "issue": {"number": 7, "title": "x", "pull_request": {}, "html_url": "h"},
                      "comment": {"id": 77, "body": body, "user": {"id": USERS.get(login, 99), "login": login}}},
                     host="git.replicant.localhost:3300")


def test_parse_commands() -> None:
    assert parse_commands("fix it\n/replicant no-memory") == {"no_memory": True, "remember": False, "command_only": False}
    assert parse_commands("/replicant remember") == {"no_memory": False, "remember": True, "command_only": True}
    assert parse_commands("plain") == {"no_memory": False, "remember": False, "command_only": False}


async def test_linked_writer_review_is_signalled(consumer: Consumer) -> None:
    out = await consumer._pr_feedback(review_ev("carol"), REPO_ROW, None)
    assert out == "pr_feedback_signalled"
    (wf, name, sig), = consumer.temporal.signals  # type: ignore[attr-defined]
    assert (wf, name, sig["event"]) == (JOB, "pr.event", "review_submitted")
    assert sig["actor"]["repo_permission"] == "write" and sig["actor"]["principal_id"] == "usr_carol"
    assert {r["comment_id"] for r in sig["comment_refs"]} == {4, 91}
    doc = consumer.s3.puts[f"tenants/payments/jobs/{JOB}/events/pr/d-carol-7.json"]  # type: ignore[attr-defined]
    kinds = {c["comment_id"]: (c["kind"], c["no_memory"]) for c in doc["comments"]}
    assert kinds == {4: ("review_body", False), 91: ("review_comment", True)}
    assert sig["payload_uri"].endswith("/events/pr/d-carol-7.json")


async def test_unlinked_reply_and_ignored(consumer: Consumer) -> None:
    assert await consumer._pr_feedback(review_ev("zed"), REPO_ROW, None) == "ignored_unlinked"
    assert consumer.unlinked_replies == ["zed"] and not consumer.temporal.signals  # type: ignore[attr-defined]


async def test_linked_without_write_is_ignored(consumer: Consumer) -> None:
    async def linked(uid: int) -> str | None:
        return "usr_alice"
    consumer.linked_principal = linked  # type: ignore[method-assign]
    assert await consumer._pr_feedback(review_ev("alice"), REPO_ROW, None) == "ignored_no_write"
    assert not consumer.temporal.signals  # type: ignore[attr-defined]


async def test_not_a_job_pr_is_ignored(consumer: Consumer) -> None:
    assert await consumer._pr_feedback(review_ev("carol", number=8), REPO_ROW, None) == "ignored_not_job_pr"


async def test_bot_is_ignored_before_routing(consumer: Consumer) -> None:
    ev = review_ev("replicant-bot")
    assert ev.sender.is_bot and consumer._is_pr_feedback(ev)


async def test_pr_comment_and_command_only(consumer: Consumer) -> None:
    assert await consumer._pr_feedback(comment_ev("carol", "rename x"), REPO_ROW, None) == "pr_feedback_signalled"
    assert consumer.temporal.signals[-1][2]["event"] == "issue_comment_created"  # type: ignore[attr-defined]
    ev = normalize({"X-Gitea-Event": "issue_comment", "X-Gitea-Event-Type": "pull_request_comment",
                    "X-Gitea-Delivery": "c-cmd"},
                   {"action": "created", "repository": REPO, "sender": {"id": 5, "login": "carol"},
                    "issue": {"number": 7, "title": "x", "pull_request": {}},
                    "comment": {"id": 78, "body": "/replicant no-memory", "user": {"id": 5, "login": "carol"}}},
                   host="git.replicant.localhost:3300")
    assert await consumer._pr_feedback(ev, REPO_ROW, None) == "pr_feedback_signalled"
    assert consumer.temporal.signals[-1][2]["event"] == "replicant_command"  # type: ignore[attr-defined]
