from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from slack_integration.config import Settings
from slack_integration.repo import Post
from slack_integration.slack import SlackFailure

CALLER_TOKEN = "notify-caller-token-test"
BOT_TOKEN = "xoxb-test-bot-token"
SIGNING_SECRET = "test-signing-secret"
PRINCIPAL = "usr_" + "a" * 26
DELIVERY = "ntf_" + "0123456789abcdefghjkmnpqrs"


class FakeRepo:
    def __init__(self) -> None:
        self.links: dict[tuple[str, str], str] = {}
        self.posts: dict[str, Post] = {}
        self.fail: str | None = None          # "ping" | "get" | "link" | "record"
        self.link_calls: list[tuple[str, str | None]] = []

    async def ping(self) -> None:
        if self.fail == "ping":
            raise ConnectionError("db down")

    async def slack_user_for(self, principal_id: str, team_id: str | None) -> str | None:
        self.link_calls.append((principal_id, team_id))
        if self.fail == "link":
            raise ConnectionError("db down")
        exact = self.links.get((principal_id, team_id or ""))
        if exact:
            return exact
        return next((v for (p, _), v in self.links.items() if p == principal_id), None)

    async def get_post(self, delivery_id: str) -> Post | None:
        if self.fail == "get":
            raise ConnectionError("db down")
        return self.posts.get(delivery_id)

    async def record_post(self, post: Post) -> None:
        if self.fail == "record":
            raise ConnectionError("db down")
        self.posts.setdefault(post.delivery_id, post)

    async def close(self) -> None:
        pass

    # --- phase 2 (threads, refs, decisions) ---
    threads: dict[tuple[str, str], str]
    refs: list[Any]
    seen: set[tuple[str, str, str]]
    pending: dict[str, Any]

    def _p2(self) -> None:
        for k, v in (("threads", {}), ("refs", []), ("seen", set()), ("pending", {}), ("principals", {})):
            if not hasattr(self, k) or getattr(self, k) is None:
                setattr(self, k, v)

    async def principal_for_slack(self, slack_user_id: str, team_id: str | None) -> Any:
        self._p2()
        return self.principals.get(slack_user_id)

    async def get_thread(self, job_id: str, channel_id: str) -> str | None:
        self._p2()
        return self.threads.get((job_id, channel_id))

    async def put_thread(self, job_id: str, channel_id: str, parent_ts: str) -> str:
        self._p2()
        return self.threads.setdefault((job_id, channel_id), parent_ts)

    async def add_message_ref(self, ref: Any) -> None:
        self._p2()
        self.refs.append(ref)

    async def message_refs(self, gate_id: str) -> list[Any]:
        self._p2()
        out = []
        for r in self.refs:
            if r.gate_id == gate_id:
                post = next((p for p in self.posts.values() if p.channel_id == r.channel_id and
                             p.message_ts == r.message_ts), None)
                r.blocks = post.blocks if post else None
                out.append(r)
        return out

    async def ref_by_message(self, channel_id: str, message_ts: str) -> Any:
        self._p2()
        for r in self.refs:
            if r.channel_id == channel_id and r.message_ts == message_ts:
                post = next((p for p in self.posts.values() if p.channel_id == channel_id and
                             p.message_ts == message_ts), None)
                r.blocks = post.blocks if post else None
                return r
        return None

    async def set_final(self, channel_id: str, message_ts: str, final_state: str) -> None:
        self._p2()
        for r in self.refs:
            if r.channel_id == channel_id and r.message_ts == message_ts:
                r.final_state = final_state

    async def first_interaction(self, team_id: str, user_id: str, action_ts: str) -> bool:
        self._p2()
        k = (team_id, user_id, action_ts)
        if k in self.seen:
            return False
        self.seen.add(k)
        return True

    async def put_pending(self, p: Any, response_url: str | None) -> None:
        self._p2()
        self.pending.setdefault(p.decision_id, p)

    async def due_pending(self, limit: int = 20) -> list[Any]:
        self._p2()
        return [p for p in self.pending.values() if p.state in ("pending", "retry")][:limit]

    async def finish_pending(self, decision_id: str, state: str, result: Any) -> None:
        self._p2()
        if decision_id in self.pending:
            self.pending[decision_id].state = state


class FakeSlack:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.fail: SlackFailure | None = None
        self.auth_fail: SlackFailure | None = None

    async def auth_test(self) -> dict[str, Any]:
        if self.auth_fail:
            raise self.auth_fail
        return {"ok": True, "team_id": "T0LOCAL01"}

    async def open_dm(self, user_id: str) -> str:
        if self.fail:
            raise self.fail
        return "D" + user_id[1:]

    async def post_message(self, **kw: Any) -> str:
        self.posts.append(kw)
        return f"1792851600.{len(self.posts):06d}"

    async def update_message(self, **kw: Any) -> None:
        self.__dict__.setdefault("updates", []).append(kw)

    async def post_ephemeral(self, **kw: Any) -> None:
        self.__dict__.setdefault("ephemerals", []).append(kw)

    async def open_view(self, **kw: Any) -> None:
        self.__dict__.setdefault("views", []).append(kw)

    async def publish_home(self, **kw: Any) -> None:
        self.__dict__.setdefault("homes", []).append(kw)


@pytest.fixture()
def secrets_dir(tmp_path: Path) -> Path:
    (tmp_path / "caller").write_text(CALLER_TOKEN + "\n")
    (tmp_path / "bot").write_text(BOT_TOKEN + "\n")
    (tmp_path / "signing").write_text(SIGNING_SECRET + "\n")
    return tmp_path


@pytest.fixture()
def settings(secrets_dir: Path) -> Settings:
    return Settings(db_dsn="postgresql+psycopg://x@127.0.0.1:1/x", notify_caller_token_file=str(secrets_dir / "caller"),
                    slack_bot_token_file=str(secrets_dir / "bot"),
                    slack_signing_secret_file=str(secrets_dir / "signing"))


def deliver_body(**over: Any) -> dict[str, Any]:
    body = {
        "delivery_id": DELIVERY, "event_key": "job.pr_opened", "recipient": PRINCIPAL, "tenant_id": "payments",
        "job_id": "job_" + "b" * 26, "title": "PR #8 is ready for review",
        "text": "*<http://127.0.0.1:3300/acme-payments/ledger-api/pulls/8|Fix stale FX cache>*\n`acme-payments/ledger-api`",
        "fields": [{"label": "Diff", "value": "3 files · +41 −12"}, {"label": "QA", "value": "Passed"}],
        "context": "Replicant · job_bbbb",
        "links": [{"label": "Review PR #8", "url": "http://127.0.0.1:3300/acme-payments/ledger-api/pulls/8", "primary": True},
                  {"label": "Open in Replicant", "url": "http://127.0.0.1:8400/jobs/job_" + "b" * 26}],
    }
    body.update(over)
    return body
