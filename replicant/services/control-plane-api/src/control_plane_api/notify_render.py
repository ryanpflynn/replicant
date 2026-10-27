"""Notification content (notifications.md N-3): fixed templates rendered from structured data, one per event key
and channel family. Untrusted text (issue titles, failure reasons, model output) is quoted on its own line or in
code spans, never interpolated into a sentence, and escaped for the target markup.

Pure functions only (unit-tested in tests/test_notify.py). Three output families:
* `markdown`  GitHub/Gitea issue comments
* `slack`     Slack mrkdwn (slack-integration builds the Block Kit around `title`/`text`/`fields`/`links`)
* `plain`     the UI feed summary (notify.feed.summary, <= 300 chars) and Slack's notification fallback
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

MAX_FEED = 300


# ------------------------------------------------------------------------------------------------ escaping
def esc_md(s: str) -> str:
    """Markdown-inert text: escapes markup and neutralizes @mentions and #refs (no pings from untrusted text)."""
    s = re.sub(r"([\\`*_{}\[\]()<>#+!|~>])", r"\\\1", s)
    return s.replace("@", "@​")


def esc_slack(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def one_line(s: str | None, limit: int = 200) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def human_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return f"{s} s" if s < 60 else "1 min"
    if s < 90 * 60:
        return f"{round(s / 60)} min"
    if s < 36 * 3600:
        h = s / 3600
        return f"{h:.0f} h" if h >= 10 else f"{h:.1f} h".replace(".0 h", " h")
    return f"{round(s / 86400)} d"


def utc_clock(t: dt.datetime | None) -> str:
    return t.astimezone(dt.UTC).strftime("%b %d %H:%M UTC") if t else ""


def diff_line(stat: dict[str, Any] | None) -> str:
    if not stat:
        return ""
    files = int(stat.get("files") or 0)
    return f"{files} file{'s' if files != 1 else ''} +{int(stat.get('additions') or 0)} −{int(stat.get('deletions') or 0)}"


# ------------------------------------------------------------------------------------------------ model
@dataclass
class Link:
    label: str
    url: str
    primary: bool = False


@dataclass
class Message:
    """One rendered notification. `markdown` is for issue comments, `slack_*` for slack-integration, `summary`
    for the UI feed."""
    title: str
    summary: str
    href: str
    markdown: str = ""
    slack_text: str = ""
    fields: list[tuple[str, str]] = field(default_factory=list)
    context: str = ""
    links: list[Link] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)    # interactive buttons (gate decisions)
    gate_ref: dict[str, Any] | None = None                          # {gate_id, revision, presented_digest}

    def slack_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"title": self.title[:150], "text": self.slack_text[:2900]}
        if self.fields:
            out["fields"] = [{"label": a[:100], "value": b[:1900]} for a, b in self.fields[:10]]
        if self.context:
            out["context"] = self.context[:1900]
        out["links"] = [{"label": lk.label[:75], "url": lk.url, **({"primary": True} if lk.primary else {})}
                        for lk in self.links[:5]]
        if self.actions:
            out["actions"] = self.actions[:4]
        if self.gate_ref:
            out["gate"] = self.gate_ref
        return out


@dataclass
class JobFacts:
    """What templates may use about a job (all from the projection, intake or the intent)."""
    job_id: str
    tenant_id: str
    title: str = ""
    repo_full: str = ""               # owner/name
    repo_short: str = ""              # name
    requester_login: str | None = None
    job_url: str = ""
    pr_number: int | None = None
    pr_url: str | None = None
    diff: dict[str, Any] | None = None
    phase: str | None = None
    outcome: str | None = None
    reason: str | None = None
    cost_usd: float | None = None


def _mention_md(login: str | None) -> str:
    return f"@{login} " if login and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,38}", login) else ""


def _quote_md(s: str) -> str:
    return "> " + esc_md(one_line(s, 300))


def _job_ref_md(j: JobFacts) -> str:
    return f"[`{j.job_id}`]({j.job_url})"


