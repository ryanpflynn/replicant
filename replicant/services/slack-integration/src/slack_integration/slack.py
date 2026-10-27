"""Slack Web API access (slack_sdk AsyncWebClient, base_url = SLACK_API_URL) and error classification."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import aiohttp
from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.web.async_client import AsyncWebClient

from slack_integration.config import Settings, read_secret

# Slack errors that will not succeed on retry: the message or the recipient is wrong.
PERMANENT = frozenset({
    "channel_not_found", "user_not_found", "user_not_visible", "cannot_dm_bot", "user_disabled", "is_archived",
    "not_in_channel", "messages_tab_disabled", "restricted_action", "invalid_blocks", "invalid_blocks_format",
    "msg_too_long", "no_text", "too_many_attachments", "invalid_arguments", "invalid_metadata_format",
    "invalid_metadata_schema", "metadata_too_large", "metadata_must_be_sent_from_app", "users_list_not_supplied",
    "not_supported", "too_many_users", "invalid_attachments", "cannot_find_channel",
})
# Our configuration is wrong (token, scopes): retryable once an operator fixes it.
CONFIG = frozenset({
    "not_authed", "invalid_auth", "account_inactive", "token_revoked", "token_expired", "no_permission",
    "missing_scope", "not_allowed_token_type", "team_access_not_granted", "org_login_required", "ekm_access_denied",
})


@dataclass
class SlackFailure(Exception):
    """kind: ratelimited | retryable | config | permanent."""
    kind: str
    code: str
    retry_after: int | None = None

    def __str__(self) -> str:
        return f"{self.kind}:{self.code}"


def classify(exc: BaseException) -> SlackFailure:
    if isinstance(exc, SlackFailure):
        return exc
    if isinstance(exc, SlackApiError):
        resp = exc.response
        status = getattr(resp, "status_code", 0) or 0
        data = resp.data if isinstance(getattr(resp, "data", None), dict) else {}
        code = str(data.get("error") or (f"http_{status}" if status else "unknown_error"))
        if code == "ratelimited" or status == 429:
            hdrs = getattr(resp, "headers", None) or {}
            ra = hdrs.get("Retry-After") or hdrs.get("retry-after")
            try:
                retry = max(1, int(float(ra))) if ra is not None else 1
            except ValueError:
                retry = 1
            return SlackFailure("ratelimited", "ratelimited", retry)
        if code in CONFIG:
            return SlackFailure("config", code)
        if code in PERMANENT:
            return SlackFailure("permanent", code)
        return SlackFailure("retryable", code)
    if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError)):
        return SlackFailure("retryable", "slack_unreachable")
    if isinstance(exc, SlackClientError):
        return SlackFailure("retryable", "slack_client_error")
    return SlackFailure("retryable", "slack_error")


class SlackGateway:
    """Thin async wrapper; every failure surfaces as SlackFailure. The bot token is re-read from its file and the
    client rebuilt when it changes (rotation without restart)."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._client: AsyncWebClient | None = None
        self._token: str | None = None

    def _get_client(self) -> AsyncWebClient:
        token = read_secret(self._s.slack_bot_token_file)
        if not token:
            raise SlackFailure("config", "bot_token_not_configured")
        if self._client is None or token != self._token:
            self._client = AsyncWebClient(token=token, base_url=self._s.slack_api_url, timeout=int(self._s.slack_timeout_s))
            self._token = token
        return self._client

    async def _call(self, coro_fn: Any) -> dict[str, Any]:
        try:
            resp = await coro_fn(self._get_client())
        except Exception as e:  # noqa: BLE001 - classified below
            raise classify(e) from e
        return resp.data if isinstance(resp.data, dict) else {}

    async def auth_test(self) -> dict[str, Any]:
        return await self._call(lambda c: c.auth_test())

    async def open_dm(self, user_id: str) -> str:
        data = await self._call(lambda c: c.conversations_open(users=[user_id]))
        cid = (data.get("channel") or {}).get("id")
        if not cid:
            raise SlackFailure("retryable", "conversations_open_no_channel")
        return str(cid)

    async def post_message(self, *, channel: str, text: str, blocks: list[dict[str, Any]],
                           metadata: dict[str, Any] | None = None, thread_ts: str | None = None) -> str:
        kw: dict[str, Any] = {"metadata": metadata} if metadata else {}
        if thread_ts:
            kw["thread_ts"] = thread_ts
        data = await self._call(lambda c: c.chat_postMessage(channel=channel, text=text, blocks=blocks,
                                                             unfurl_links=False, unfurl_media=False, **kw))
        ts = data.get("ts")
        if not ts:
            raise SlackFailure("retryable", "post_no_ts")
        return str(ts)

    async def update_message(self, *, channel: str, ts: str, text: str, blocks: list[dict[str, Any]]) -> None:
        await self._call(lambda c: c.chat_update(channel=channel, ts=ts, text=text, blocks=blocks))

    async def post_ephemeral(self, *, channel: str, user: str, text: str) -> None:
        await self._call(lambda c: c.chat_postEphemeral(channel=channel, user=user, text=text))

    async def open_view(self, *, trigger_id: str, view: dict[str, Any]) -> None:
        await self._call(lambda c: c.views_open(trigger_id=trigger_id, view=view))

    async def publish_home(self, *, user_id: str, view: dict[str, Any]) -> None:
        await self._call(lambda c: c.views_publish(user_id=user_id, view=view))
