"""Delivery consumer (github-app.md §3.3-3.5, §7) for the Gitea adapter.

Claims `github_app.inbound_deliveries` rows with SKIP LOCKED, normalizes them (replicant-scm), and routes:
* `issues.labeled` / `issues.opened` carrying the repo's trigger label (default `replicant`) -> JobRequest ->
  control-plane-api `POST /v1/jobs` -> acknowledgement comment as `replicant-bot` (edited in place on duplicates);
* `pull_request.closed` on a PR owned by a job -> Signal `pr.event` (`pull_request_merged` | `pull_request_closed`);
* everything else -> `ignored`.
Every effect is keyed by the delivery id (dedupe_key, comment_refs, pr.event delivery_id), so redelivery is safe.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import socket
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from replicant_ids import derived, ulid
from replicant_scm import canonical_host
from replicant_scm.events import NormalizedEvent
from replicant_scm.gitea import GiteaClient, normalize
from github_integration.clients import Broker, ControlPlane, S3, Temporal
from github_integration.config import Settings

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 50
JOB_BRANCH_RE = re.compile(r"^replicant/(job_[0-9a-hjkmnp-tv-z]{26})$")
WORKER_ID = f"{socket.gethostname()}:{ulid('wrk')[-8:]}"


class Retry(Exception):
    """Transient failure: back off and retry the delivery."""


def dev_principal(login: str) -> str:
    """Static M1 identity links (no identity-service): the same rule as control-plane-api (CONTROL-02)."""
    return derived("usr", "replicant-dev-user", login)


def backoff_s(attempts: int) -> float:
    return min(600.0, float(2 ** min(attempts, 10)))


class Consumer:
    def __init__(self, s: Settings, engine: AsyncEngine, cp: ControlPlane, broker: Broker, s3: S3,
                 temporal: Temporal, gitea_transport: Any = None) -> None:
        self.s, self.engine, self.cp, self.broker, self.s3, self.temporal = s, engine, cp, broker, s3, temporal
        self._gitea_transport = gitea_transport
        self._bots = frozenset(s.bot_logins)

    # ----------------------------------------------------------------------------------------- claim loop
    async def claim(self, limit: int = 20) -> list[Any]:
        async with self.engine.begin() as conn:
            return (await conn.execute(text(
                "UPDATE github_app.inbound_deliveries d SET state = 'processing', claimed_by = :w, claimed_at = now(), "
                "attempts = attempts + 1 WHERE d.delivery_guid IN (SELECT delivery_guid FROM github_app.inbound_deliveries "
                "WHERE (state IN ('received', 'retry') AND next_attempt_at <= now()) OR (state = 'processing' AND "
                "claimed_at < now() - interval '5 minutes') ORDER BY received_at LIMIT :n FOR UPDATE SKIP LOCKED) "
                "RETURNING d.*"), {"w": WORKER_ID, "n": limit})).all()

    async def run_once(self) -> int:
        rows = await self.claim()
        for r in rows:
            await self.process_row(r)
        return len(rows)

    async def loop(self, interval_s: float = 1.0) -> None:
        while True:
            try:
                n = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("consumer: %s: %s", type(e).__name__, str(e)[:200])
                n = 0
            if not n:
                await asyncio.sleep(interval_s)

    async def process_row(self, r: Any) -> None:
        try:
            outcome = await self.process(r)
            await self._finish(r.delivery_guid, "done" if not outcome.startswith("ignored") else "ignored", outcome)
        except Retry as e:
            await self._retry(r, str(e))
        except Exception as e:  # noqa: BLE001 - unexpected: retry with backoff, never lose the delivery
            log.exception("delivery %s failed", r.delivery_guid)
            await self._retry(r, f"{type(e).__name__}: {str(e)[:300]}")

    async def _finish(self, guid: str, state: str, outcome: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("UPDATE github_app.inbound_deliveries SET state = :s, outcome = :o, "
                                    "processed_at = now(), last_error = NULL WHERE delivery_guid = :g"),
                               {"s": state, "o": outcome[:100], "g": guid})
        log.info(json.dumps({"event": "delivery.processed", "delivery_id": guid, "state": state, "outcome": outcome}))

    async def _retry(self, r: Any, err: str) -> None:
        dead = r.attempts >= MAX_ATTEMPTS
        async with self.engine.begin() as conn:
            await conn.execute(text(
                "UPDATE github_app.inbound_deliveries SET state = :s, last_error = :e, next_attempt_at = now() + "
                "make_interval(secs => :b) WHERE delivery_guid = :g"),
                {"s": "dead" if dead else "retry", "e": err[:500], "b": backoff_s(r.attempts), "g": r.delivery_guid})
        log.warning("delivery %s %s: %s", r.delivery_guid, "dead" if dead else "retry", err[:200])

    # ----------------------------------------------------------------------------------------- routing
    async def process(self, r: Any) -> str:
        if r.provider != "gitea":
            return "ignored_provider"
        headers = {"X-Gitea-Event": r.event, "X-Gitea-Event-Type": r.target_type or r.event,
                   "X-Gitea-Delivery": r.delivery_guid}
        payload = json.loads(bytes(r.payload))
        ev = normalize(headers, payload, host=self.s.gitea_base_url, bot_logins=self._bots)
        if ev.repo is None:
            return "ignored_no_repo"
        if ev.sender is None or ev.sender.is_bot:
            return "ignored_bot"
        repo = await self._repo(ev)
        if repo is None:
            return "ignored_not_installed"
        if repo["archived"] or repo["removed_at"] is not None:
            return "ignored_archived"
        settings = repo["settings"] or {}
        label = settings.get("trigger_label", "replicant")
        if ev.kind in ("issues.labeled", "issues.opened") and ev.issue and not ev.issue.is_pull:
            if label not in ev.issue.labels or "label" not in settings.get("trigger_modes", ["label", "command"]):
                return "ignored_no_trigger"
            if settings.get("enabled") is False:
                return "ignored_repo_disabled"
            return await self._issue_trigger(ev, repo, label, r.received_at)
        if ev.kind == "pull_request.closed" and ev.pull_request:
            return await self._pr_closed(ev, repo, r.received_at)
        return "ignored_event"

    async def _repo(self, ev: NormalizedEvent) -> dict[str, Any] | None:
        """github_app.repos row (lazily registered for repos of an installed Gitea org)."""
        assert ev.repo is not None
        async with self.engine.begin() as conn:
            row = (await conn.execute(text(
                "SELECT r.repo_id, r.installation_id, r.tenant_id, r.full_name, r.default_branch, r.archived, "
                "r.removed_at, s.settings FROM github_app.repos r LEFT JOIN github_app.repo_settings s ON "
                "s.provider = r.provider AND s.host = r.host AND s.repo_id = r.repo_id AND s.is_current WHERE "
                "r.provider = 'gitea' AND r.host = :h AND r.repo_id = :id"), {"h": ev.repo.host, "id": ev.repo.repo_id})
            ).first()
            if row is not None:
                if row.full_name != ev.repo.full_name or (ev.repo.default_branch and
                                                          row.default_branch != ev.repo.default_branch):
                    await conn.execute(text("UPDATE github_app.repos SET full_name = :f, default_branch = coalesce(:b, "
                                            "default_branch) WHERE provider = 'gitea' AND host = :h AND repo_id = :id"),
                                       {"f": ev.repo.full_name, "b": ev.repo.default_branch, "h": ev.repo.host,
                                        "id": ev.repo.repo_id})
                return dict(row._mapping)
            inst = (await conn.execute(text(
                "SELECT installation_id, tenant_id FROM github_app.installations WHERE provider = 'gitea' AND host = :h "
                "AND lower(account_login) = lower(:o) AND status = 'active'"), {"h": ev.repo.host, "o": ev.repo.owner})
            ).first()
            if inst is None or inst.tenant_id is None:
                return None
            await register_repo(conn, ev.repo.host, ev.repo.repo_id, inst.installation_id, ev.repo.full_name,
                                inst.tenant_id, ev.repo.default_branch, "svc_github-integration")
        return await self._repo(ev)

    async def _issue_trigger(self, ev: NormalizedEvent, repo: dict[str, Any], label: str, received_at: Any) -> str:
        assert ev.repo and ev.issue and ev.sender
        if ev.sender.login not in self.s.dev_linked_logins:
            await self._unlinked_reply(ev, repo)
            return "unlinked"
        tenant = repo["tenant_id"]
        jr = build_job_request(ev, tenant=tenant, installation_id=repo["installation_id"], label=label,
                               default_branch=repo["default_branch"] or ev.repo.default_branch or "main",
                               received_at=received_at)
        res = await self.cp.submit_job(jr, idempotency_key=ev.delivery_id)
        if res.status == 503 or res.status >= 500 or res.status == 429:
            raise Retry(f"intake {res.status} {res.body.get('code')}")
        if res.status in (200, 201):
            job_id = res.body["job_id"]
            await self._ack(ev, repo, job_id, duplicate=(res.status == 200))
            return "job_created" if res.status == 201 else "duplicate"
        code = res.body.get("code", "refused")
        await self._comment(ev, repo, "refusal", None,
                            f"@{ev.sender.login}, the Replicant did not start a job for this issue: "
                            f"`{code}` ({res.status}).")
        return f"refused_{code}"[:60]

    async def _gitea(self, repo: dict[str, Any], purpose: str, job_id: str | None = None) -> GiteaClient:
        tok = await self.broker.token(repo["installation_id"], repo["repo_id"], purpose, job_id)
        return GiteaClient(self.s.gitea_base_url, token=tok, transport=self._gitea_transport)

    def _ack_body(self, ev: NormalizedEvent, job_id: str, duplicate: bool) -> str:
        link = f"{self.s.web_origin.rstrip('/')}/jobs/{job_id}"
        assert ev.sender
        head = "Already running as" if duplicate else "Replicant job started"
        return (f"**{head}** · [`{job_id[:14]}`]({link}) · requested by @{ev.sender.login}\n\n"
                f"| | |\n|---|---|\n| Status | Queued |\n| Job | `{job_id}` |\n\n"
                f"<sub>Reply `/replicant status` for details or `/replicant cancel` to stop.</sub>")

    async def _ack(self, ev: NormalizedEvent, repo: dict[str, Any], job_id: str, duplicate: bool) -> None:
        log.info(json.dumps({"event": "job.ack", "job_id": job_id, "tenant_id": repo.get("tenant_id"),
                             "duplicate": duplicate, "issue": ev.issue.number if ev.issue else None}))
        if (repo["settings"] or {}).get("ack_comment") is False:
            return
        await self._comment(ev, repo, "ack", job_id, self._ack_body(ev, job_id, duplicate), edit_existing=True)

    async def _comment(self, ev: NormalizedEvent, repo: dict[str, Any], purpose: str, job_id: str | None, body: str,
                       edit_existing: bool = False) -> None:
        """Posts once per (repo, issue, purpose[, delivery]) (github_app.comment_refs uniqueness)."""
        assert ev.repo and ev.issue
        guid = None if purpose == "ack" else ev.delivery_id
        ref_purpose = purpose if purpose in ("ack", "gate") else "unlinked_reply"   # refusals are one-off replies
        async with self.engine.begin() as conn:
            existing = (await conn.execute(text(
                "SELECT external_id, job_id FROM github_app.comment_refs WHERE repo_id = :r AND number = :n AND "
                "purpose = :p AND coalesce(delivery_guid, '') = coalesce(:g, '') AND coalesce(gate_id, '') = ''"),
                {"r": repo["repo_id"], "n": ev.issue.number, "p": ref_purpose, "g": guid})).first()
        if existing is not None and not edit_existing:
            return
        if existing is not None and existing.job_id == job_id and purpose == "ack":
            return  # already acknowledged this job (redelivery)
        g = await self._gitea(repo, "github.post_reply", job_id)
        async with g:
            owner, name = ev.repo.owner, ev.repo.name
            if existing is not None:
                await g.edit_issue_comment(owner, name, int(existing.external_id), body)
                async with self.engine.begin() as conn:
                    await conn.execute(text("UPDATE github_app.comment_refs SET job_id = :j, updated_at = now() WHERE "
                                            "repo_id = :r AND number = :n AND purpose = :p"),
                                       {"j": job_id, "r": repo["repo_id"], "n": ev.issue.number, "p": ref_purpose})
                return
            c = await g.create_issue_comment(owner, name, ev.issue.number, body)
        async with self.engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO github_app.comment_refs (provider, repo_id, number, purpose, job_id, external_id, "
                "delivery_guid) VALUES ('gitea', :r, :n, :p, :j, :c, :g) ON CONFLICT DO NOTHING"),
                {"r": repo["repo_id"], "n": ev.issue.number, "p": ref_purpose, "j": job_id, "c": int(c["id"]), "g": guid})

    async def post_notification(self, delivery_id: str, repo_ref: dict[str, Any], number: int, body: str,
                                job_id: str | None) -> dict[str, Any]:
        """Issue comment for a control-plane-api notify delivery (notifications.md `originating` channel). Idempotent
        per delivery: github_app.comment_refs (purpose `notification`, delivery_guid = delivery_id)."""
        host = canonical_host(str(repo_ref.get("host", "")))
        async with self.engine.begin() as conn:
            existing = (await conn.execute(text(
                "SELECT external_id FROM github_app.comment_refs WHERE purpose = 'notification' AND delivery_guid = :d"),
                {"d": delivery_id})).first()
            repo = (await conn.execute(text(
                "SELECT repo_id, installation_id, full_name FROM github_app.repos WHERE provider = 'gitea' AND host = :h "
                "AND repo_id = :id"), {"h": host, "id": str(repo_ref.get("repo_id", ""))})).first()
        if existing is not None:
            return {"state": "sent", "external_ref": str(existing.external_id), "duplicate": True}
        if repo is None:
            return {"state": "suppressed", "reason": "repo_not_registered"}
        owner, _, name = repo.full_name.partition("/")
        g = await self._gitea(dict(repo._mapping), "github.post_reply", job_id)
        async with g:
            c = await g.create_issue_comment(owner, name, number, body)
        async with self.engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO github_app.comment_refs (provider, repo_id, number, purpose, job_id, external_id, "
                "delivery_guid) VALUES ('gitea', :r, :n, 'notification', :j, :c, :d) ON CONFLICT DO NOTHING"),
                {"r": repo.repo_id, "n": number, "j": job_id, "c": int(c["id"]), "d": delivery_id})
        return {"state": "sent", "external_ref": str(c["id"])}

    async def _unlinked_reply(self, ev: NormalizedEvent, repo: dict[str, Any]) -> None:
        assert ev.sender and ev.issue
        async with self.engine.begin() as conn:
            seen = (await conn.execute(text(
                "SELECT replied_at FROM github_app.unlinked_replies WHERE provider = 'gitea' AND user_id = :u AND "
                "repo_id = :r AND replied_at > now() - interval '24 hours'"),
                {"u": ev.sender.id, "r": repo["repo_id"]})).first()
            if seen is not None:
                return
            await conn.execute(text(
                "INSERT INTO github_app.unlinked_replies (provider, user_id, repo_id, replied_at) VALUES ('gitea', :u, :r, "
                "now()) ON CONFLICT (provider, user_id, repo_id) DO UPDATE SET replied_at = now()"),
                {"u": ev.sender.id, "r": repo["repo_id"]})
        await self._comment(ev, repo, "unlinked_reply", None,
                            f"@{ev.sender.login}, link your account to the Replicant first: "
                            f"{self.s.web_origin.rstrip('/')}/settings/identity. Then comment `/replicant retry`.")

    async def _pr_closed(self, ev: NormalizedEvent, repo: dict[str, Any], received_at: Any) -> str:
        assert ev.repo and ev.pull_request and ev.sender
        tenant = repo["tenant_id"]
        job_id = None
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('replicant.tenant_id', :t, true)"), {"t": tenant})
            row = (await conn.execute(text("SELECT job_id FROM jobs_read.job_prs WHERE repo_id = :r AND pr_number = :n"),
                                      {"r": repo["repo_id"], "n": ev.pull_request.number})).first()
            if row is not None:
                job_id = row.job_id
        if job_id is None:
            m = JOB_BRANCH_RE.match(ev.pull_request.head_ref or "")
            job_id = m.group(1) if m else None
        if job_id is None:
            return "ignored_not_job_pr"
        event = "pull_request_merged" if ev.pull_request.merged else "pull_request_closed"
        key = f"tenants/{tenant}/jobs/{job_id}/events/pr/{ev.delivery_id}.json"
        bucket = self.s.bucket("artifacts")
        payload = {"schema": "replicant.pr-event-payload/v1", "provider": "gitea", "delivery_id": ev.delivery_id,
                   "event": event, "repo": ev.repo.ref, "pr_number": ev.pull_request.number,
                   "head_ref": ev.pull_request.head_ref, "head_sha": ev.pull_request.head_sha,
                   "actor": {"id": ev.sender.id, "login": ev.sender.login}}
        if self.s.s3_endpoint:
            try:
                await self.s3.put(bucket, key, json.dumps(payload, sort_keys=True).encode())
            except Exception as e:  # noqa: BLE001
                raise Retry(f"pr_event_payload write failed: {type(e).__name__}") from None
        linked = ev.sender.login in self.s.dev_linked_logins
        actor: dict[str, Any] = {"github_user_id": max(1, ev.sender.id), "login": ev.sender.login[:64],
                                 "is_bot": ev.sender.is_bot, "linked": linked}
        if linked:
            actor["principal_id"] = dev_principal(ev.sender.login)
        sig: dict[str, Any] = {"schema": "replicant.msg.pr-event/v1", "delivery_id": ev.delivery_id[:64], "event": event,
                               "job_id": job_id, "repo": ev.repo.ref, "pr_number": ev.pull_request.number,
                               "actor": actor, "payload_uri": f"s3://{bucket}/{key}", "received_at": _iso(received_at)}
        if ev.pull_request.head_sha and re.match(r"^[0-9a-f]{40}([0-9a-f]{24})?$", ev.pull_request.head_sha):
            sig["head_sha"] = ev.pull_request.head_sha
        try:
            await self.temporal.signal(job_id, "pr.event", sig)
        except Exception as e:  # noqa: BLE001
            from temporalio.service import RPCError, RPCStatusCode
            if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:
                return "ignored_closed_job"
            raise Retry(f"signal failed: {type(e).__name__}") from None
        return "pr_event_signalled"


def _iso(v: Any) -> str:
    if isinstance(v, dt.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=dt.UTC)
        return v.astimezone(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_job_request(ev: NormalizedEvent, *, tenant: str, installation_id: int, label: str, default_branch: str,
                      received_at: Any) -> dict[str, Any]:
    """C-JOBREQ 2.0.0 document for a Gitea issue trigger (triggers.md §1, Gitea row; INTERFACES-LOG CONTROL-04)."""
    assert ev.repo and ev.issue and ev.sender
    text_ = ev.issue.body or ""
    if len(text_) > 65536:
        text_ = text_[:65536]
    jr: dict[str, Any] = {
        "schema_version": "replicant.job-request/v1",
        "request_id": ulid("jrq"),
        "dedupe_key": f"gitea:issue-label:{ev.repo.repo_id}:{ev.issue.number}:{label}",
        "tenant_id": tenant,
        "source": {"channel": "github", "event_type": ev.kind, "event_id": ev.delivery_id[:200],
                   "github": {"installation_id": int(installation_id), "app_registration": "dev"}},
        "requester": {"kind": "user", "principal_id": dev_principal(ev.sender.login),
                      "channel_identity": {"kind": "gitea", "id": str(ev.sender.id), "login": ev.sender.login}},
        "targets": [{"repo": {"provider": "gitea", "host": ev.repo.host, "owner": ev.repo.owner, "name": ev.repo.name,
                              "repo_id": ev.repo.repo_id},
                     "base_ref": default_branch, "issue": {"number": ev.issue.number}}],
        "intent": {"title": (ev.issue.title or f"Issue #{ev.issue.number}")[:256], "text": text_, "format": "markdown",
                   "untrusted": True},
        "workflow_type": "auto",
        "priority": "normal",
        "received_at": _iso(received_at),
    }
    if ev.issue.html_url:
        jr["source"]["url"] = ev.issue.html_url[:2048]
    return jr


async def register_repo(conn: Any, host: str, repo_id: str, installation_id: int, full_name: str, tenant_id: str,
                        default_branch: str | None, by: str) -> None:
    await conn.execute(text(
        "INSERT INTO github_app.repos (provider, host, repo_id, installation_id, full_name, tenant_id, default_branch) "
        "VALUES ('gitea', :h, :id, :i, :f, :t, :b) ON CONFLICT (provider, host, repo_id) DO UPDATE SET "
        "installation_id = EXCLUDED.installation_id, full_name = EXCLUDED.full_name, tenant_id = EXCLUDED.tenant_id, "
        "default_branch = coalesce(EXCLUDED.default_branch, github_app.repos.default_branch)"),
        {"h": host, "id": repo_id, "i": installation_id, "f": full_name, "t": tenant_id, "b": default_branch})
    settings = {"trigger_modes": ["label", "command"], "trigger_label": "replicant", "enabled": True, "ack_comment": True,
                "status_label_prefix": "replicant:", "command_prefix": "/replicant",
                "allowed_workflow_types": ["auto", "bugfix", "feature", "docs", "api_change"], "default_priority": "normal"}
    await conn.execute(text(
        "INSERT INTO github_app.repo_settings (provider, host, repo_id, version, settings, is_current, updated_by) "
        "SELECT 'gitea', :h, :id, 1, CAST(:s AS jsonb), true, :by WHERE NOT EXISTS (SELECT 1 FROM github_app.repo_settings "
        "WHERE provider = 'gitea' AND host = :h AND repo_id = :id)"),
        {"h": host, "id": repo_id, "s": json.dumps(settings), "by": by})
