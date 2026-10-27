"""Notification helpers shared by e2e02 (notify) and e2e03 (Slack): slack-mock DMs, the in-memory notify overrides
of the running control-plane-api (LOCAL ONLY test hook), preferences, bot comments and /v1/me/waiting."""
from __future__ import annotations

import os
import re
import time

import httpx

import replicant_e2e as e1
from replicant_e2e import log

SLACK = os.environ.get("REPLICANT_SLACK_MOCK_URL", "http://127.0.0.1:8410")
SLACK_IDS = {"alice": "U0ALICE01", "bob": "U0BOB0001", "grace": "U0GRACE01"}


ACTIVE_OVERRIDES: dict = {}


def overrides(values: dict) -> dict:
    """LOCAL ONLY test hook (replicant_dev_auth): notification thresholds of the running control-plane-api. In memory,
    so they are re-applied while waiting (a recreated control-plane-api would otherwise drop them mid-test)."""
    ACTIVE_OVERRIDES.clear()
    ACTIVE_OVERRIDES.update(values)
    r = httpx.put(f"{e1.API}/v1/admin/dev/notify-overrides", json=values, timeout=10,
                  headers={"Authorization": f"Bearer {e1.secret('dev_seed_token')}"})
    return e1.ok(r, "PUT notify-overrides").json()["effective"]


def no_quiet_hours(user: str) -> None:
    with e1.api(user) as c:
        cur = e1.ok(c.get("/v1/me/notification-preferences"), "get prefs").json()
        cur.pop("version", None)
        cur["quiet_hours"] = {"start": "00:00", "end": "00:00", "timezone": "UTC"}
        v = e1.ok(c.put("/v1/me/notification-preferences", json=cur), "put prefs").json()["version"]
    log(f"{user}: notification preferences v{v} (quiet_hours none)")


def dms(user: str, since: float) -> list[dict]:
    r = e1.ok(httpx.get(f"{SLACK}/_mock/messages", params={"user": SLACK_IDS[user]}, timeout=10), "slack-mock messages")
    out = []
    for m in r.json().get("messages", []):
        if float(m.get("ts") or 0) >= since:
            out.append(m)
    return out


def event_key(m: dict) -> str | None:
    md = (m.get("payload") or {}).get("metadata") or {}
    return (md.get("event_payload") or {}).get("event_key")


def wait_dm(user: str, key: str, since: float, timeout: float, job_id: str | None = None) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reassert_overrides()
        for m in dms(user, since):
            ep = ((m.get("payload") or {}).get("metadata") or {}).get("event_payload") or {}
            if event_key(m) == key and (job_id is None or ep.get("job_id") in (None, job_id)):
                return m
        time.sleep(2)
    raise AssertionError(f"no Slack DM {key} to {user} within {timeout:.0f}s; have "
                         f"{[event_key(m) for m in dms(user, since)]}")


def comments(number: int) -> list[dict]:
    with e1.gitea() as g:
        return e1.ok(g.get(f"/repos/{e1.ORG}/{e1.REPO}/issues/{number}/comments"), "comments").json()


def reassert_overrides() -> None:
    if ACTIVE_OVERRIDES:
        try:
            httpx.put(f"{e1.API}/v1/admin/dev/notify-overrides", json=ACTIVE_OVERRIDES, timeout=10,
                      headers={"Authorization": f"Bearer {e1.secret('dev_seed_token')}"})
        except httpx.HTTPError:
            pass


def wait_comment(number: int, pattern: str, timeout: float) -> dict:
    rx = re.compile(pattern)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reassert_overrides()
        for c in comments(number):
            if c["user"]["login"] == "replicant-bot" and rx.search(c["body"]):
                return c
        time.sleep(2)
    raise AssertionError(f"no replicant-bot comment matching {pattern!r} on #{number} within {timeout:.0f}s")


def waiting(user: str) -> dict:
    with e1.api(user) as c:
        return e1.ok(c.get("/v1/me/waiting"), "GET /v1/me/waiting").json()


def dm_text(m: dict) -> str:
    p = m.get("payload") or {}
    parts = [p.get("text") or ""]
    for b in p.get("blocks") or []:
        t = b.get("text")
        if isinstance(t, dict):
            parts.append(t.get("text", ""))
        for f in b.get("fields") or []:
            parts.append(f.get("text", ""))
        for e in b.get("elements") or []:
            if e.get("url"):
                parts.append(f"[{(e.get('text') or {}).get('text', '')}]({e['url']})")
    return "\n".join(x for x in parts if x)
