"""Unit tests for the notification fan-out (no database): templates, channels, quiet hours, reminder merge, the
dispatcher's adapter mapping and the new routes."""
import datetime as dt
from types import SimpleNamespace

import httpx
import pytest

from control_plane_api import notify_render as R
from control_plane_api.app import create_app, iter_routes
from control_plane_api.config import Settings
from control_plane_api.notify_loops import NotifyLoops, backoff_seconds, merge_reminders
from control_plane_api.notify_policy import NotifySettings, QuietHours, channels_for, quiet_hours

UTC = dt.UTC


def facts(**kw):
    d = dict(job_id="job_01m5sxdmbsb9ykgn85d9x3j89t", tenant_id="payments",
             title="Refund ledger entries double-count FX fees", repo_full="acme-payments/ledger-api",
             repo_short="ledger-api", requester_login="alice",
             job_url="http://127.0.0.1:8400/jobs/job_01m5sxdmbsb9ykgn85d9x3j89t", pr_number=8,
             pr_url="http://127.0.0.1:3300/acme-payments/ledger-api/pulls/8",
             diff={"files": 3, "additions": 41, "deletions": 12})
    d.update(kw)
    return R.JobFacts(**d)


def test_pr_opened_comment_has_mention_pr_diff_and_links():
    m = R.pr_opened(facts())
    assert m.markdown.startswith("@alice **PR #8 is ready for review**")
    assert "3 files +41 −12" in m.markdown
    assert "(http://127.0.0.1:3300/acme-payments/ledger-api/pulls/8)" in m.markdown
    assert "(http://127.0.0.1:8400/jobs/job_01m5sxdmbsb9ykgn85d9x3j89t)" in m.markdown
    assert m.summary == "PR #8 opened: ledger-api · 3 files +41 −12"     # notifications.md §2 copy
    sp = m.slack_payload()
    assert sp["links"][0] == {"label": "Review PR #8", "url": m.links[0].url, "primary": True}


def test_untrusted_title_is_quoted_and_escaped():
    m = R.pr_opened(facts(title="@everyone [click](http://evil) <b>"))
    assert "@​everyone" in m.markdown and "\\[click\\]" in m.markdown
    assert "&lt;b&gt;" in m.slack_text and "\n>" in m.slack_text


def test_succeeded_and_failed_copy():
    assert R.succeeded(facts(cost_usd=9.84)).summary == "PR #8 merged · job done · $9.84"
    f = R.failed(facts(phase="qa", outcome="qa_failed", reason="WorkspaceDiskExceeded (40 GiB)"))
    assert f.summary == "Failed in QA: qa_failed"
    assert "WorkspaceDiskExceeded" in f.markdown and "Next step" in f.markdown


def test_channels_defaults_prefs_and_obligations():
    assert channels_for("job.pr_opened", {}, "github_comment") == ["github_comment", "ui", "slack_dm"]
    assert channels_for("job.pr_opened", {}, None) == ["ui", "slack_dm"]
    assert channels_for("job.pr_opened", {"channels": {"job.pr_opened": ["ui"]}}, "github_comment") == ["ui"]
    assert channels_for("job.pr_opened", {"channels": {"job.pr_opened": ["none"]}}, "github_comment") == []
    assert channels_for("gate.escalated", {"channels": {"gate.escalated": ["none"]}}, None) == ["ui"]
    assert channels_for("job.something_new", {}, "github_comment") == ["ui"]


def test_quiet_hours_window_weekend_and_override():
    ns = NotifySettings()
    q = quiet_hours({}, "Europe/Berlin", ns)                     # spec default: 20:00-08:00 + weekends
    mon_noon = dt.datetime(2026, 10, 26, 11, 0, tzinfo=UTC)      # 12:00 Berlin (CET)
    mon_night = dt.datetime(2026, 10, 26, 21, 30, tzinfo=UTC)    # 22:30 Berlin
    sunday = dt.datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    assert not q.active(mon_noon) and q.active(mon_night) and q.active(sunday)
    nxt = q.next_allowed(mon_night)
    assert nxt.astimezone(q.tz).hour == 8 and nxt.astimezone(q.tz).day == 27
    assert q.next_allowed(sunday).astimezone(q.tz).weekday() == 0
    none = quiet_hours({"quiet_hours": {"start": "00:00", "end": "00:00", "timezone": "UTC"}}, None, ns)
    assert not none.active(sunday) and not none.active(mon_night)
    off = quiet_hours({}, None, NotifySettings(quiet_default="off"))
    assert not off.active(sunday)
    assert QuietHours(dt.time(9), dt.time(17), UTC).active(mon_noon)


def test_consolidated_reminder_one_message_for_several_items():
    now = dt.datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    rows = [SimpleNamespace(payload={"consolidate": {"kind": "pr", "title": "A", "repo_short": "ledger-api",
                                                     "url": "http://g/pulls/8", "pr_number": 8,
                                                     "waiting_since": (now - dt.timedelta(days=2)).isoformat()}}),
            SimpleNamespace(payload={"consolidate": {"kind": "gate", "title": "B", "repo_short": "ledger-api",
                                                     "url": "http://w/approvals/g1",
                                                     "waiting_since": (now - dt.timedelta(hours=4)).isoformat(),
                                                     "expires_at": (now + dt.timedelta(hours=3)).isoformat()}}),
            SimpleNamespace(payload={"consolidate": {"kind": "pr", "title": "A", "repo_short": "ledger-api",
                                                     "url": "http://g/pulls/8", "pr_number": 8,
                                                     "waiting_since": (now - dt.timedelta(days=2)).isoformat()}})]
    p = merge_reminders(rows, now, "http://w/waiting")
    assert p["title"] == "Waiting on you"
    assert p["text"].startswith("*2 waiting for you · 1 expire today · oldest 2 d*")
    assert p["text"].index("Approval") < p["text"].index("PR #8")        # expiring gates first
    assert backoff_seconds(1) == 5 and backoff_seconds(20) == 600


@pytest.mark.asyncio
async def test_dispatcher_maps_adapter_responses():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers.get("authorization"), req.read()))
        if "slack" in str(req.url):
            return httpx.Response(200, json={"state": "suppressed", "reason": "no_slack_link"})
        return httpx.Response(503, json={"code": "broker_down"})

    svc = SimpleNamespace(db=None, ns=NotifySettings(slack_integration_url="http://slack", github_integration_url="http://gh"))
    loops = NotifyLoops(svc, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    row = SimpleNamespace(channel="slack_dm", payload={"slack": {"title": "t", "text": "x"}}, delivery_id="ntf_x",
                          recipient="usr_x", tenant_id="payments", event_key="job.pr_opened", job_id="job_x")
    assert await loops.send(row) == ("suppressed", None, "no_slack_link")
    row2 = SimpleNamespace(channel="github_comment", payload={"github": {"repo": {}, "issue_number": 7, "body": "b"}},
                           delivery_id="ntf_y", recipient="usr_x", tenant_id="payments", event_key="job.pr_opened",
                           job_id="job_x")
    out, _, err = await loops.send(row2)
    assert out == "retry" and err == "http_503"
    assert seen[0][0] == "/internal/v1/notifications/deliver"


def test_notify_routes_registered():
    app = create_app(Settings(background_tasks=False))
    paths = {(getattr(r, "path", ""), m) for r in iter_routes(list(app.routes)) for m in getattr(r, "methods", ())}
    for p in [("/v1/me/waiting", "GET"), ("/v1/me/notification-preferences", "GET"),
              ("/v1/me/notification-preferences", "PUT"), ("/internal/v1/notifications", "POST")]:
        assert p in paths, p
