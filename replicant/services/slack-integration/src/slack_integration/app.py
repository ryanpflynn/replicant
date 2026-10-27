"""slack-integration ASGI app (v0).

Routes:
* `GET /healthz` - process up.
* `GET /readyz` - 200 when the database answers `SELECT 1`; the Slack `auth.test` status is reported but does not
  gate readiness (a down slack-mock or Slack outage must not take the service out of rotation: deliveries then fail
  with retryable 502/503 and the control-plane dispatcher retries).
* `POST /internal/v1/notifications/deliver` - NOTIFY contract: bearer auth (REPLICANT_NOTIFY_CALLER_TOKEN_FILE),
  identity.links resolution, conversations.open + chat.postMessage, idempotent per delivery_id
  (slack_app.notification_posts).
* `POST /slack/events|interactions|commands` - v0 signature verification (approvals.md §4.2), 401 otherwise.
  Events: url_verification challenge, everything else acked. Interactions and commands: acked with an ephemeral
  "not available locally yet" answer (gate decisions from Slack are not implemented in v0).
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
import urllib.parse
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from slack_integration import signing
from slack_integration.blocks import (DeliverRequest, build_blocks, fallback_text, home_view, metadata,
                                     thread_parent_blocks)
from slack_integration.decisions import Decisions
from slack_integration.config import Settings, read_secret
from slack_integration.repo import MessageRef, PgRepo, Post, Repo, make_engine
from slack_integration.slack import SlackFailure, SlackGateway

log = logging.getLogger("slack_integration")
MAX_SLACK_BODY = 1024 * 1024
MAX_DELIVER_BODY = 256 * 1024
NOT_LOCAL_TEXT = ("Decisions from Slack are not available locally yet. "
                  "Open the approval in Replicant to decide.")


def _log(event: str, **kw: Any) -> None:
    log.info(json.dumps({"msg": event, **kw}))


class SlackStatus:
    """Cached auth.test outcome (team_id is needed to prefer links in the bot's workspace)."""

    def __init__(self) -> None:
        self.state = "unknown"
        self.team_id: str | None = None
        self.checked_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.state, "team_id": self.team_id}


def _json(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=headers)


