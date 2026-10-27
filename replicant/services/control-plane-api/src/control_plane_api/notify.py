"""Notification fan-out (notifications.md N-1..N-5): intents from the workflow's `notify.*` activities become
`notify.deliveries` rows (one per event, recipient and channel; `dedupe_key` unique, so activity retries never
double-post) and `notify.feed` items. Deliveries for external channels are pushed to the channel adapters by the
dispatcher (notify_loops.py) with retries; reminders and lifetime escalations come from the scheduler there.

Recipients (v0): the requester (projection / JobRequest), eligible approvers (static dev directory principals
holding `gate:decide` on the tenant, the same evaluator that authorizes decisions), tenant admins (role
`tenant_admin`), reviewers of an open PR (the principals who approved the job's publish gate). Gitea logins for
@mentions come from `identity.links` (kind gitea), else the directory login.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from control_plane_api import notify_render as R
from control_plane_api.auth import AuthContext, check
from control_plane_api.db import OPERATOR_SCOPE, Database
from control_plane_api.ids import derived
from control_plane_api.notify_policy import QUIET_DELAYED, NotifySettings, channels_for, quiet_hours

log = logging.getLogger("control_plane_api.notify")
NOTIFY_ROLE = "svc_cpa_notify"
TERMINAL_KINDS = {"succeeded", "failed", "cancelled", "rejected", "expired", "taken_over"}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _ts(v: Any) -> dt.datetime | None:
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.UTC)
    if isinstance(v, str) and v:
        try:
            t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
            return t if t.tzinfo else t.replace(tzinfo=dt.UTC)
        except ValueError:
            return None
    return None


def _iso(v: Any) -> Any:
    return v.astimezone(dt.UTC).isoformat().replace("+00:00", "Z") if isinstance(v, dt.datetime) else v


def short_hash(s: str, n: int = 16) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:n]


@dataclass
class JobCtx:
    facts: R.JobFacts
    requester: str | None
    status: str | None = None
    created_at: dt.datetime | None = None
    repo: dict[str, Any] | None = None          # C-JOBREQ target repo {provider, host, owner, name, repo_id}
    issue_number: int | None = None
    source_channel: str | None = None
    pr_opened_at: dt.datetime | None = None
    snapshot_gates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def originating(self) -> str | None:
        """The job's source channel as a delivery channel (GitHub and Gitea issues -> github_comment)."""
        if self.issue_number and self.repo and self.source_channel in ("github", "gitea", None):
            return "github_comment"
        return None


@dataclass
class Planned:
    recipient: str
    event_key: str
    msg: R.Message
    subject: str                                 # job / gate / slot the dedupe key is about
    job_id: str | None = None
    gate_id: str | None = None
    consolidate_item: R.ReminderItem | None = None
    channels: list[str] | None = None            # None = preferences/defaults
    mention_logins: list[str] = field(default_factory=list)
    thread: dict[str, Any] | None = None         # slack_thread: {channel_id, key, parent}
    op: dict[str, Any] | None = None             # adapter operation instead of a message (gate_resolved)


