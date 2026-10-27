"""Notification policy (notifications.md §1-§3): default channels per event key, preferences, quiet hours, and the
v0 knobs (environment). Pure functions; the service in notify.py applies them."""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Delivery-channel vocabulary (R1-179). `originating` resolves to the job's source channel before a delivery row is
# written; `none` suppresses. v0 has no digest builder: `digest` is delivered as a Slack DM, deferred by quiet hours.
DELIVERY_CHANNELS = ("ui", "slack_dm", "slack_thread", "slack_channel", "github_comment", "jira_comment", "digest")

# notifications.md §2 defaults, plus v0 additions marked (+v0) and logged in DEVIATIONS (NOTIFY-D3).
DEFAULT_CHANNELS: dict[str, tuple[str, ...]] = {
    "job.started": ("originating", "ui"),
    "job.gate_waiting": ("originating", "ui"),
    "gate.requested": ("slack_dm", "ui"),                 # spec: slack_thread (approval channel) + DM; v0 DMs all (≤ 5)
    "gate.reminder": ("slack_dm", "ui"),                  # consolidated per approver per slot (R2-024)
    "gate.escalated": ("slack_dm", "ui"),                 # spec: slack_channel (escalation target); v0 DMs the targets
    "gate.decided": ("originating", "ui"),
    "gate.expiring": ("ui", "slack_dm"),
    "job.gate_held": ("ui", "slack_dm"),
    "job.pr_opened": ("originating", "ui", "slack_dm"),   # (+v0) slack_dm: the requester must act (merge/close)
    "job.pr_reminder": ("originating", "ui", "slack_dm"), # (+v0) PR-waiting reminder, consolidated per person
    "job.succeeded": ("originating", "ui", "digest"),     # (+v0) originating: the issue gets the "merged" comment
    "job.failed": ("originating", "ui", "slack_dm"),
    "job.cancelled": ("originating", "ui"),
    "job.rejected": ("originating", "ui"),
    "job.expired": ("originating", "ui"),
    "job.taken_over": ("ui",),
    "job.lifetime_escalation": ("slack_dm", "ui"),       # (+v0) tenant admins, before job_max_age_days
    "job.paused_identity": ("slack_dm", "ui"),
    "job.requester_deprovisioned": ("slack_dm", "ui"),
    "job.gate_overdue": ("ui", "slack_dm"),
    "digest.daily": ("slack_dm", "ui"),                   # (+v0) daily "waiting on you" digest (NOTIFY-D14)
}
GENERIC_CHANNELS: tuple[str, ...] = ("ui",)

# N-4: obligations that preferences cannot silence (they always reach at least one channel).
OBLIGATIONS = {"gate.requested", "gate.escalated", "job.lifetime_escalation"}
# Quiet hours delay these (notifications.md §2): reminders, job.succeeded and digest-eligible events. Never
# escalations, and never gate.expiring within 2 h of expiry.
QUIET_DELAYED = {"gate.reminder", "job.pr_reminder", "job.succeeded"}


def env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass(frozen=True)
class NotifySettings:
    web_url: str = "http://app.replicant.localhost:8400"
    scm_web_urls: dict[str, str] = field(default_factory=dict)       # repo host -> browser base URL
    pr_idle_seconds: int = 86400
    pr_remind_every_seconds: int = 86400
    job_max_age_days: int = 60
    lifetime_lead_seconds: int = 7 * 86400
    sweep_seconds: float = 30.0
    dispatch_seconds: float = 2.0
    max_attempts: int = 12
    quiet_default: str = "20:00-08:00,weekends"
    default_timezone: str = "UTC"
    delivery_token_file: str | None = None
    slack_integration_url: str = "http://slack-integration:8080"
    github_integration_url: str = "http://github-integration:8080"
    max_dm_recipients: int = 5
    digest_threshold: int = 5                   # more waiting items than this: reminders only through the digest
    digest_times: tuple[str, ...] = ("09:30",)   # default when the person's preferences set none (§4)
    digest_weekdays_only: bool = True
    digest_grace_seconds: int = 6 * 3600         # a digest slot is still sent this long after its time

    @classmethod
    def from_env(cls, web_origin: str = "http://app.replicant.localhost:8400") -> "NotifySettings":
        scm: dict[str, str] = {}
        for pair in env("REPLICANT_NOTIFY_SCM_WEB_URLS", "").split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                scm[k.strip()] = v.strip().rstrip("/")
        return cls(
            web_url=env("REPLICANT_NOTIFY_WEB_URL", web_origin).rstrip("/"),
            scm_web_urls=scm,
            pr_idle_seconds=int(env("REPLICANT_NOTIFY_PR_IDLE_SECONDS", "86400")),
            pr_remind_every_seconds=int(env("REPLICANT_NOTIFY_PR_REMIND_EVERY_SECONDS", "86400")),
            job_max_age_days=int(env("REPLICANT_NOTIFY_JOB_MAX_AGE_DAYS", "60")),
            lifetime_lead_seconds=int(env("REPLICANT_NOTIFY_LIFETIME_LEAD_SECONDS", str(7 * 86400))),
            sweep_seconds=float(env("REPLICANT_NOTIFY_SWEEP_SECONDS", "30")),
            dispatch_seconds=float(env("REPLICANT_NOTIFY_DISPATCH_SECONDS", "2")),
            max_attempts=int(env("REPLICANT_NOTIFY_MAX_ATTEMPTS", "12")),
            digest_threshold=int(env("REPLICANT_NOTIFY_DIGEST_THRESHOLD", "5")),
            digest_times=tuple(x.strip() for x in env("REPLICANT_NOTIFY_DIGEST_TIMES", "09:30").split(",") if x.strip()),
            digest_weekdays_only=env("REPLICANT_NOTIFY_DIGEST_WEEKDAYS_ONLY", "true") == "true",
            quiet_default=env("REPLICANT_NOTIFY_QUIET_HOURS", "20:00-08:00,weekends"),
            default_timezone=env("REPLICANT_NOTIFY_DEFAULT_TIMEZONE", "UTC"),
            delivery_token_file=os.environ.get("REPLICANT_NOTIFY_DELIVERY_TOKEN_FILE") or None,
            slack_integration_url=env("REPLICANT_SLACK_INTEGRATION_URL", "http://slack-integration:8080").rstrip("/"),
            github_integration_url=env("REPLICANT_GITHUB_INTEGRATION_URL", "http://github-integration:8080").rstrip("/"),
        )

    def scm_web(self, host: str) -> str:
        return self.scm_web_urls.get(host) or f"http://{host}"


