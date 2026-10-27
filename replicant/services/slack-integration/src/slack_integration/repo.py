"""Database access (login svc_slack_integration). Kept behind a small class so tests can use an in-memory fake."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from slack_integration.config import Settings, read_secret


@dataclass(frozen=True)
class Post:
    delivery_id: str
    principal_id: str
    slack_user_id: str
    channel_id: str
    message_ts: str
    event_key: str
    blocks: list[dict[str, Any]] | None = field(default=None, compare=False)

    @property
    def external_ref(self) -> str:
        return f"{self.channel_id}:{self.message_ts}"


@dataclass
class MessageRef:
    gate_id: str
    channel_id: str
    message_ts: str
    purpose: str
    packet_revision: int
    presented_digest: str
    final_state: str | None = None
    blocks: list[dict[str, Any]] | None = None


@dataclass
class Pending:
    decision_id: str
    gate_id: str
    principal_id: str
    slack_user_id: str
    decision: str
    presented_digest: str
    comment: str | None
    channel_id: str
    message_ts: str
    attempts: int = 0
    state: str = "pending"


class Repo(Protocol):
    async def ping(self) -> None: ...
    async def slack_user_for(self, principal_id: str, team_id: str | None) -> str | None: ...
    async def get_post(self, delivery_id: str) -> Post | None: ...
    async def record_post(self, post: Post) -> None: ...
    async def close(self) -> None: ...
    async def principal_for_slack(self, slack_user_id: str, team_id: str | None) -> tuple[str, str | None] | None: ...
    async def get_thread(self, job_id: str, channel_id: str) -> str | None: ...
    async def put_thread(self, job_id: str, channel_id: str, parent_ts: str) -> str: ...
    async def add_message_ref(self, ref: MessageRef) -> None: ...
    async def message_refs(self, gate_id: str) -> list[MessageRef]: ...
    async def set_final(self, channel_id: str, message_ts: str, final_state: str) -> None: ...
    async def first_interaction(self, team_id: str, user_id: str, action_ts: str) -> bool: ...
    async def put_pending(self, p: Pending, response_url: str | None) -> None: ...
    async def due_pending(self, limit: int = 20) -> list[Pending]: ...
    async def finish_pending(self, decision_id: str, state: str, result: dict[str, Any] | None) -> None: ...


def make_engine(s: Settings) -> AsyncEngine:
    if not s.db_dsn:
        raise SystemExit("REPLICANT_DB_DSN is required")
    url = make_url(s.db_dsn)
    pw = read_secret(s.db_password_file)
    if pw:
        url = url.set(password=pw)
    return create_async_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True, pool_timeout=5,
                               connect_args={"application_name": "slack-integration", "connect_timeout": 5})


# Active slack link of the principal; the link in the bot's own workspace (auth.test team_id) wins, otherwise any.
_SQL_LINK = text(
    "SELECT external_id FROM identity.links WHERE principal_id = :p AND kind = 'slack' AND status = 'active' "
    "ORDER BY (namespace = :t) IS TRUE DESC, verified_at DESC NULLS LAST, created_at DESC LIMIT 1")
_SQL_GET = text(
    "SELECT delivery_id, principal_id, slack_user_id, channel_id, message_ts, event_key, blocks "
    "FROM slack_app.notification_posts WHERE delivery_id = :d")
_SQL_PUT = text(
    "INSERT INTO slack_app.notification_posts (delivery_id, principal_id, slack_user_id, channel_id, message_ts, "
    "event_key, blocks) VALUES (:delivery_id, :principal_id, :slack_user_id, :channel_id, :message_ts, :event_key, "
    "CAST(:blocks AS jsonb)) "
    "ON CONFLICT (delivery_id) DO NOTHING")


class PgRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def ping(self) -> None:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def slack_user_for(self, principal_id: str, team_id: str | None) -> str | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(_SQL_LINK, {"p": principal_id, "t": team_id or ""})).first()
        return str(row[0]) if row else None

    async def get_post(self, delivery_id: str) -> Post | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(_SQL_GET, {"d": delivery_id})).mappings().first()
        return Post(**row) if row else None

    async def record_post(self, post: Post) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(_SQL_PUT, {**post.__dict__, "blocks": json.dumps(post.blocks) if post.blocks else None})

    async def principal_for_slack(self, slack_user_id: str, team_id: str | None) -> tuple[str, str | None] | None:
        """Reverse identity link (identity.md I-5): Slack user -> (principal_id, login)."""
        async with self.engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT principal_id, login FROM identity.links WHERE kind = 'slack' AND external_id = :u AND "
                "status = 'active' ORDER BY (namespace = :t) IS TRUE DESC, created_at DESC LIMIT 1"),
                {"u": slack_user_id, "t": team_id or ""})).first()
        return (row.principal_id, row.login) if row else None

    async def get_thread(self, job_id: str, channel_id: str) -> str | None:
        async with self.engine.connect() as conn:
            return (await conn.execute(text("SELECT parent_ts FROM slack_app.job_threads WHERE job_id = :j AND "
                                            "channel_id = :c"), {"j": job_id, "c": channel_id})).scalar()

    async def put_thread(self, job_id: str, channel_id: str, parent_ts: str) -> str:
        async with self.engine.begin() as conn:
            await conn.execute(text("INSERT INTO slack_app.job_threads (job_id, channel_id, parent_ts) VALUES "
                                    "(:j, :c, :t) ON CONFLICT DO NOTHING"), {"j": job_id, "c": channel_id, "t": parent_ts})
            return (await conn.execute(text("SELECT parent_ts FROM slack_app.job_threads WHERE job_id = :j AND "
                                            "channel_id = :c"), {"j": job_id, "c": channel_id})).scalar_one()

    async def add_message_ref(self, ref: MessageRef) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO slack_app.message_refs (gate_id, channel_id, message_ts, purpose, packet_revision, "
                "presented_digest) VALUES (:g, :c, :t, :p, :r, :d) ON CONFLICT DO NOTHING"),
                {"g": ref.gate_id, "c": ref.channel_id, "t": ref.message_ts, "p": ref.purpose,
                 "r": ref.packet_revision, "d": ref.presented_digest})

    async def message_refs(self, gate_id: str) -> list[MessageRef]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT m.gate_id, m.channel_id, m.message_ts, m.purpose, m.packet_revision, m.presented_digest, "
                "m.final_state, p.blocks FROM slack_app.message_refs m LEFT JOIN slack_app.notification_posts p ON "
                "p.channel_id = m.channel_id AND p.message_ts = m.message_ts WHERE m.gate_id = :g AND NOT m.dead"),
                {"g": gate_id})).mappings().all()
        return [MessageRef(**dict(r)) for r in rows]

    async def ref_by_message(self, channel_id: str, message_ts: str) -> MessageRef | None:
        async with self.engine.connect() as conn:
            r = (await conn.execute(text(
                "SELECT m.gate_id, m.channel_id, m.message_ts, m.purpose, m.packet_revision, m.presented_digest, "
                "m.final_state, p.blocks FROM slack_app.message_refs m LEFT JOIN slack_app.notification_posts p ON "
                "p.channel_id = m.channel_id AND p.message_ts = m.message_ts WHERE m.channel_id = :c AND "
                "m.message_ts = :t"), {"c": channel_id, "t": message_ts})).mappings().first()
        return MessageRef(**dict(r)) if r else None

    async def set_final(self, channel_id: str, message_ts: str, final_state: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("UPDATE slack_app.message_refs SET final_state = :f, updated_at = now() WHERE "
                                    "channel_id = :c AND message_ts = :t"), {"f": final_state, "c": channel_id,
                                                                             "t": message_ts})

    async def first_interaction(self, team_id: str, user_id: str, action_ts: str) -> bool:
        async with self.engine.begin() as conn:
            r = (await conn.execute(text(
                "INSERT INTO slack_app.interaction_dedupe (team_id, user_id, action_ts) VALUES (:t, :u, :a) "
                "ON CONFLICT DO NOTHING RETURNING action_ts"), {"t": team_id, "u": user_id, "a": action_ts})).first()
        return r is not None

    async def put_pending(self, p: Pending, response_url: str | None) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO slack_app.pending_decisions (decision_id, gate_id, principal_id, slack_user_id, decision, "
                "presented_digest, comment, response_url, response_url_expires_at, result) VALUES (:d, :g, :p, :u, :dec, "
                ":dig, :c, :r, now() + interval '30 minutes', CAST(:res AS jsonb)) ON CONFLICT DO NOTHING"),
                {"d": p.decision_id, "g": p.gate_id, "p": p.principal_id, "u": p.slack_user_id, "dec": p.decision,
                 "dig": p.presented_digest, "c": p.comment, "r": response_url,
                 "res": json.dumps({"channel_id": p.channel_id, "message_ts": p.message_ts})})

    async def due_pending(self, limit: int = 20) -> list[Pending]:
        async with self.engine.begin() as conn:
            rows = (await conn.execute(text(
                "UPDATE slack_app.pending_decisions SET next_attempt_at = now() + interval '30 seconds' WHERE "
                "decision_id IN (SELECT decision_id FROM slack_app.pending_decisions WHERE state IN ('pending', 'retry') "
                "AND next_attempt_at <= now() AND created_at < now() - interval '3 seconds' ORDER BY next_attempt_at "
                "LIMIT :n FOR UPDATE SKIP LOCKED) RETURNING *"), {"n": limit})).mappings().all()
        out = []
        for r in rows:
            loc = r["result"] or {}
            out.append(Pending(r["decision_id"], r["gate_id"], r["principal_id"], r["slack_user_id"], r["decision"],
                               r["presented_digest"], r["comment"], loc.get("channel_id", ""), loc.get("message_ts", ""),
                               r["attempts"], r["state"]))
        return out

    async def finish_pending(self, decision_id: str, state: str, result: dict[str, Any] | None) -> None:
        async with self.engine.begin() as conn:
            if state == "retry":
                await conn.execute(text(
                    "UPDATE slack_app.pending_decisions SET state = CASE WHEN attempts >= 60 THEN 'dead' ELSE 'retry' END, "
                    "attempts = attempts + 1, next_attempt_at = now() + make_interval(secs => least(60, 5 * (attempts + 1))) "
                    "WHERE decision_id = :d"), {"d": decision_id})
            else:
                await conn.execute(text(
                    "UPDATE slack_app.pending_decisions SET state = :s, attempts = attempts + 1, result = "
                    "coalesce(result, '{}'::jsonb) || CAST(:r AS jsonb) WHERE decision_id = :d"),
                    {"s": state, "r": json.dumps(result or {}), "d": decision_id})

    async def close(self) -> None:
        await self.engine.dispose()
