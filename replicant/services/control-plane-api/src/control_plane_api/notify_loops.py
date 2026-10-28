"""Notification background loops in control-plane-api (v0 scheduler design, DEVIATIONS NOTIFY-D2).

* Dispatcher (every REPLICANT_NOTIFY_DISPATCH_SECONDS): claims due `notify.deliveries` rows in state `queued`
  (a 60 s lease through `next_attempt_at`, so a crashed attempt is retried), pushes them to the channel adapter
  (`slack_dm` -> slack-integration, `github_comment` -> github-integration) and records `sent` / `suppressed` /
  `failed`, or retries with exponential backoff (5 s doubling to 10 min, REPLICANT_NOTIFY_MAX_ATTEMPTS). Reminder
  rows (`payload.consolidate`) due for the same recipient are merged into ONE DM (R2-024).
* Scheduler (every REPLICANT_NOTIFY_SWEEP_SECONDS): PR-waiting reminders (after REPLICANT_NOTIFY_PR_IDLE_SECONDS,
  then at most once per REPLICANT_NOTIFY_PR_REMIND_EVERY_SECONDS per person and job, only outside the person's
  quiet hours) and the job-lifetime escalation to tenant admins (REPLICANT_NOTIFY_LIFETIME_LEAD_SECONDS before
  REPLICANT_NOTIFY_JOB_MAX_AGE_DAYS). Gate reminders come from the workflow's durable timers (approvals.md §1) as
  `gate.reminder` intents and are consolidated by the dispatcher.

Both loops are singletons across replicas through Postgres advisory locks (like the action outbox).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

import httpx
from sqlalchemy import text

from control_plane_api import notify_render as R
from control_plane_api.config import read_secret
from control_plane_api.db import OPERATOR_SCOPE
from control_plane_api.locks import locked_loop
from control_plane_api.notify import NOTIFY_ROLE, NotifyService, Planned, utcnow, _ts
from control_plane_api.notify_policy import channels_for, quiet_hours

log = logging.getLogger("control_plane_api.notify")
DISPATCH_LOCK = (0x46414354, 3)
SCHEDULER_LOCK = (0x46414354, 4)
LEASE_S = 60
MAX_BACKOFF_S = 600


def backoff_seconds(attempts: int) -> int:
    return int(min(MAX_BACKOFF_S, 5 * 2 ** max(0, attempts - 1)))


def merge_reminders(rows: list[Any], now: dt.datetime, inbox_url: str) -> dict[str, Any]:
    """One consolidated Slack payload from several due reminder rows of one recipient."""
    items = []
    for r in rows:
        c = (r.payload or {}).get("consolidate") or {}
        since = _ts(c.get("waiting_since")) or now
        items.append(R.ReminderItem(c.get("kind", "pr"), c.get("title", ""), c.get("repo_short", ""), c.get("url", ""),
                                    (now - since).total_seconds(), _ts(c.get("expires_at")), c.get("pr_number"),
                                    c.get("job_url", "")))
    seen, uniq = set(), []
    for it in items:                       # the same gate/PR reminded twice in one slot is listed once
        k = (it.kind, it.url)
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    return R.consolidated_reminder(uniq, now, inbox_url).slack_payload()


class NotifyLoops:
    def __init__(self, svc: NotifyService, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.svc = svc
        self.db = svc.db
        self._transport = transport

    @property
    def ns(self):  # noqa: ANN201 - follows runtime overrides (dev-only PUT /v1/admin/dev/notify-overrides)
        return self.svc.ns

    # ------------------------------------------------------------------ adapters
    def _token(self) -> str | None:
        return read_secret(self.ns.delivery_token_file)

    async def _post(self, url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        tok = self._token()
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        async with httpx.AsyncClient(timeout=20, transport=self._transport) as c:
            r = await c.post(url, json=body, headers=headers)
        try:
            data = r.json()
        except ValueError:
            data = {"detail": r.text[:200]}
        return r.status_code, data if isinstance(data, dict) else {}

    async def send(self, row: Any, slack_override: dict[str, Any] | None = None) -> tuple[str, str | None, str | None]:
        """-> (outcome, external_ref, error) with outcome in sent | suppressed | failed | retry."""
        p = row.payload or {}
        try:
            if p.get("op"):
                op = p["op"]
                code, data = await self._post(
                    f"{self.ns.slack_integration_url}/internal/v1/gates/{op['gate_id']}/resolved",
                    {"delivery_id": row.delivery_id, "final_state": op.get("final_state"), "text": op.get("text")})
            elif row.channel in ("slack_dm", "slack_thread"):
                body = {**(p.get("slack") or {}), **(slack_override or {}), "delivery_id": row.delivery_id,
                        "recipient": row.recipient, "tenant_id": row.tenant_id, "event_key": row.event_key}
                code, data = await self._post(f"{self.ns.slack_integration_url}/internal/v1/notifications/deliver", body)
            elif row.channel == "github_comment":
                g = p.get("github") or {}
                body = {"delivery_id": row.delivery_id, "tenant_id": row.tenant_id, "event_key": row.event_key,
                        "job_id": row.job_id, "repo": g.get("repo"), "issue_number": g.get("issue_number"),
                        "body": g.get("body", "")}
                code, data = await self._post(f"{self.ns.github_integration_url}/internal/v1/notifications/deliver", body)
            else:
                return "failed", None, f"channel {row.channel} has no adapter in v0"
        except httpx.HTTPError as e:
            return "retry", None, type(e).__name__
        if code == 200 and data.get("state") in ("sent", "suppressed"):
            return data["state"], data.get("external_ref"), data.get("reason")
        if code in (429,) or code >= 500:
            return "retry", None, f"http_{code}"
        return "failed", None, f"http_{code} {str(data.get('code') or data.get('detail') or '')[:120]}"

    async def _tenants(self) -> list[str]:
        async with self.db.tx(scope=OPERATOR_SCOPE) as conn:
            return [r.tenant_id for r in (await conn.execute(text(
                "SELECT DISTINCT tenant_id FROM notify.deliveries WHERE state = 'queued' AND next_attempt_at <= now()"
            ))).all()]

    async def dispatch_once(self, limit: int = 50) -> int:
        n = 0
        for tenant in await self._tenants():
            async with self.db.tx(role=NOTIFY_ROLE, tenant=tenant) as conn:
                rows = (await conn.execute(text(
                    "UPDATE notify.deliveries SET next_attempt_at = now() + make_interval(secs => :lease) "
                    "WHERE delivery_id IN (SELECT delivery_id FROM notify.deliveries WHERE state = 'queued' AND "
                    "channel IN ('slack_dm', 'slack_thread', 'github_comment') AND next_attempt_at <= now() ORDER BY next_attempt_at, "
                    "created_at LIMIT :n FOR UPDATE SKIP LOCKED) RETURNING delivery_id, tenant_id, event_key, job_id, "
                    "gate_id, recipient, channel, payload, attempts, created_at"), {"lease": LEASE_S, "n": limit})).all()
            groups: list[list[Any]] = []
            by_rcpt: dict[str, list[Any]] = {}
            for r in sorted(rows, key=lambda x: (x.created_at, x.delivery_id)):
                if r.channel == "slack_dm" and (r.payload or {}).get("consolidate"):
                    by_rcpt.setdefault(r.recipient, []).append(r)
                else:
                    groups.append([r])
            groups += list(by_rcpt.values())
            for grp in groups:
                head = grp[0]
                override = merge_reminders(grp, utcnow(), f"{self.ns.web_url}/waiting") if len(grp) > 1 or \
                    (head.payload or {}).get("consolidate") else None
                outcome, ref, err = await self.send(head, override)
                await self._record(tenant, grp, outcome, ref, err)
                n += len(grp)
        return n

    async def _record(self, tenant: str, grp: list[Any], outcome: str, ref: str | None, err: str | None) -> None:
        ids = [r.delivery_id for r in grp]
        attempts = max(r.attempts for r in grp) + 1
        async with self.db.tx(role=NOTIFY_ROLE, tenant=tenant) as conn:
            if outcome == "sent":
                await conn.execute(text("UPDATE notify.deliveries SET state = 'sent', sent_at = now(), external_ref = :x, "
                                        "attempts = :a, last_error = NULL WHERE delivery_id = ANY(:ids)"),
                                   {"x": ref, "a": attempts, "ids": ids})
            elif outcome == "suppressed":
                await conn.execute(text("UPDATE notify.deliveries SET state = 'suppressed', suppressed_reason = :r, "
                                        "attempts = :a WHERE delivery_id = ANY(:ids)"),
                                   {"r": (err or "suppressed")[:200], "a": attempts, "ids": ids})
            elif outcome == "failed" or attempts >= self.ns.max_attempts:
                await conn.execute(text("UPDATE notify.deliveries SET state = 'failed', last_error = :e, attempts = :a "
                                        "WHERE delivery_id = ANY(:ids)"), {"e": (err or "")[:300], "a": attempts, "ids": ids})
            else:
                await conn.execute(text("UPDATE notify.deliveries SET attempts = :a, last_error = :e, next_attempt_at = "
                                        "now() + make_interval(secs => :b) WHERE delivery_id = ANY(:ids)"),
                                   {"a": attempts, "e": (err or "")[:300], "b": backoff_seconds(attempts), "ids": ids})
        level = logging.INFO if outcome in ("sent", "suppressed", "retry") else logging.WARNING
        log.log(level, json.dumps({"event": f"notification.{outcome}", "deliveries": ids, "channel": grp[0].channel,
                                   "event_key": grp[0].event_key, "job_id": grp[0].job_id, "error": err}))

    # ------------------------------------------------------------------ scheduler
    async def sweep_once(self, now: dt.datetime | None = None) -> dict[str, int]:
        now = now or utcnow()
        return {"pr_reminders": await self.pr_reminders(now), "lifetime": await self.lifetime_escalations(now),
                "digests": await self.digests(now)}

    async def _last_reminded(self, job_id: str) -> dict[str, dt.datetime]:
        async with self.db.tx(scope=OPERATOR_SCOPE) as conn:
            rows = (await conn.execute(text(
                "SELECT recipient, max(created_at) AS at FROM notify.deliveries WHERE job_id = :j AND "
                "event_key = 'job.pr_reminder' GROUP BY recipient"), {"j": job_id})).all()
        return {r.recipient: r.at for r in rows}

    async def pr_reminders(self, now: dt.datetime) -> int:
        idle = dt.timedelta(seconds=self.ns.pr_idle_seconds)
        every = dt.timedelta(seconds=self.ns.pr_remind_every_seconds)
        async with self.db.tx(scope=OPERATOR_SCOPE) as conn:
            prs = (await conn.execute(text(
                "SELECT p.job_id, p.tenant_id, p.pr_number, p.opened_at FROM jobs_read.job_prs p JOIN jobs_read.jobs j "
                "USING (job_id) WHERE p.state = 'open' AND p.job_open AND NOT j.is_terminal AND p.opened_at <= :cut"),
                {"cut": now - idle})).all()
        total = 0
        slot = int(now.timestamp() // max(1, self.ns.pr_remind_every_seconds))
        for pr in prs:
            ctx = await self.svc.job_ctx(pr.job_id)
            if ctx is None or not ctx.requester:
                continue
            f = ctx.facts
            people = [ctx.requester] + [p for p in await self.svc.publish_approvers(pr.job_id) if p != ctx.requester]
            info = await self.svc.prefs_and_tz(people)
            last = await self._last_reminded(pr.job_id)
            since = _ts(pr.opened_at) or now
            waited = (now - since).total_seconds()
            item = R.ReminderItem("pr", f.title, f.repo_short, f.pr_url or f.job_url, waited, None, f.pr_number, f.job_url)
            plans: list[Planned] = []
            due = []
            for p in people:
                prefs, tz, _ = info.get(p, ({}, None, None))
                if last.get(p) and now - last[p] < every:
                    continue                                     # once per period per person and job
                if quiet_hours(prefs, tz, self.ns).active(now):
                    continue                                     # retried by a later sweep, outside quiet hours
                due.append(p)
                chans = [c for c in channels_for("job.pr_reminder", prefs, ctx.originating) if c in ("ui", "slack_dm")]
                if await self.svc.waiting_count(p) > self.ns.digest_threshold and self.svc._mark_digest(p):
                    chans = [c for c in chans if c != "slack_dm"]   # the daily digest carries it (NOTIFY-D14)
                msg = R.pr_reminder_comment(f, waited, since, [])
                plans.append(Planned(p, "job.pr_reminder", msg, f"{pr.job_id}:s{slot}", pr.job_id,
                                     consolidate_item=item, channels=chans))
            if due and ctx.originating and ctx.requester in due:
                logins = [info[p][2] for p in due if info.get(p) and info[p][2]]
                plans.append(Planned(ctx.requester, "job.pr_reminder", R.pr_reminder_comment(f, waited, since, logins),
                                     f"{pr.job_id}:s{slot}", pr.job_id, channels=["github_comment"]))
                if (chan := await self.svc.approval_channel(pr.tenant_id)):
                    plans.append(self.svc.thread_plan(chan, ctx, "job.pr_reminder", R.Message(
                        title=f"PR #{f.pr_number} still waiting for review",
                        summary=f"PR #{f.pr_number} still waiting for review",
                        href=f"/jobs/{pr.job_id}", slack_text=f"*PR #{f.pr_number} is still waiting for review* "
                        f"({R.human_duration(waited)})", links=[R.Link(f"Review PR #{f.pr_number}", f.pr_url or f.job_url)]),
                        f"{pr.job_id}:s{slot}"))
            total += await self.svc.deliver(pr.tenant_id, ctx, plans, now)
        return total

    async def digests(self, now: dt.datetime) -> int:
        """Daily digest DM per person (notifications.md §4, NOTIFY-D14): at each of the person's digest times
        (preferences `digest.times`, default REPLICANT_NOTIFY_DIGEST_TIMES) in their timezone, weekdays only unless
        they say otherwise, outside quiet hours, once per slot, when something is waiting on them."""
        total = 0
        users = self.svc.users()
        info = await self.svc.prefs_and_tz([u.principal_id for u in users])
        for u in users:
            prefs, tz, _ = info.get(u.principal_id, ({}, None, None))
            d = (prefs or {}).get("digest") or {}
            if d.get("enabled") is False:
                continue
            qh = quiet_hours(prefs, tz, self.ns)
            local = now.astimezone(qh.tz)
            weekdays_only = d.get("weekdays_only", self.ns.digest_weekdays_only)
            if weekdays_only and local.weekday() >= 5:
                continue
            for t in d.get("times") or list(self.ns.digest_times):
                try:
                    hh, mm = (int(x) for x in str(t).split(":"))
                except ValueError:
                    continue
                slot = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if not (slot <= local < slot + dt.timedelta(seconds=self.ns.digest_grace_seconds)):
                    continue
                if qh.active(now):
                    continue                                     # a later sweep inside the grace window sends it
                doc = await self.svc.waiting_doc(u)
                if not doc["items"]:
                    continue
                label = f"{slot.strftime('%a %b %d %H:%M')} {slot.tzname() or ''}".strip()
                msg = R.digest(doc["items"], now, f"{self.ns.web_url}/waiting", label)
                tenant = doc["items"][0].get("tenant_id") or "local-dev"
                total += await self.svc.deliver(tenant, None, [Planned(
                    u.principal_id, "digest.daily", msg, f"{slot.date().isoformat()}T{hh:02d}{mm:02d}")], now)
        return total

    async def lifetime_escalations(self, now: dt.datetime) -> int:
        cut = now - dt.timedelta(seconds=self.ns.job_max_age_days * 86400 - self.ns.lifetime_lead_seconds)
        async with self.db.tx(scope=OPERATOR_SCOPE) as conn:
            jobs = (await conn.execute(text(
                "SELECT job_id, tenant_id, created_at FROM jobs_read.jobs WHERE NOT is_terminal AND created_at <= :cut "
                "AND workflow_kind = 'ReplicantJob' LIMIT 200"), {"cut": cut})).all()
        total = 0
        for j in jobs:
            ctx = await self.svc.job_ctx(j.job_id)
            if ctx is None:
                continue
            age = (now - _ts(j.created_at)).total_seconds()
            msg = R.lifetime_escalation(ctx.facts, age, self.ns.job_max_age_days)
            plans = [Planned(a, "job.lifetime_escalation", msg, j.job_id, j.job_id)
                     for a in self.svc.tenant_admins(j.tenant_id)]
            total += await self.svc.deliver(j.tenant_id, ctx, plans, now)
        return total

    # ------------------------------------------------------------------ loops
    async def dispatcher_loop(self) -> None:
        await locked_loop(self.db, DISPATCH_LOCK, self.ns.dispatch_seconds, self.dispatch_once,
                          "notify dispatcher", retry_s=max(self.ns.dispatch_seconds, 5))

    async def scheduler_loop(self) -> None:
        await locked_loop(self.db, SCHEDULER_LOCK, self.ns.sweep_seconds, self.sweep_once,
                          "notify scheduler", retry_s=max(self.ns.sweep_seconds, 5))
