"""E2E-02-v0 notifications (spec/07-control-plane-and-ux/notifications.md, approvals.md, identity.md), on a real job.

Same bugfix flow as e2e01 (Gitea issue labelled `replicant` -> publish gate -> PR -> merge), asserting every channel:
  1. gate opened  -> Slack DM (slack-mock) to the approver (bob)                                      gate.requested
  2. PR opened    -> replicant-bot issue comment "@alice PR #N is ready for review" with the diff summary, the PR
                     link and the job link; Slack DM to the requester (alice)                          job.pr_opened
  3. inbox        -> GET /v1/me/waiting lists the PR for alice (requester) and bob (reviewer); the count matches
  4. reminder     -> with REPLICANT_NOTIFY_PR_IDLE_SECONDS (e.g. 120) and the PR left open: a reminder issue
                     comment and a consolidated "Waiting on you" DM to alice and bob                  job.pr_reminder
  5. merge        -> "PR #N merged · job done" issue comment and DM to alice; the PR leaves the inbox job.succeeded

Quiet hours: the platform default (20:00-08:00 and weekends) would hold the reminder, so the run first sets alice's
and bob's notification preferences to "no quiet hours" through PUT /v1/me/notification-preferences (the preference
path the spec gives users). The short PR idle threshold is a test-only, in-memory override of the running
control-plane-api (dev seed token, `PUT /v1/admin/dev/notify-overrides {"pr_idle_seconds": 120}`), restored at the
end; nothing is written to dev/compose/.env.

Run: `uv run --package replicant-e2e python tests/e2e/e2e02_notify.py`. Env as e2e01 plus REPLICANT_SLACK_MOCK_URL
(http://127.0.0.1:8410), E2E_REMINDER_S (120). Never prints a secret.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e01_v0 as e1  # noqa: E402

SLACK = os.environ.get("REPLICANT_SLACK_MOCK_URL", "http://127.0.0.1:8410")
REMINDER_S = float(os.environ.get("E2E_REMINDER_S", "120"))
SLACK_IDS = {"alice": "U0ALICE01", "bob": "U0BOB0001", "grace": "U0GRACE01"}
EVIDENCE: dict = {}


def log(msg: str) -> None:
    print(f"[e2e02-notify {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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


def run() -> str:
    eff = overrides({"pr_idle_seconds": int(REMINDER_S)})
    log(f"notify overrides (test only): pr_idle_seconds={eff['pr_idle_seconds']}")
    try:
        return _run()
    finally:
        overrides({})


def _run() -> str:
    t_start = time.time() - 1
    for u in ("alice", "bob"):
        no_quiet_hours(u)
    deadline = time.monotonic() + e1.JOB_TIMEOUT_S
    number, t0 = e1.open_issue(f"e2e02-{int(t_start)}")
    job_id = e1.wait_ack(number, t0)
    reader = e1.api(e1.READER)
    job = e1.wait_until(reader, job_id, lambda s: e1.open_publish_gate(s) is not None, "publish gate", deadline)
    gate = e1.open_publish_gate(e1.snapshot(job))

    # 1. gate opened -> approver DM
    m = wait_dm("bob", "gate.requested", t_start, 60, job_id)
    EVIDENCE["dm_bob_gate_requested"] = dm_text(m)
    log(f"DM to bob (gate.requested): {dm_text(m)[:160]!r}")
    assert gate["gate_id"] in json.dumps(m["payload"]), "gate DM does not link the gate"
    w = waiting("bob")
    assert any(i["kind"] == "gate" and i["gate_id"] == gate["gate_id"] for i in w["items"]), w
    log(f"inbox(bob) while the gate is open: counts={w['counts']}")

    # 2. approve -> PR -> issue comment + requester DM
    e1.approve(gate)
    pr = e1.wait_pr(job_id, deadline)
    n = pr["number"]
    c = wait_comment(number, rf"PR #{n} is ready for review", 90)
    body = c["body"]
    EVIDENCE["comment_pr_opened"] = body
    log(f"issue comment (job.pr_opened):\n{body}")
    assert body.startswith(f"@{e1.REQUESTER} "), "comment does not mention the requester"
    assert re.search(r"\d+ files? \+\d+ \u2212\d+", body), "no diff summary"
    assert f"/acme-payments/ledger-api/pulls/{n})" in body, "no PR link"
    assert f"http://127.0.0.1:8400/jobs/{job_id})" in body, "no job link to the web UI"
    m = wait_dm("alice", "job.pr_opened", t_start, 60, job_id)
    EVIDENCE["dm_alice_pr_opened"] = dm_text(m)
    log(f"DM to alice (job.pr_opened): {dm_text(m)[:200]!r}")
    t_pr = time.monotonic()

    # 3. inbox lists the PR for the requester and the reviewer
    for user, role in (("alice", "requester"), ("bob", "reviewer")):
        dl = time.monotonic() + 30
        while True:
            w = waiting(user)
            hit = [i for i in w["items"] if i["kind"] == "pr" and i["job_id"] == job_id]
            if hit or time.monotonic() > dl:
                break
            time.sleep(2)
        assert hit, f"/v1/me/waiting for {user} does not list the PR: {w}"
        assert hit[0]["role"] == role and hit[0]["pr"]["number"] == n, hit[0]
        assert w["counts"]["total"] == len(w["items"]), w["counts"]
        EVIDENCE[f"inbox_{user}"] = {"counts": w["counts"], "item": hit[0]}
        log(f"inbox({user}): counts={w['counts']} pr=#{hit[0]['pr']['number']} role={hit[0]['role']}")

    # 4. leave the PR open past the idle threshold -> reminder comment + consolidated DMs
    timeout = REMINDER_S + 150 - (time.monotonic() - t_pr)
    c = wait_comment(number, rf"PR #{n} is still waiting for review", max(timeout, 60))
    EVIDENCE["comment_pr_reminder"] = c["body"]
    log(f"reminder comment after {time.monotonic() - t_pr:.0f}s:\n{c['body']}")
    for user in ("alice", "bob"):
        m = wait_dm(user, "job.pr_reminder", t_start, 60)
        EVIDENCE[f"dm_{user}_pr_reminder"] = dm_text(m)
        log(f"reminder DM to {user}: {dm_text(m)[:220]!r}")
        assert "waiting for you" in dm_text(m) and f"PR #{n}" in dm_text(m)
    w = waiting("alice")
    hit = [i for i in w["items"] if i["kind"] == "pr" and i["job_id"] == job_id]
    assert hit and hit[0]["last_reminded_at"], hit

    # 5. merge -> "merged" notification
    e1.merge(pr)
    e1.wait_until(reader, job_id, lambda s: s.get("status") == "succeeded", "job succeeded", deadline)
    c = wait_comment(number, rf"PR #{n} merged · job done", 90)
    EVIDENCE["comment_succeeded"] = c["body"]
    log(f"merged comment:\n{c['body']}")
    m = wait_dm("alice", "job.succeeded", t_start, 60, job_id)
    EVIDENCE["dm_alice_succeeded"] = dm_text(m)
    log(f"DM to alice (job.succeeded): {dm_text(m)[:200]!r}")
    w = waiting("alice")
    assert not any(i["kind"] == "pr" and i["job_id"] == job_id for i in w["items"]), "merged PR still in the inbox"
    log(f"PASS: {job_id} (issue #{number}, PR #{n})")
    out = Path(os.environ.get("E2E_EVIDENCE_FILE", "/tmp/e2e02-evidence.json"))
    try:
        out.write_text(json.dumps({"job_id": job_id, "issue": number, "pr": n, **EVIDENCE}, indent=2))
        log(f"evidence written to {out}")
    except OSError:
        pass
    return job_id


def test_e2e02_notify() -> None:
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        log(f"FAIL: {e}")
        sys.exit(1)