# ------------------------------------------------------------------------------------------------ job events
def pr_opened(j: JobFacts) -> Message:
    n, dl = j.pr_number, diff_line(j.diff)
    head = f"PR #{n} is ready for review"
    facts = " · ".join(x for x in (f"`{j.repo_short}`" if j.repo_short else "", dl) if x)
    md = (f"{_mention_md(j.requester_login)}**{head}**\n\n{_quote_md(j.title)}\n\n"
          + (f"{facts}\n\n" if facts else "")
          + f"[Review PR #{n}]({j.pr_url}) · job {_job_ref_md(j)} in Replicant\n\n"
          "<sub>Replicant keeps this job in PR review until the PR is merged or closed. "
          "Review comments on the PR are picked up as feedback.</sub>")
    st = (f"*{head}*\n>{esc_slack(one_line(j.title, 300))}\n`{esc_slack(j.repo_full)}`"
          + (f" · {dl}" if dl else ""))
    return Message(title=head, href=f"/jobs/{j.job_id}",
                   summary=one_line(f"PR #{n} opened: {j.repo_short} · {dl}".rstrip(" ·"), MAX_FEED),
                   markdown=md, slack_text=st, fields=[("Diff", dl)] if dl else [],
                   links=[Link(f"Review PR #{n}", j.pr_url or j.job_url, True), Link("Open in Replicant", j.job_url)])


def succeeded(j: JobFacts) -> Message:
    if j.pr_number:
        head = f"PR #{j.pr_number} merged · job done"
    else:
        head = "Job done"
    cost = f" · ${j.cost_usd:,.2f}" if j.cost_usd else ""
    md = (f"{_mention_md(j.requester_login)}**{head}**{cost}\n\n{_quote_md(j.title)}\n\n"
          f"Job {_job_ref_md(j)}" + (f" · [PR #{j.pr_number}]({j.pr_url})" if j.pr_url else ""))
    st = f"*{head}*{cost}\n>{esc_slack(one_line(j.title, 300))}"
    links = ([Link(f"PR #{j.pr_number}", j.pr_url)] if j.pr_url else []) + [Link("Open in Replicant", j.job_url)]
    return Message(title=head, summary=one_line(head + cost, MAX_FEED), href=f"/jobs/{j.job_id}", markdown=md,
                   slack_text=st, links=links)


PHASE_LABEL = {"intake": "intake", "plan": "planning", "implement": "implementation", "qa": "QA",
               "review": "review", "publish": "publishing", "pr_review": "PR review", "repair": "repair",
               "approval": "approval", "done": "the last step"}


def failed(j: JobFacts) -> Message:
    where = PHASE_LABEL.get(j.phase or "", j.phase or "the job")
    outcome = j.outcome or "failed"
    head = f"Failed in {where}: {outcome}"
    md = (f"{_mention_md(j.requester_login)}**{esc_md(head)}**\n\n{_quote_md(j.title)}\n\n"
          + (f"Reason:\n\n```text\n{(j.reason or '')[:500].replace('```', '` ` `')}\n```\n\n" if j.reason else "")
          + f"Next step: open the job {_job_ref_md(j)} for the timeline and logs, then comment `/replicant retry` "
            "here to start a new job, or ask #replicant-platform if it looks like a platform problem.")
    st = (f"*{esc_slack(head)}*\n>{esc_slack(one_line(j.title, 300))}"
          + (f"\n`{esc_slack(one_line(j.reason, 400))}`" if j.reason else ""))
    return Message(title=head[:150], summary=one_line(head, MAX_FEED), href=f"/jobs/{j.job_id}", markdown=md,
                   slack_text=st, links=[Link("Open in Replicant", j.job_url, True)])


def ended(j: JobFacts, status: str) -> Message:
    label = {"cancelled": "Job cancelled", "rejected": "Job rejected", "expired": "Job expired",
             "taken_over": "Job taken over"}.get(status, f"Job {status}")
    head = f"{label}: {j.outcome}" if j.outcome else label
    if j.outcome == "pr_closed_unmerged" and j.pr_number:
        head = f"PR #{j.pr_number} closed without merging · job ended"
    md = f"{_mention_md(j.requester_login)}**{esc_md(head)}**\n\n{_quote_md(j.title)}\n\nJob {_job_ref_md(j)}"
    st = f"*{esc_slack(head)}*\n>{esc_slack(one_line(j.title, 300))}"
    return Message(title=head[:150], summary=one_line(head, MAX_FEED), href=f"/jobs/{j.job_id}", markdown=md,
                   slack_text=st, links=[Link("Open in Replicant", j.job_url, True)])