def create_app(settings: Settings | None = None, *, repo: Repo | None = None, slack: Any = None,
               auth_check_interval_s: float = 300.0) -> FastAPI:
    s = settings or Settings.from_env()
    repo = repo or PgRepo(make_engine(s))
    slack = slack or SlackGateway(s)
    status = SlackStatus()
    locks: dict[str, asyncio.Lock] = {}
    decisions = Decisions(s, repo, slack, lambda: status.team_id)
    background: set[asyncio.Task[Any]] = set()

    def spawn(coro: Any) -> None:
        """Slack wants interaction acks within 3 s: work runs after the response (approvals.md §4.4)."""
        t = asyncio.create_task(coro)
        background.add(t)
        t.add_done_callback(background.discard)

    async def guarded(name: str, coro: Any) -> None:
        try:
            await coro
        except Exception as e:  # noqa: BLE001 - logged; Slack already got its ack
            log.warning(json.dumps({"msg": f"{name}_failed", "error": f"{type(e).__name__}: {str(e)[:200]}"}))

    async def decision_retrier() -> None:
        while True:
            await asyncio.sleep(s.decision_retry_seconds)
            await guarded("decision_retry", decisions.retry_due())

    async def publish_home(slack_user: str) -> None:
        who = await repo.principal_for_slack(slack_user, status.team_id)
        doc = await decisions.waiting_for(who[0]) if who else None
        await slack.publish_home(user_id=slack_user, view=home_view(doc, s.web_url, linked=who is not None))
        _log("home_published", slack_user_id=slack_user, items=len((doc or {}).get("items") or []))

    async def refresh_auth() -> bool:
        try:
            data = await slack.auth_test()
        except SlackFailure as e:
            status.state = "invalid_auth" if e.kind == "config" else "unreachable"
            status.checked_at = time.time()
            return False
        status.state, status.team_id, status.checked_at = "ok", data.get("team_id"), time.time()
        return True

    async def auth_loop() -> None:
        backoff, last = 2.0, None
        while True:
            ok = await refresh_auth()
            if status.state != last:
                _log("slack_auth_test", status=status.state, team_id=status.team_id)
                last = status.state
            if ok:
                backoff = 2.0
                await asyncio.sleep(auth_check_interval_s)
            else:
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        task = asyncio.create_task(auth_loop())
        retrier = asyncio.create_task(decision_retrier())
        yield
        task.cancel()
        retrier.cancel()
        with contextlib.suppress(Exception):
            await repo.close()

    app = FastAPI(title="slack-integration", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.slack_status = status
    app.state.decisions = decisions

    def caller_ok(request: Request) -> Response | None:
        expected = read_secret(s.notify_caller_token_file)
        if not expected:
            return _json(503, {"state": "error", "reason": "caller_token_not_configured"})
        h = request.headers.get("authorization", "")
        tok = h[7:].strip() if h[:7].lower() == "bearer " else ""
        if not tok or not hmac.compare_digest(tok.encode(), expected.encode()):
            return _json(401, {"state": "error", "reason": "unauthenticated"})
        return None

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        try:
            await asyncio.wait_for(repo.ping(), timeout=2.5)
            db = "ok"
        except Exception as e:  # noqa: BLE001
            db = f"error: {type(e).__name__}"
        body = {"status": "ready" if db == "ok" else "not_ready", "db": db, "slack": status.as_dict()}
        return _json(200 if db == "ok" else 503, body)

    # --- NOTIFY delivery ----------------------------------------------------------------------------------------
    @app.post("/internal/v1/notifications/deliver")
    async def deliver(request: Request) -> Response:
        expected = read_secret(s.notify_caller_token_file)
        if not expected:
            log.error(json.dumps({"msg": "notify caller token not configured"}))
            return _json(503, {"state": "error", "reason": "caller_token_not_configured"})
        h = request.headers.get("authorization", "")
        tok = h[7:].strip() if h[:7].lower() == "bearer " else ""
        if not tok or not hmac.compare_digest(tok.encode(), expected.encode()):
            return _json(401, {"state": "error", "reason": "unauthenticated"})
        raw = await request.body()
        if len(raw) > MAX_DELIVER_BODY:
            return _json(413, {"state": "rejected", "reason": "body_too_large"})
        try:
            req = DeliverRequest.model_validate_json(raw)
        except ValidationError as e:
            errs = [{"loc": ".".join(str(x) for x in er["loc"]), "msg": er["msg"]} for er in e.errors()[:10]]
            return _json(422, {"state": "rejected", "reason": "invalid_request", "errors": errs})

        lock = locks.setdefault(req.delivery_id, asyncio.Lock())
        try:
            async with lock:
                return await _deliver(req)
        finally:
            if not lock.locked() and locks.get(req.delivery_id) is lock:
                locks.pop(req.delivery_id, None)

    async def _deliver(req: DeliverRequest) -> Response:
        ctx = {"delivery_id": req.delivery_id, "event_key": req.event_key, "principal_id": req.recipient}
        try:
            prev = await repo.get_post(req.delivery_id)
        except Exception as e:  # noqa: BLE001
            _log("deliver_failed", **ctx, reason="db_unavailable", error=type(e).__name__)
            return _json(503, {"state": "error", "reason": "db_unavailable", "retryable": True})
        if prev is not None:
            _log("deliver_duplicate", **ctx, slack_user_id=prev.slack_user_id)
            return _json(200, {"state": "sent", "external_ref": prev.external_ref, "slack_user_id": prev.slack_user_id,
                               "duplicate": True})
        if status.team_id is None:
            await refresh_auth()                      # best effort; resolution falls back to any namespace
        thread_ts: str | None = None
        if req.channel_id:                            # channel post, threaded per job (NOTIFY-D13)
            slack_user = "-"
        else:
            try:
                slack_user = await repo.slack_user_for(req.recipient, status.team_id)
            except Exception as e:  # noqa: BLE001
                _log("deliver_failed", **ctx, reason="db_unavailable", error=type(e).__name__)
                return _json(503, {"state": "error", "reason": "db_unavailable", "retryable": True})
            if not slack_user:
                _log("deliver_suppressed", **ctx, reason="no_slack_link")
                return _json(200, {"state": "suppressed", "reason": "no_slack_link"})
        blocks = build_blocks(req)
        try:
            if req.channel_id:
                channel = req.channel_id
                if req.thread:
                    thread_ts = await repo.get_thread(req.thread.key, channel)
                    if not thread_ts:
                        ptext, pblocks = thread_parent_blocks(req.thread.parent)
                        parent = await slack.post_message(channel=channel, text=ptext, blocks=pblocks,
                                                          metadata={"event_type": "replicant_job_thread",
                                                                    "event_payload": {"job_id": req.thread.key}})
                        thread_ts = await repo.put_thread(req.thread.key, channel, parent)
                        _log("thread_parent_posted", channel_id=channel, job_id=req.thread.key, parent_ts=thread_ts)
            else:
                channel = await slack.open_dm(slack_user)
            ts = await slack.post_message(channel=channel, text=fallback_text(req), blocks=blocks, metadata=metadata(req),
                                          thread_ts=thread_ts)
        except SlackFailure as f:
            _log("deliver_failed", **ctx, slack_user_id=slack_user, reason=f"slack_{f.code}", kind=f.kind)
            body = {"state": "error", "reason": f"slack_{f.code}", "retryable": f.kind != "permanent"}
            if f.kind == "ratelimited":
                ra = str(f.retry_after or 1)
                return _json(429, {**body, "retry_after": int(ra)}, headers={"Retry-After": ra})
            if f.kind == "permanent":
                return _json(422, {**body, "state": "failed"})
            if f.kind == "config":
                return _json(503, body)
            return _json(502, body)
        post = Post(delivery_id=req.delivery_id, principal_id=req.recipient, slack_user_id=slack_user,
                    channel_id=channel, message_ts=ts, event_key=req.event_key, blocks=blocks)
        recorded = True
        try:
            await repo.record_post(post)
            if req.gate and req.actions:
                await repo.add_message_ref(MessageRef(req.gate.gate_id, channel, ts,
                                                      "gate_channel" if req.channel_id else "gate_dm",
                                                      req.gate.revision, req.gate.presented_digest))
        except Exception as e:  # noqa: BLE001 - the DM is out; a retry could duplicate it, but failing now would too
            recorded = False
            log.error(json.dumps({"msg": "record_post_failed", **ctx, "error": type(e).__name__}))
        _log("deliver_sent", **ctx, slack_user_id=slack_user, channel_id=channel, message_ts=ts, blocks=len(blocks),
             recorded=recorded)
        if not req.channel_id:
            spawn(guarded("home_refresh", publish_home(slack_user)))   # App Home follows the person's inbox
        out = {"state": "sent", "external_ref": post.external_ref, "slack_user_id": slack_user}
        if thread_ts:
            out["thread_ts"] = thread_ts
        if not recorded:
            out["recorded"] = False
        return _json(200, out)

    @app.post("/internal/v1/gates/{gate_id}/resolved")
    async def gate_resolved(gate_id: str, request: Request) -> Response:
        """notify.gate_resolved (approvals.md §4.5): re-render every message of the gate without its buttons."""
        if (bad := caller_ok(request)) is not None:
            return bad
        try:
            b = await request.json()
            final, line = str(b.get("final_state") or "closed")[:32], str(b.get("text") or "Decided")[:2000]
        except (ValueError, AttributeError):
            return _json(422, {"state": "rejected", "reason": "invalid_request"})
        try:
            n = await decisions.gate_resolved(gate_id, final, line)
        except Exception as e:  # noqa: BLE001 - retried by the dispatcher
            _log("gate_resolved_failed", gate_id=gate_id, error=type(e).__name__)
            return _json(503, {"state": "error", "reason": "update_failed", "retryable": True})
        _log("gate_resolved", gate_id=gate_id, final_state=final, updated=n)
        return _json(200, {"state": "sent", "external_ref": f"updated:{n}"})

    # --- Slack ingress ------------------------------------------------------------------------------------------
    async def verified_body(request: Request) -> bytes | None:
        body = await request.body()
        if len(body) > MAX_SLACK_BODY:
            return None
        secrets = s.signing_secrets()
        if not secrets:
            log.error(json.dumps({"msg": "slack signing secret not configured"}))
            return None
        if not signing.verify(secrets, request.headers, body):
            _log("slack_signature_rejected", path=request.url.path)
            return None
        return body

    @app.post("/slack/events")
    async def events(request: Request) -> Response:
        body = await verified_body(request)
        if body is None:
            return Response(status_code=401)
        try:
            ev = json.loads(body)
        except ValueError:
            return Response(status_code=400)
        if ev.get("type") == "url_verification":
            return _json(200, {"challenge": ev.get("challenge", "")})
        inner = ev.get("event") or {}
        _log("slack_event", type=ev.get("type"), event_type=inner.get("type"),
             retry_num=request.headers.get("x-slack-retry-num"))
        if inner.get("type") == "app_home_opened" and inner.get("tab", "home") == "home" and inner.get("user"):
            spawn(guarded("home_publish", publish_home(str(inner["user"]))))
        return Response(status_code=200)

    @app.post("/slack/interactions")
    async def interactions(request: Request) -> Response:
        body = await verified_body(request)
        if body is None:
            return Response(status_code=401)
        try:
            payload = json.loads(urllib.parse.parse_qs(body.decode("utf-8", "replace")).get("payload", ["{}"])[0])
        except ValueError:
            payload = {}
        _log("slack_interaction", type=payload.get("type"), user_id=(payload.get("user") or {}).get("id"),
             action_ids=[a.get("action_id") for a in payload.get("actions") or []][:5])
        gate_action = any(a.get("action_id") in ("gate_approve", "gate_reject") for a in payload.get("actions") or [])
        if gate_action or payload.get("type") == "view_submission":
            spawn(guarded("interaction", decisions.handle(payload)))
            return Response(status_code=200)      # ack first; outcome follows by chat.update / ephemeral
        if payload.get("type") == "block_actions":
            return Response(status_code=200)      # URL buttons need no answer
        return _json(200, {"response_type": "ephemeral", "replace_original": False, "text": NOT_LOCAL_TEXT})

    @app.post("/slack/commands")
    async def commands(request: Request) -> Response:
        body = await verified_body(request)
        if body is None:
            return Response(status_code=401)
        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        _log("slack_command", command=(form.get("command") or [""])[0], user_id=(form.get("user_id") or [""])[0])
        return _json(200, {"response_type": "ephemeral", "text": NOT_LOCAL_TEXT})

    return app