# ------------------------------------------------------------------------------------------------ channels
def channels_for(event_key: str, prefs: dict[str, Any] | None, originating: str | None) -> list[str]:
    """Preferences override the default list for an event (§3); `originating` resolves to the job's source channel
    (or is dropped when the job has none); `none` suppresses except for obligations (N-4)."""
    want = list(DEFAULT_CHANNELS.get(event_key, GENERIC_CHANNELS))
    pref = ((prefs or {}).get("channels") or {}).get(event_key)
    if isinstance(pref, list) and pref:
        want = [c for c in pref if isinstance(c, str)]
        if "none" in want:
            want = [] if event_key not in OBLIGATIONS else ["ui"]
    out: list[str] = []
    for c in want:
        c = (originating or "") if c == "originating" else c
        if c in DELIVERY_CHANNELS and c not in out:
            out.append(c)
    if event_key in OBLIGATIONS and not out:
        out = ["ui"]
    return out


# ------------------------------------------------------------------------------------------------ quiet hours
@dataclass(frozen=True)
class QuietHours:
    start: dt.time | None           # None = no quiet window
    end: dt.time | None
    tz: dt.tzinfo
    weekends: bool = False

    def active(self, now: dt.datetime) -> bool:
        local = now.astimezone(self.tz)
        if self.weekends and local.weekday() >= 5:
            return True
        if self.start is None or self.end is None or self.start == self.end:
            return False
        t = local.time()
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end          # window over midnight

    def next_allowed(self, now: dt.datetime) -> dt.datetime:
        """The first instant at or after `now` outside quiet hours (minute resolution, at most 8 days ahead)."""
        t = now
        for _ in range(8 * 24 * 60):
            if not self.active(t):
                return t
            local = t.astimezone(self.tz)
            if self.weekends and local.weekday() >= 5:      # jump to next local midnight
                nxt = (local + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                t = nxt.astimezone(dt.UTC)
                continue
            if self.end is not None:
                cand = local.replace(hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0)
                if cand <= local:
                    cand += dt.timedelta(days=1)
                t = cand.astimezone(dt.UTC)
                continue
            t += dt.timedelta(minutes=1)
        return t


def _tz(name: str | None, default: str) -> dt.tzinfo:
    for n in (name, default, "UTC"):
        if n:
            try:
                return ZoneInfo(n)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return dt.UTC


def _hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def quiet_hours(prefs: dict[str, Any] | None, user_tz: str | None, settings: NotifySettings) -> QuietHours:
    """The principal's `quiet_hours` preference wins (start == end means none). Otherwise the platform default
    `REPLICANT_NOTIFY_QUIET_HOURS` (spec: 20:00-08:00 and weekends in the person's timezone; `off` disables)."""
    q = (prefs or {}).get("quiet_hours")
    if isinstance(q, dict) and q.get("start") and q.get("end"):
        return QuietHours(_hm(q["start"]), _hm(q["end"]), _tz(q.get("timezone") or user_tz, settings.default_timezone))
    d = (settings.quiet_default or "").strip().lower()
    tz = _tz(user_tz, settings.default_timezone)
    if d in ("", "off", "none"):
        return QuietHours(None, None, tz)
    start = end = None
    weekends = False
    for part in d.split(","):
        part = part.strip()
        if part == "weekends":
            weekends = True
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _hm(a), _hm(b)
    return QuietHours(start, end, tz, weekends)