def pr_reminder_comment(j: JobFacts, waited_s: float, since: dt.datetime | None, logins: list[str]) -> Message:
    who = " ".join(_mention_md(x).strip() for x in logins if _mention_md(x)) or ""
    head = f"PR #{j.pr_number} is still waiting for review"
    md = (f"{who + ' ' if who else ''}**{head}** ({human_duration(waited_s)}"
          + (f", since {utc_clock(since)}" if since else "") + ").\n\n"
          f"[Review PR #{j.pr_number}]({j.pr_url}) · job {_job_ref_md(j)}\n\n"
          "<sub>Merge or close the PR in the Git host; Replicant keeps the job open until then and reminds "
          "once a day.</sub>")
    return Message(title=head, summary=one_line(f"{head} ({human_duration(waited_s)})", MAX_FEED),
                   href=f"/jobs/{j.job_id}", markdown=md, slack_text=f"*{head}*",
                   links=[Link(f"Review PR #{j.pr_number}", j.pr_url or j.job_url, True), Link("Open in Replicant", j.job_url)])


def lifetime_escalation(j: JobFacts, age_s: float, max_age_days: int) -> Message:
    left = max_age_days * 86400 - age_s
    head = f"Job open {human_duration(age_s)}: it expires in {human_duration(left)}"
    st = (f"*{head}*\n>{esc_slack(one_line(j.title, 300))}\n`{esc_slack(j.repo_full)}`"
          + (f" · PR #{j.pr_number} still open" if j.pr_number else "")
          + f"\nThe hard job lifetime is {max_age_days} days. Ask the requester to merge, close or cancel it.")
    md = (f"**{head}**\n\n{_quote_md(j.title)}\n\nThe hard job lifetime is {max_age_days} days. "
          f"Tenant admins were notified. Job {_job_ref_md(j)}")
    links = [Link("Open in Replicant", j.job_url, True)] + ([Link(f"PR #{j.pr_number}", j.pr_url)] if j.pr_url else [])
    return Message(title=head, summary=one_line(head, MAX_FEED), href=f"/jobs/{j.job_id}", markdown=md,
                   slack_text=st, links=links)


# ------------------------------------------------------------------------------------------------ gates
GATE_LABEL = {"plan_approval": "Plan approval", "publish_approval": "Publish approval", "clarification": "Clarification",
              "budget_extension": "Budget extension", "bundle_migration": "Bundle migration", "leak_purge": "Leak purge"}


@dataclass
class GateFacts:
    gate_id: str
    kind: str
    risk_level: str = "low"
    expires_at: dt.datetime | None = None
    opened_at: dt.datetime | None = None
    gate_url: str = ""
    suggested_login: str | None = None
    revision: int | None = None
    presented_digest: str | None = None
    job_id: str | None = None
    decidable_in_slack: bool = False


def gate_requested(j: JobFacts, g: GateFacts) -> Message:
    label = GATE_LABEL.get(g.kind, g.kind)
    head = f"Approval needed ({label.lower()}, {g.risk_level} risk)"
    exp = f"expires {utc_clock(g.expires_at)}" if g.expires_at else ""
    st = (f"*{head}*\n>{esc_slack(one_line(j.title, 300))}\n`{esc_slack(j.repo_full)}`"
          + (f" · requested by {esc_slack(j.requester_login)}" if j.requester_login else ""))
    fields = [("Gate", label), ("Risk", g.risk_level)] + ([("Expires", utc_clock(g.expires_at))] if g.expires_at else [])
    m = Message(title=f"{label} · {g.risk_level} risk", href=f"/approvals/{g.gate_id}",
                summary=one_line(f"{head}: {j.repo_short}" + (f" · {exp}" if exp else ""), MAX_FEED),
                markdown="", slack_text=st, fields=fields, context="Also decidable in the Replicant UI and by API.",
                links=[Link("Review in Replicant", g.gate_url, not g.decidable_in_slack), Link("Open job", j.job_url)])
    m.actions = gate_actions(g)
    if g.presented_digest:
        m.gate_ref = {"gate_id": g.gate_id, "revision": g.revision or 1, "presented_digest": g.presented_digest}
    return m


def gate_actions(g: GateFacts) -> list[dict[str, Any]]:
    """approvals.md §4.3 decision buttons (Approve with a confirm dialog; Reject opens a modal for the required
    comment). Only when the gate admits the `slack` channel and has a presented digest; `critical` gates are decided
    in the UI only (A-7)."""
    if not (g.decidable_in_slack and g.presented_digest) or g.risk_level == "critical":
        return []
    import json as _json
    v = _json.dumps({"j": g.job_id, "g": g.gate_id, "r": g.revision or 1, "d": g.presented_digest},
                    separators=(",", ":"))
    return [{"action_id": "gate_approve", "label": "Approve", "style": "primary", "value": v,
             "confirm": {"title": "Approve and publish?",
                         "text": "Replicant will publish exactly the diff in this approval and open the PR.",
                         "confirm": "Approve", "deny": "Back"}},
            {"action_id": "gate_reject", "label": "Reject", "style": "danger", "value": v}]


