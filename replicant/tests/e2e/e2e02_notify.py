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


import replicant_e2e as e1
from replicant_e2e import log
from replicant_e2e.notify import (SLACK, SLACK_IDS, comments, dm_text, dms, event_key, no_quiet_hours,  # noqa: F401
                                 overrides, reassert_overrides, wait_comment, wait_dm, waiting)

REMINDER_S = float(os.environ.get("E2E_REMINDER_S", "120"))
EVIDENCE: dict = {}
e1.TAG = "e2e02-notify"


def run() -> str:
    eff = overrides({"pr_idle_seconds": int(REMINDER_S)})
    log(f"notify overrides (test only): pr_idle_seconds={eff['pr_idle_seconds']}")
    try:
        return _run()
    finally:
        overrides({})


def _run() -> str:
    t_start = time.time() - 1
    log(f"main reset to the initial commit {e1.reset_repo()[:12]} (the planted bug)")
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