class NotifyService:
    def __init__(self, db: Database, auth: AuthContext, ns: NotifySettings) -> None:
        self.db = db
        self.auth = auth
        self.ns = ns

    # ------------------------------------------------------------------ lookups
    def job_url(self, job_id: str) -> str:
        return f"{self.ns.web_url}/jobs/{job_id}"

    def gate_url(self, gate_id: str) -> str:
        return f"{self.ns.web_url}/approvals/{gate_id}"

    def pr_web_url(self, repo: dict[str, Any] | None, number: int | None, fallback: str | None) -> str | None:
        if repo and number and repo.get("host") and repo.get("owner") and repo.get("name"):
            base = self.ns.scm_web(repo["host"])
            path = "pulls" if repo.get("provider", "gitea") == "gitea" else "pull"
            return f"{base}/{repo['owner']}/{repo['name']}/{path}/{number}"
        return fallback

    async def job_ctx(self, job_id: str, intent_vars: dict[str, Any] | None = None) -> JobCtx | None:
        async with self.db.tx(scope=OPERATOR_SCOPE) as conn:
            j = (await conn.execute(text(
                "SELECT j.job_id, j.tenant_id, j.title, j.requester, j.status, j.phase, j.repos, j.created_at, "
                "j.terminal_outcome, j.snapshot -> 'pull_request' AS pr, j.snapshot -> 'terminal' AS terminal, "
                "j.snapshot -> 'gates' AS gates FROM jobs_read.jobs j WHERE j.job_id = :j"), {"j": job_id})).first()
            r = (await conn.execute(text("SELECT tenant_id, requester, request, created_at FROM intake.job_requests "
                                         "WHERE job_id = :j"), {"j": job_id})).first()
            p = (await conn.execute(text("SELECT pr_number, url, state, opened_at FROM jobs_read.job_prs WHERE "
                                         "job_id = :j ORDER BY opened_at DESC LIMIT 1"), {"j": job_id})).first()
        if j is None and r is None:
            return None
        req = (r.request if r is not None else None) or {}
        tgt = (req.get("targets") or [{}])[0]
        repo = tgt.get("repo") or {}
        iv = intent_vars or {}
        pr = iv.get("pr") or (j.pr if j is not None else None) or {}
        title = (j.title if j is not None and j.title else None) or iv.get("title") or \
            (req.get("intent") or {}).get("title") or job_id
        pr_number = pr.get("number") or (p.pr_number if p is not None else None)
        pr_url = self.pr_web_url(repo, pr_number, pr.get("url") or (p.url if p is not None else None))
        term = (j.terminal if j is not None else None) or {}
        facts = R.JobFacts(
            job_id=job_id, tenant_id=(j.tenant_id if j is not None else r.tenant_id), title=title,
            repo_full=f"{repo.get('owner', '')}/{repo.get('name', '')}".strip("/"), repo_short=repo.get("name", ""),
            requester_login=((req.get("requester") or {}).get("channel_identity") or {}).get("login"),
            job_url=self.job_url(job_id), pr_number=pr_number, pr_url=pr_url, diff=pr.get("diff_stat"),
            phase=(j.phase if j is not None else None), outcome=iv.get("outcome") or term.get("outcome"),
            reason=iv.get("reason") or term.get("reason"))
        return JobCtx(facts=facts, requester=(j.requester if j is not None else r.requester),
                      status=(j.status if j is not None else None),
                      created_at=(j.created_at if j is not None else r.created_at), repo=repo or None,
                      issue_number=(tgt.get("issue") or {}).get("number"),
                      source_channel=(req.get("source") or {}).get("channel"),
                      pr_opened_at=(p.opened_at if p is not None else None),
                      snapshot_gates=list((j.gates if j is not None else None) or []))

    async def gate_row(self, gate_id: str) -> Any:
        async with self.db.tx(scope=OPERATOR_SCOPE) as conn:
            return (await conn.execute(text("SELECT * FROM approvals.gates WHERE gate_id = :g"), {"g": gate_id})).first()

    def gate_facts(self, gate_id: str, g: Any, ctx: JobCtx | None, kind: str | None,
                   intent_vars: dict[str, Any] | None = None) -> R.GateFacts:
        """From approvals.gates when projected, else the job snapshot, else the intent itself (the workflow notifies
        before the projector writes the gate row)."""
        snap = next((x for x in (ctx.snapshot_gates if ctx else []) if x.get("gate_id") == gate_id), {})
        iv = intent_vars or {}
        snap = {**{k: iv[k] for k in ("channels", "presented_digest", "expires_at", "opened_at", "kind") if iv.get(k)},
                **snap}
        risk = (g.risk_level if g is not None else None) or "low"
        sugg = g.suggested_approver if g is not None else None
        chans = list(g.channels or []) if g is not None else list(snap.get("channels") or [])
        return R.GateFacts(gate_id=gate_id, kind=(g.kind if g is not None else None) or kind or snap.get("kind", "gate"),
                           risk_level=risk, expires_at=_ts(g.expires_at if g is not None else snap.get("expires_at")),
                           opened_at=_ts(g.opened_at if g is not None else snap.get("opened_at")),
                           gate_url=self.gate_url(gate_id), suggested_login=self.login_of(sugg) if sugg else None,
                           revision=(g.packet_revision if g is not None else snap.get("packet_revision")),
                           presented_digest=(g.presented_digest if g is not None else snap.get("presented_digest")),
                           job_id=ctx.facts.job_id if ctx else None, decidable_in_slack="slack" in chans)

    def display_name(self, principal_id: str | None) -> str | None:
        login = self.login_of(principal_id)
        return login.capitalize() if login else None

    def _mark_digest(self, principal_id: str) -> bool:
        """Above the digest threshold the per-item reminder DM is replaced by the daily digest (NOTIFY-D14)."""
        log.info(json.dumps({"event": "notification.reminder_folded_into_digest", "recipient": principal_id}))
        return True

    def login_of(self, principal_id: str | None) -> str | None:
        p = self.auth.directory.get(principal_id) if principal_id else None
        return p.login if p is not None else None

    async def approval_channel(self, tenant_id: str) -> str | None:
        """tenancy.tenant_settings `approval_targets.slack.approval_channel` (operational config, ADR-013): the
        channel that gets one thread per job (NOTIFY-D13). None = no channel posts for this tenant."""
        async with self.db.tx(scope=OPERATOR_SCOPE) as conn:
            v = (await conn.execute(text(
                "SELECT settings -> 'approval_targets' -> 'slack' ->> 'approval_channel' FROM tenancy.tenant_settings "
                "WHERE tenant_id = :t AND is_current"), {"t": tenant_id})).scalar()
        return v or None

    def thread_plan(self, channel_id: str, ctx: JobCtx, event_key: str, msg: R.Message, subject: str,
                    gate_id: str | None = None) -> Planned:
        pl = Planned(f"channel:{channel_id}", event_key, msg, subject, ctx.facts.job_id, gate_id,
                     channels=["slack_thread"])
        pl.thread = {"channel_id": channel_id, "key": ctx.facts.job_id, "parent": R.thread_parent(ctx.facts)}
        return pl

    def user_principal(self, principal_id: str) -> Any:
        p = self.auth.directory.get(principal_id)
        return p if p is not None and p.is_user else None

    async def waiting_count(self, principal_id: str) -> int:
        p = self.user_principal(principal_id)
        return (await self.waiting_doc(p))["counts"]["total"] if p is not None else 0

    def users(self) -> list[Any]:
        return [p for p in self.auth.directory.all() if p.is_user]

    def eligible_approvers(self, tenant_id: str, requester: str | None, requester_may_decide: bool,
                           suggested: str | None = None) -> list[str]:
        out = [p.principal_id for p in self.users()
               if check(self.auth, p, "gate:decide", tenant_id=tenant_id)
               and (requester_may_decide or p.principal_id != requester)]
        out.sort(key=lambda x: (x != suggested, self.login_of(x) or x))
        return out[: self.ns.max_dm_recipients]

    def tenant_admins(self, tenant_id: str) -> list[str]:
        return sorted(p.principal_id for p in self.users() if "tenant_admin" in p.tenant_roles.get(tenant_id, ()))

    async def publish_approvers(self, job_id: str) -> list[str]:
        async with self.db.tx(scope=OPERATOR_SCOPE) as conn:
            rows = (await conn.execute(text(
                "SELECT DISTINCT winning_decision ->> 'principal_id' AS p FROM approvals.gates WHERE job_id = :j AND "
                "kind = 'publish_approval' AND state = 'approved' AND winning_decision ? 'principal_id'"),
                {"j": job_id})).all()
        return [r.p for r in rows if r.p]

    async def prefs_and_tz(self, principals: list[str]) -> dict[str, tuple[dict[str, Any], str | None, str | None]]:
        """principal -> (preferences, timezone, gitea login from identity.links)."""
        if not principals:
            return {}
        async with self.db.tx(scope=OPERATOR_SCOPE) as conn:
            prefs = {r.principal_id: r.prefs for r in (await conn.execute(text(
                "SELECT principal_id, prefs FROM notify.preferences WHERE principal_id = ANY(:p)"),
                {"p": principals})).all()}
            tzs = {r.principal_id: r.timezone for r in (await conn.execute(text(
                "SELECT principal_id, timezone FROM identity.users WHERE principal_id = ANY(:p)"), {"p": principals})).all()}
            logins = {r.principal_id: r.login for r in (await conn.execute(text(
                "SELECT principal_id, login FROM identity.links WHERE principal_id = ANY(:p) AND kind = 'gitea' "
                "AND status = 'active'"), {"p": principals})).all()}
        return {p: (prefs.get(p) or {}, tzs.get(p), logins.get(p) or self.login_of(p)) for p in principals}

    # ------------------------------------------------------------------ "waiting on you"
    async def waiting_doc(self, principal: Any) -> dict[str, Any]:
        """Gates the principal can decide plus open PRs of non-terminal jobs where they are the requester or a
        reviewer (approved the publish gate). `GET /v1/me/waiting`, the digest and App Home use it."""
        now = utcnow()
        me = principal.principal_id
        db = self.db
        gate_tenants = principal.tenants_with("gate:decide")
        read_tenants = principal.tenants_with("job:read")
        items: list[dict[str, Any]] = []
        prs: list[Any] = []
        async with db.tx(scope=OPERATOR_SCOPE) as conn:
            if gate_tenants is None or gate_tenants:
                where, p = ["g.state = 'open'"], {}
                if gate_tenants is not None:
                    where.append("g.tenant_id = ANY(:t)")
                    p["t"] = sorted(gate_tenants)
                gates = (await conn.execute(text(
                    f"SELECT g.* FROM approvals.gates g WHERE {' AND '.join(where)} ORDER BY g.expires_at ASC NULLS LAST, "
                    "g.gate_id LIMIT 200"), p)).all()
                for g in gates:
                    if g.requester == me and not g.requester_may_decide:
                        continue
                    it = {"kind": "gate", "gate_id": g.gate_id, "job_id": g.job_id, "tenant_id": g.tenant_id,
                          "gate_kind": g.kind, "title": g.title or "", "repo": g.repo or "", "risk_level": g.risk_level or "low",
                          "requester": g.requester, "opened_at": _iso(g.opened_at), "waiting_since": _iso(g.opened_at),
                          "why": "suggested_approver" if g.suggested_approver == me else "eligible_approver",
                          "href": f"/approvals/{g.gate_id}"}
                    if g.expires_at:
                        it["expires_at"] = _iso(g.expires_at)
                    items.append(it)
            if read_tenants is None or read_tenants:
                p2: dict[str, Any] = {"me": me}
                tw = ""
                if read_tenants is not None:
                    tw = "AND j.tenant_id = ANY(:t)"
                    p2["t"] = sorted(read_tenants)
                prs = (await conn.execute(text(
                    "SELECT p.job_id, p.tenant_id, p.pr_number, p.url, p.state, p.opened_at, j.title, j.requester, "
                    "j.repos[1] AS repo, (j.requester = :me) AS is_requester FROM jobs_read.job_prs p JOIN jobs_read.jobs j "
                    "USING (job_id) WHERE p.state IN ('open', 'draft') AND p.job_open AND NOT j.is_terminal " + tw + " AND "
                    "(j.requester = :me OR EXISTS (SELECT 1 FROM approvals.gates g WHERE g.job_id = p.job_id AND "
                    "g.kind = 'publish_approval' AND g.state = 'approved' AND g.winning_decision ->> 'principal_id' = :me)) "
                    "ORDER BY p.opened_at LIMIT 200"), p2)).all()
                last = {}
                if prs:
                    last = {r.job_id: r.at for r in (await conn.execute(text(
                        "SELECT job_id, max(created_at) AS at FROM notify.deliveries WHERE recipient = :me AND "
                        "event_key = 'job.pr_reminder' AND job_id = ANY(:j) GROUP BY job_id"),
                        {"me": me, "j": [r.job_id for r in prs]})).all()}
        svc = self
        for r in prs:
            host, _, path = (r.repo or "").partition("/")
            owner, _, name = path.partition("/")
            url = svc.pr_web_url({"host": host, "owner": owner, "name": name, "provider": "gitea"}, r.pr_number, r.url) \
                if host else r.url
            opened = _ts(r.opened_at)
            items.append({"kind": "pr", "job_id": r.job_id, "tenant_id": r.tenant_id, "title": r.title or "",
                          "repo": r.repo or "", "requester": r.requester, "role": "requester" if r.is_requester else "reviewer",
                          "pr": {"number": r.pr_number, "url": url, "state": r.state, "opened_at": _iso(r.opened_at)},
                          "waiting_since": _iso(r.opened_at),
                          "idle_seconds": int((now - opened).total_seconds()) if opened else None,
                          "last_reminded_at": _iso(last.get(r.job_id)), "href": f"/jobs/{r.job_id}"})
        n_g = sum(1 for i in items if i["kind"] == "gate")
        return {"items": items, "counts": {"gates": n_g, "prs": len(items) - n_g, "total": len(items)}, "as_of": _iso(now)}

    # ------------------------------------------------------------------ intents (POST /internal/v1/notifications)
    async def accept_intent(self, body: dict[str, Any]) -> dict[str, Any]:
        ek = body["event_key"]
        tenant = body["tenant_id"]
        job_id, gate_id = body.get("job_id"), body.get("gate_id")
        v = body.get("vars") or {}
        intent = short_hash(body["dedupe_key"])
        if body.get("operator") or ek.startswith("ops."):
            log.info(json.dumps({"event": "notification.operator", "event_key": ek, "tenant_id": tenant, "job_id": job_id}))
            return {"accepted": True, "deliveries": 0}
        ctx = await self.job_ctx(job_id, v) if job_id else None
        if ctx is None:
            log.info(json.dumps({"event": "notification.no_job", "event_key": ek, "job_id": job_id}))
            return {"accepted": True, "deliveries": 0}
        f = ctx.facts
        plans: list[Planned] = []
        subj = f"{job_id}:{intent}"
        if ek in ("gate.opened", "gate.reminder", "gate.escalated", "gate.resolved"):
            g = await self.gate_row(gate_id) if gate_id else None
            gf = self.gate_facts(gate_id or "", g, ctx, v.get("kind"), v)
            may = bool(g.requester_may_decide) if g is not None else False
            approvers = self.eligible_approvers(tenant, ctx.requester, may, g.suggested_approver if g is not None else None)
            gsubj = f"{gate_id}:{intent}"
            chan = await self.approval_channel(tenant)
            if ek == "gate.opened":
                plans += [Planned(p, "gate.requested", R.gate_requested(f, gf), gsubj, job_id, gate_id) for p in approvers]
                if chan:
                    plans.append(self.thread_plan(chan, ctx, "gate.requested", R.gate_requested(f, gf), gsubj, gate_id))
                if ctx.requester:
                    plans.append(Planned(ctx.requester, "job.gate_waiting", R.gate_waiting(f, gf), gsubj, job_id, gate_id))
            elif ek == "gate.reminder":
                now = utcnow()
                item = R.ReminderItem("gate", f.title, f.repo_short, gf.gate_url,
                                      (now - gf.opened_at).total_seconds() if gf.opened_at else 0, gf.expires_at,
                                      job_url=f.job_url)
                msg = R.consolidated_reminder([item], now, f"{self.ns.web_url}/waiting")
                for p in approvers:
                    folded = await self.waiting_count(p) > self.ns.digest_threshold and self._mark_digest(p)
                    plans.append(Planned(p, "gate.reminder", msg, gsubj, job_id, gate_id, consolidate_item=item,
                                         channels=["ui"] if folded else None))
            elif ek == "gate.escalated":
                lvl = int(v.get("level") or 1)
                targets = list(dict.fromkeys(self.tenant_admins(tenant) + (approvers if lvl < 2 else [])))
                plans += [Planned(p, "gate.escalated", R.gate_escalated(f, gf, lvl), gsubj, job_id, gate_id) for p in targets]
            else:
                state = "expired" if v.get("state") == "expired" else (g.state if g is not None else None)
                msg = R.gate_decided(f, gf, v.get("decision"), v.get("channel"), state)
                if ctx.requester:
                    plans.append(Planned(ctx.requester, "gate.decided", msg, gsubj, job_id, gate_id))
                who = self.display_name(v.get("principal_id") or ((g.winning_decision or {}).get("principal_id")
                                                                   if g is not None else None))
                line = R.gate_resolution_line(state, v.get("decision"), v.get("channel"), who,
                                              _ts(v.get("decided_at")) or utcnow())
                # approvals.md §4.5: every Slack message of the gate (DMs, channel thread) loses its buttons
                op = Planned(f"gate:{gate_id}", "gate.resolved", msg, gsubj, job_id, gate_id, channels=["slack_dm"])
                op.op = {"op": "gate_resolved", "gate_id": gate_id, "final_state": state or v.get("decision") or "closed",
                         "text": line}
                plans.append(op)
                if chan:
                    plans.append(self.thread_plan(chan, ctx, "gate.decided", R.Message(
                        title=line, summary=line, href=f"/jobs/{job_id}", slack_text=R.esc_slack(line)), gsubj, gate_id))
        elif ek == "job.pr_opened":
            if ctx.requester:
                plans.append(Planned(ctx.requester, "job.pr_opened", R.pr_opened(f), job_id, job_id))
            if (chan := await self.approval_channel(tenant)):
                plans.append(self.thread_plan(chan, ctx, ek, R.pr_opened(f), job_id))
        elif ek.startswith("job.") and ek[4:] in TERMINAL_KINDS:
            kind = ek[4:]
            msg = R.succeeded(f) if kind == "succeeded" else R.failed(f) if kind == "failed" else R.ended(f, kind)
            if ctx.requester:
                plans.append(Planned(ctx.requester, ek, msg, job_id, job_id))
            if (chan := await self.approval_channel(tenant)):
                plans.append(self.thread_plan(chan, ctx, ek, msg, job_id))
        elif ek == "job.pr_waiting_for_review":
            # The workflow's own PR-wait reminders (repair_limits.pr_wait.reminder_days) are folded into the
            # scheduler's daily consolidated reminder (R2-024): keep a feed item only.
            if ctx.requester:
                msg = R.Message(title="PR waiting for review", summary=R.one_line(
                    f"PR #{f.pr_number} has waited {v.get('days', '?')} d for review", R.MAX_FEED), href=f"/jobs/{job_id}")
                plans.append(Planned(ctx.requester, ek, msg, subj, job_id, channels=["ui"]))
        else:
            to = [body["recipients"]["principals"][0]] if (body.get("recipients") or {}).get("principals") else []
            roles = (body.get("recipients") or {}).get("roles") or []
            if "tenant_admins" in roles or v.get("audience") == "tenant_admins" or ek == "job.requester_deprovisioned":
                to += self.tenant_admins(tenant)
            if not to and ctx.requester:
                to = [ctx.requester]
            label = ek.split(".", 1)[-1].replace("_", " ")
            msg = R.Message(title=label.capitalize(), summary=R.one_line(f"{label.capitalize()} · {f.title}", R.MAX_FEED),
                            href=f"/jobs/{job_id}", slack_text=f"*{R.esc_slack(label.capitalize())}*\n>"
                            f"{R.esc_slack(R.one_line(f.title, 200))}", links=[R.Link("Open in Replicant", f.job_url, True)])
            plans += [Planned(p, ek, msg, subj, job_id, gate_id) for p in dict.fromkeys(to)]
        n = await self.deliver(tenant, ctx, plans)
        log.info(json.dumps({"event": "notification.accepted", "event_key": ek, "tenant_id": tenant, "job_id": job_id,
                             "gate_id": gate_id, "deliveries": n}))
        return {"accepted": True, "deliveries": n}

    # ------------------------------------------------------------------ planning -> rows
    async def deliver(self, tenant: str, ctx: JobCtx | None, plans: list[Planned], now: dt.datetime | None = None) -> int:
        """Writes one delivery per (plan, channel). Returns the number of new rows (duplicates are no-ops)."""
        if not plans:
            return 0
        now = now or utcnow()
        people = await self.prefs_and_tz(list({p.recipient for p in plans} | ({ctx.requester} if ctx and ctx.requester else set())))
        rows: list[dict[str, Any]] = []
        feed: list[dict[str, Any]] = []
        for pl in plans:
            prefs, tz, _login = people.get(pl.recipient, ({}, None, None))
            chans = pl.channels if pl.channels is not None else channels_for(pl.event_key, prefs, ctx.originating if ctx else None)
            qh = quiet_hours(prefs, tz, self.ns)
            for ch in chans:
                send_ch = "slack_dm" if ch == "digest" else ch          # v0: no digest builder (NOTIFY-D3)
                if send_ch not in ("ui", "slack_dm", "github_comment", "slack_thread"):
                    continue                                              # slack_channel, jira: not in v0
                if send_ch == "slack_thread" and not pl.thread:
                    continue
                if send_ch == "github_comment" and (ctx is None or not ctx.originating):
                    continue
                key_recipient = pl.recipient
                if send_ch == "github_comment":
                    # one comment per event on the job's issue, whoever the recipients are
                    key_recipient = f"issue:{ctx.repo.get('repo_id') if ctx and ctx.repo else ''}#{ctx.issue_number if ctx else ''}"
                dedupe = f"{pl.event_key}:{pl.subject}:{key_recipient}:{send_ch}"[:512]
                due = now
                if send_ch == "slack_thread" or pl.op:
                    pass                                                  # channel posts and updates: never deferred
                elif pl.event_key in QUIET_DELAYED or ch == "digest":
                    if send_ch == "github_comment" and ctx and ctx.requester:
                        rp, rtz, _ = people.get(ctx.requester, ({}, None, None))
                        due = quiet_hours(rp, rtz, self.ns).next_allowed(now)
                    else:
                        due = qh.next_allowed(now)
                payload: dict[str, Any] = {"summary": pl.msg.summary, "href": pl.msg.href}
                state = "queued"
                if send_ch == "ui":
                    state = "sent"
                    feed.append({"recipient": pl.recipient, "event_key": pl.event_key, "summary": pl.msg.summary[:300],
                                 "href": pl.msg.href, "dedupe": dedupe})
                elif pl.op:
                    payload["op"] = pl.op
                elif send_ch == "slack_thread":
                    payload["slack"] = {**pl.msg.slack_payload(), "event_key": pl.event_key, "tenant_id": tenant,
                                        "channel_id": pl.thread["channel_id"], "thread": pl.thread,
                                        **({"job_id": pl.job_id} if pl.job_id else {}),
                                        **({"gate_id": pl.gate_id} if pl.gate_id else {})}
                elif send_ch == "slack_dm":
                    payload["slack"] = {**pl.msg.slack_payload(), "event_key": pl.event_key, "recipient": pl.recipient,
                                        "tenant_id": tenant, **({"job_id": pl.job_id} if pl.job_id else {}),
                                        **({"gate_id": pl.gate_id} if pl.gate_id else {})}
                    if pl.consolidate_item is not None:
                        it = pl.consolidate_item
                        payload["consolidate"] = {"kind": it.kind, "title": it.title, "repo_short": it.repo_short,
                                                  "url": it.url, "waiting_since": (now - dt.timedelta(seconds=it.waiting_s)).isoformat(),
                                                  "expires_at": it.expires_at.isoformat() if it.expires_at else None,
                                                  "pr_number": it.pr_number, "job_url": it.job_url}
                else:
                    payload["github"] = {"repo": ctx.repo, "issue_number": ctx.issue_number, "body": pl.msg.markdown,
                                         "job_id": pl.job_id}
                rows.append({"id": derived("ntf", dedupe), "dedupe": dedupe, "t": tenant, "ek": pl.event_key,
                             "j": pl.job_id, "g": pl.gate_id, "r": pl.recipient, "ch": send_ch, "st": state,
                             "sent": now if state == "sent" else None, "p": json.dumps(payload, default=str), "due": due})
        if not rows:
            return 0
        inserted: set[str] = set()
        async with self.db.tx(role=NOTIFY_ROLE, tenant=tenant) as conn:
            for r in rows:
                got = (await conn.execute(text(
                    "INSERT INTO notify.deliveries (delivery_id, dedupe_key, tenant_id, event_key, job_id, gate_id, "
                    "recipient, channel, state, sent_at, payload, next_attempt_at) VALUES (:id, :dedupe, :t, :ek, :j, :g, "
                    ":r, :ch, :st, :sent, CAST(:p AS jsonb), :due) ON CONFLICT (dedupe_key) DO NOTHING RETURNING dedupe_key"),
                    r)).first()
                if got is not None:
                    inserted.add(r["dedupe"])
            for fi in feed:
                if fi["dedupe"] in inserted:
                    await conn.execute(text("INSERT INTO notify.feed (recipient, tenant_id, event_key, summary, href) "
                                            "VALUES (:r, :t, :ek, :s, :h)"),
                                       {"r": fi["recipient"], "t": tenant, "ek": fi["event_key"], "s": fi["summary"],
                                        "h": fi["href"]})
        return len(inserted)