DECISION_VERB = {"approve": "Approved", "reject": "Rejected", "request_changes": "Changes requested",
                 "answer": "Answered", "rerun_qa": "QA re-run requested"}
CHANNEL_LABEL = {"ui": "the UI", "cli": "the CLI", "api": "the API", "slack": "Slack", "github": "GitHub",
                 "gitea": "Gitea", "system": "Replicant"}


def gate_resolution_line(state: str | None, decision: str | None, channel: str | None, who: str | None,
                         at: dt.datetime | None) -> str:
    """approvals.md §4.5: the context line that replaces a gate message's buttons in every channel."""
    if state == "expired":
        return "Expired · the job ended without approval"
    if state == "cancelled":
        return "Cancelled · this gate is closed"
    verb = DECISION_VERB.get(decision or "", (decision or state or "Decided").replace("_", " ").capitalize())
    mark = "\u2713 " if decision == "approve" else ""
    parts = [f"{mark}{verb}" + (f" by {who}" if who else "") + (f" in {CHANNEL_LABEL.get(channel, channel)}" if channel else "")]
    if at:
        parts.append(at.astimezone(dt.UTC).strftime("%H:%M UTC"))
    return " · ".join(parts)


def thread_parent(j: JobFacts) -> dict[str, Any]:
    """The parent message of a job's thread in the approval channel (NOTIFY-D13); events reply under it."""
    st = (f">{esc_slack(one_line(j.title, 300))}\n`{esc_slack(j.repo_full)}`"
          + (f" · requested by {esc_slack(j.requester_login)}" if j.requester_login else "")
          + f" · `{j.job_id}`")
    return {"title": f"Replicant job · {j.repo_short or 'job'}", "text": st,
            "links": [{"label": "Open in Replicant", "url": j.job_url}]}


def gate_waiting(j: JobFacts, g: GateFacts) -> Message:
    label = GATE_LABEL.get(g.kind, g.kind)
    who = f" (suggested: {g.suggested_login})" if g.suggested_login else ""
    exp = f" · expires {utc_clock(g.expires_at)}" if g.expires_at else ""
    head = f"{label} needed{who}{exp}"
    md = f"{_mention_md(j.requester_login)}**{esc_md(head)}**\n\nJob {_job_ref_md(j)} · [open the approval]({g.gate_url})"
    return Message(title=head[:150], summary=one_line(head, MAX_FEED), href=f"/jobs/{j.job_id}", markdown=md,
                   slack_text=f"*{esc_slack(head)}*", links=[Link("Open in Replicant", j.job_url, True)])


def gate_decided(j: JobFacts, g: GateFacts, decision: str | None, channel: str | None, state: str | None) -> Message:
    label = GATE_LABEL.get(g.kind, g.kind)
    if state == "expired":
        head = f"{label} expired"
    else:
        verb = {"approve": "approved", "reject": "rejected", "request_changes": "changes requested",
                "answer": "answered", "rerun_qa": "QA re-run requested"}.get(decision or "", decision or "decided")
        head = f"{label} {verb}" + (f" in {channel.upper() if channel in ('ui', 'api', 'cli') else channel}" if channel else "")
    md = f"{_mention_md(j.requester_login)}**{esc_md(head)}** · job {_job_ref_md(j)}"
    return Message(title=head, summary=one_line(head, MAX_FEED), href=f"/jobs/{j.job_id}", markdown=md,
                   slack_text=f"*{esc_slack(head)}*", links=[Link("Open in Replicant", j.job_url, True)])


def gate_escalated(j: JobFacts, g: GateFacts, level: int) -> Message:
    label = GATE_LABEL.get(g.kind, g.kind)
    head = f"Escalated (level {level}) · {label.lower()} waiting"
    st = f"*{head}*\n>{esc_slack(one_line(j.title, 300))}\n`{esc_slack(j.repo_full)}`"
    return Message(title=head, summary=one_line(f"{head}: {j.repo_short}", MAX_FEED), href=f"/approvals/{g.gate_id}",
                   slack_text=st, links=[Link("Review in Replicant", g.gate_url, True)])


