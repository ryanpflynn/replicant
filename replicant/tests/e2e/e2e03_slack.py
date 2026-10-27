"""E2E-03-v0 Slack phase 2 (approvals.md §4.3-§4.6, notifications.md §4; NOTIFY-D12..D15), on real jobs.

  A. gate DM to bob has Approve/Reject buttons; the job gets a thread in #payments-approvals (parent + gate reply with
     buttons). bob clicks Approve in slack-mock -> signed block_actions -> slack-integration -> control-plane-api
     gate.decide as bob (channel slack) -> the DM and the channel gate message are re-rendered in place ("Approved by
     Bob in Slack"), the PR opens and its notice is threaded under the job's parent message.
  B. grace approves in the UI/API; bob's DM loses its buttons ("Approved by Grace in the UI"); bob then clicks the
     original button anyway -> "Not recorded: Already approved in the UI" (ephemeral), no error.
  C. bob clicks Reject -> modal (views.open) -> submits a comment -> the job ends rejected, the DM says "Rejected by
     Bob in Slack", the thread gets the terminal notice.
  D. digest: alice (2 open PRs, threshold 1) gets a "Replicant digest" DM at her digest time and no per-item reminder
     DM; bob (1 item) still gets the per-item reminder DM.
  E. App Home: app_home_opened for alice -> views.publish with the same list as /waiting.
Both PRs are closed at the end (the sample repo keeps its bug). Thresholds are test-only in-memory overrides of the
running control-plane-api (`PUT /v1/admin/dev/notify-overrides`), restored at the end.

Run: `uv run --package replicant-e2e python tests/e2e/e2e03_slack.py` after `replicant dev up` + `seed` (seed step
08 sets the payments approval channel C0PAYAPPR1). Never prints a secret.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e01_v0 as e1  # noqa: E402
import e2e02_notify as e2  # noqa: E402

CH = os.environ.get("E2E_APPROVAL_CHANNEL", "C0PAYAPPR1")
SLACK = e2.SLACK
EV: dict = {}


def log(msg: str) -> None:
    print(f"[e2e03-slack {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def msgs(**params: str) -> list[dict]:
    return e1.ok(httpx.get(f"{SLACK}/_mock/messages", params=params, timeout=10), "slack-mock").json()["messages"]


def meta(m: dict) -> dict:
    return ((m.get("payload") or {}).get("metadata") or {})


def wait_for(pred, what: str, timeout: float = 90):  # noqa: ANN001, ANN201
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = pred()
        if r:
            return r
        time.sleep(2)
    raise AssertionError(f"timeout: {what}")


def gate_dm(user: str, job_id: str) -> dict:
    def f():  # noqa: ANN202
        for m in msgs(user=e2.SLACK_IDS[user], method="chat.postMessage"):
            ep = meta(m).get("event_payload") or {}
            if ep.get("event_key") == "gate.requested" and ep.get("job_id") == job_id:
                return m
        return None
    return wait_for(f, f"gate DM to {user} for {job_id}", 60)


def buttons(m: dict) -> list[str]:
    return [e.get("action_id") for b in m.get("blocks") or [] if b.get("type") == "actions" for e in b.get("elements") or []]


def update_of(channel: str, ts: str, needle: str, timeout: float = 60) -> dict:
    def f():  # noqa: ANN202
        for m in msgs(channel=channel, method="chat.update"):
            if m["ts"] == ts and needle in (m.get("text") or "") + json.dumps(m.get("blocks") or []):
                return m
        return None
    return wait_for(f, f"chat.update of {channel}/{ts} containing {needle!r}", timeout)


def ephemeral(user: str, needle: str, timeout: float = 30) -> dict:
    return wait_for(lambda: next((m for m in msgs(user=e2.SLACK_IDS[user], method="chat.postEphemeral")
                                  if needle in (m.get("text") or "")), None), f"ephemeral to {user}: {needle!r}", timeout)


def thread(job_id: str) -> tuple[dict, list[dict]]:
    cm = msgs(channel=CH, method="chat.postMessage")
    parent = next((m for m in cm if (meta(m).get("event_payload") or {}).get("job_id") == job_id
                   and meta(m).get("event_type") == "replicant_job_thread"), None)
    replies = [m for m in cm if parent and m.get("thread_ts") == parent["ts"]]
    return parent, replies


def wait_thread_reply(job_id: str, event_key: str, timeout: float = 60) -> tuple[dict, dict]:
    def f():  # noqa: ANN202
        p, rs = thread(job_id)
        r = next((x for x in rs if (meta(x).get("event_payload") or {}).get("event_key") == event_key), None)
        return (p, r) if r else None
    return wait_for(f, f"thread reply {event_key} for {job_id} in {CH}", timeout)


def click(record_id: int, action_id: str, user: str) -> dict:
    r = httpx.post(f"{SLACK}/_mock/click", json={"record_id": record_id, "action_id": action_id,
                                                  "user": e2.SLACK_IDS[user]}, timeout=20)
    out = e1.ok(r, "mock click").json()
    assert out.get("status") == 200, out
    return out


def start_job(tag: str) -> tuple[int, str, dict]:
    number, t0 = e1.open_issue(tag)
    job_id = e1.wait_ack(number, t0)
    job = e1.wait_until(e1.api("grace"), job_id, lambda s: e1.open_publish_gate(s) is not None, f"gate {tag}",
                        time.monotonic() + 900)
    gate = e1.open_publish_gate(e1.snapshot(job))
    assert "slack" in (gate.get("channels") or []), f"gate does not admit slack: {gate.get('channels')}"
    return number, job_id, gate


def close_pr(n: int) -> None:
    with e1.gitea() as g:
        e1.ok(g.patch(f"/repos/{e1.ORG}/{e1.REPO}/pulls/{n}", json={"state": "closed"}), "close PR")


def run() -> dict:
    e2.overrides({"digest_threshold": 1})
    try:
        return _run()
    finally:
        e2.overrides({})


def _run() -> dict:
    for u in ("alice", "bob"):
        e2.no_quiet_hours(u)
    deadline = time.monotonic() + 1500

    # A. approve from a Slack DM button
    nA, jA, gA = start_job("e2e03-A")
    dm = gate_dm("bob", jA)
    assert {"gate_approve", "gate_reject"} <= set(buttons(dm)), buttons(dm)
    parent, reply = wait_thread_reply(jA, "gate.requested")
    assert {"gate_approve", "gate_reject"} <= set(buttons(reply)), "channel gate message has no buttons"
    EV["A"] = {"job_id": jA, "gate_id": gA["gate_id"], "dm_channel": dm["channel"], "dm_ts": dm["ts"],
               "thread_parent": {"channel": CH, "ts": parent["ts"], "text": parent.get("text")},
               "gate_reply_ts": reply["ts"], "dm_buttons": buttons(dm)}
    log(f"A {jA}: gate DM {dm['channel']}/{dm['ts']} buttons={buttons(dm)}; thread parent {CH}/{parent['ts']} "
        f"{parent.get('text', '')[:80]!r}")
    res = click(dm["id"], "gate_approve", "bob")
    log(f"A: bob clicked Approve -> slack-integration HTTP {res.get('status')}")
    up = update_of(dm["channel"], dm["ts"], "Approved by Bob in Slack")
    assert "gate_approve" not in buttons(up), "buttons still on the decided DM"
    cup = update_of(CH, reply["ts"], "Approved by Bob in Slack")
    eph = ephemeral("bob", "Approved.")
    EV["A"].update(dm_update=up.get("text"), channel_update=cup.get("text"), ephemeral=eph.get("text"))
    log(f"A: DM updated {up.get('text')!r}; channel gate message updated {cup.get('text')!r}")
    with e1.api("grace") as c:
        gd = e1.ok(c.get(f"/v1/gates/{gA['gate_id']}"), "gate").json()["gate"]
    assert (gd.get("decision") or {}).get("channel") == "slack", gd.get("decision")
    EV["A"]["gate_decision"] = gd.get("decision")
    prA = e1.wait_pr(jA, deadline)
    _, pr_reply = wait_thread_reply(jA, "job.pr_opened")
    EV["A"].update(pr=prA["number"], thread_pr_reply=pr_reply.get("text", "")[:200])
    log(f"A: PR #{prA['number']} opened; thread reply {pr_reply.get('text', '')[:90]!r}")

    # B. decided in the UI first -> the Slack button reports it
    nB, jB, gB = start_job("e2e03-B")
    dmB = gate_dm("bob", jB)
    with e1.api("grace") as c:
        e1.ok(c.post(f"/v1/gates/{gB['gate_id']}/decisions", json={
            "decision": "approve", "presented_digest": gB["presented_digest"], "comment": "e2e03: UI approval"},
            headers={"Idempotency-Key": f'"e2e03-{jB}"'}), "UI approve")
    upB = update_of(dmB["channel"], dmB["ts"], "Approved by Grace in the UI")
    click(dmB["id"], "gate_approve", "bob")             # the original message still had the button
    ephB = ephemeral("bob", "Not recorded: Already approved in the UI")
    EV["B"] = {"job_id": jB, "gate_id": gB["gate_id"], "dm_update": upB.get("text"), "stale_click": ephB.get("text")}
    log(f"B {jB}: DM -> {upB.get('text')!r}; late Slack click -> {ephB.get('text')!r}")
    prB = e1.wait_pr(jB, deadline)
    EV["B"]["pr"] = prB["number"]

    # C. reject through the modal
    nC, jC, gC = start_job("e2e03-C")
    dmC = gate_dm("bob", jC)
    click(dmC["id"], "gate_reject", "bob")
    wait_for(lambda: msgs(method="views.open"), "reject modal opened", 30)
    r = e1.ok(httpx.post(f"{SLACK}/_mock/submit", json={"user": e2.SLACK_IDS["bob"],
                                                       "values": {"comment": {"text": "Wrong approach (e2e03)"}}},
                         timeout=20), "modal submit").json()
    assert r.get("status") == 200, r
    upC = update_of(dmC["channel"], dmC["ts"], "Rejected by Bob in Slack")
    wait_for(lambda: e1.snapshot(e1.get_job(e1.api("grace"), jC)).get("status") == "rejected", "job C rejected", 120)
    _, termC = wait_thread_reply(jC, "job.rejected")
    EV["C"] = {"job_id": jC, "dm_update": upC.get("text"), "thread_terminal": termC.get("text", "")[:160]}
    log(f"C {jC}: DM -> {upC.get('text')!r}; thread -> {termC.get('text', '')[:80]!r}")

    # D. digest for alice (2 PRs > threshold 1) instead of per-item reminders; bob (1 item) gets the reminder
    t_d = time.time()
    now = dt.datetime.now(dt.UTC)
    with e1.api("alice") as c:
        e1.ok(c.put("/v1/me/notification-preferences", json={
            "quiet_hours": {"start": "00:00", "end": "00:00", "timezone": "UTC"},
            "digest": {"enabled": True, "times": [now.strftime("%H:%M")], "weekdays_only": False}}), "alice prefs")
    e2.overrides({"digest_threshold": 1, "pr_idle_seconds": 60})
    dg = e2.wait_dm("alice", "digest.daily", t_d, 120)
    txt = e2.dm_text(dg)
    assert f"PR #{prA['number']}" in txt and f"PR #{prB['number']}" in txt, txt
    EV["D"] = {"digest_alice": txt[:600]}
    log(f"D: digest DM to alice: {txt[:240]!r}")
    rb = e2.wait_dm("bob", "job.pr_reminder", t_d, 150)
    EV["D"]["reminder_bob"] = e2.dm_text(rb)[:300]
    time.sleep(35)                                       # one more sweep: alice must not get the per-item DM
    assert not [m for m in e2.dms("alice", t_d) if e2.event_key(m) == "job.pr_reminder"], "alice got a per-item reminder"
    log(f"D: bob reminder DM {e2.dm_text(rb)[:120]!r}; alice: digest only")

    # E. App Home
    e1.ok(httpx.post(f"{SLACK}/_mock/send/events", json={"type": "event_callback", "team_id": "T0LOCAL01",
          "event": {"type": "app_home_opened", "user": e2.SLACK_IDS["alice"], "tab": "home"}}, timeout=20), "event")
    home = wait_for(lambda: (lambda v: v if v and f"PR #{prB['number']}" in json.dumps(v) else None)(
        httpx.get(f"{SLACK}/_mock/home/{e2.SLACK_IDS['alice']}", timeout=10).json().get("view")), "App Home", 30)
    hj = json.dumps(home)
    assert f"PR #{prA['number']}" in hj
    with e1.api("alice") as c:
        w = c.get("/v1/me/waiting").json()
    EV["E"] = {"home_blocks": len(home.get("blocks") or []), "waiting_total": w["counts"]["total"]}
    log(f"E: App Home for alice: {len(home.get('blocks') or [])} blocks, /waiting total {w['counts']['total']}")

    # cleanup: close both PRs (jobs end pr_closed_unmerged); the closed notice threads under A
    close_pr(prA["number"])
    close_pr(prB["number"])
    _, endA = wait_thread_reply(jA, "job.rejected", 120)
    EV["A"]["thread_terminal"] = endA.get("text", "")[:160]
    log(f"PASS: A {jA}, B {jB}, C {jC}")
    out = Path(os.environ.get("E2E_EVIDENCE_FILE", "/tmp/e2e03-evidence.json"))
    try:
        out.write_text(json.dumps(EV, indent=2, default=str))
        log(f"evidence written to {out}")
    except OSError:
        pass
    return EV


def test_e2e03_slack() -> None:
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        log(f"FAIL: {e}")
        sys.exit(1)