# ------------------------------------------------------------------------------------------------ consolidated reminder
@dataclass
class ReminderItem:
    kind: str                 # gate | pr
    title: str
    repo_short: str
    url: str                  # where to act (gate page / PR)
    waiting_s: float
    expires_at: dt.datetime | None = None
    pr_number: int | None = None
    job_url: str = ""


def consolidated_reminder(items: list[ReminderItem], now: dt.datetime, inbox_url: str) -> Message:
    """R2-024: one reminder DM per person per slot: "12 waiting for you · 3 expire today · oldest 2 d", with the
    three most urgent linked (gates expiring first, then the longest-waiting PRs)."""
    gates = [i for i in items if i.kind == "gate"]
    prs = [i for i in items if i.kind == "pr"]
    today_end = now.replace(hour=23, minute=59, second=59)
    expiring = [g for g in gates if g.expires_at and g.expires_at <= today_end]
    oldest = max((i.waiting_s for i in items), default=0)
    parts = [f"{len(items)} waiting for you"]
    if expiring:
        parts.append(f"{len(expiring)} expire today")
    parts.append(f"oldest {human_duration(oldest)}")
    head = " · ".join(parts)
    urgent = sorted(gates, key=lambda g: (g.expires_at or now + dt.timedelta(days=365))) + \
        sorted(prs, key=lambda p: -p.waiting_s)
    lines = []
    for i in urgent[:3]:
        what = f"PR #{i.pr_number}" if i.kind == "pr" else "Approval"
        lines.append(f"• <{i.url}|{what}> · `{esc_slack(i.repo_short)}` · waiting {human_duration(i.waiting_s)}"
                     + (f" · expires {utc_clock(i.expires_at)}" if i.expires_at else "")
                     + f"\n   {esc_slack(one_line(i.title, 120))}")
    more = len(items) - len(lines)
    st = f"*{head}*\n" + "\n".join(lines) + (f"\n…and {more} more in your inbox." if more > 0 else "")
    counts = []
    if gates:
        counts.append(("Approvals", str(len(gates))))
    if prs:
        counts.append(("Pull requests", str(len(prs))))
    return Message(title="Waiting on you", summary=one_line(head, MAX_FEED), href="/waiting", slack_text=st,
                   fields=counts, links=[Link("Open your inbox", inbox_url, True)])


def digest(items: list[dict[str, Any]], now: dt.datetime, inbox_url: str, label: str) -> Message:
    """Daily digest DM (notifications.md §4, NOTIFY-D14): everything waiting on the person, grouped by kind, up to
    ten linked; replaces per-item reminder DMs for people above REPLICANT_NOTIFY_DIGEST_THRESHOLD items."""
    gates = [i for i in items if i.get("kind") == "gate"]
    prs = [i for i in items if i.get("kind") == "pr"]
    head = f"Replicant digest · {len(items)} waiting for you"
    lines: list[str] = []

    def age(i: dict[str, Any]) -> str:
        t = None
        try:
            t = dt.datetime.fromisoformat(str(i.get("waiting_since", "")).replace("Z", "+00:00"))
        except ValueError:
            pass
        return human_duration((now - t).total_seconds()) if t else "?"
    for g in gates[:5]:
        lines.append(f"• <{g.get('url', inbox_url)}|{GATE_LABEL.get(g.get('gate_kind', ''), 'Approval')}> · "
                     f"`{esc_slack(str(g.get('repo', '')).rsplit('/', 1)[-1])}` · waiting {age(g)}\n   "
                     f"{esc_slack(one_line(g.get('title'), 120))}")
    for pr in prs[: max(0, 10 - len(lines))]:
        n = (pr.get("pr") or {}).get("number")
        lines.append(f"• <{(pr.get('pr') or {}).get('url') or inbox_url}|PR #{n}> · "
                     f"`{esc_slack(str(pr.get('repo', '')).rsplit('/', 1)[-1])}` · {pr.get('role', '')} · waiting {age(pr)}"
                     f"\n   {esc_slack(one_line(pr.get('title'), 120))}")
    more = len(items) - len(lines)
    st = f"*{head}*\n" + "\n".join(lines) + (f"\n…and {more} more in your inbox." if more > 0 else "")
    fields = [(k, str(v)) for k, v in (("Approvals", len(gates)), ("Pull requests", len(prs))) if v]
    return Message(title=f"Your Replicant digest · {label}", summary=one_line(head, MAX_FEED), href="/waiting",
                   slack_text=st, fields=fields, links=[Link("Open your inbox", inbox_url, True)])
