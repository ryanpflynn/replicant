"""Delivery consumer (github-app.md §3.3-3.5, §7) for the Gitea adapter.

Claims `github_app.inbound_deliveries` rows with SKIP LOCKED, normalizes them (replicant-scm), and routes:
* `issues.labeled` / `issues.opened` carrying the repo's trigger label (default `replicant`) -> JobRequest ->
  control-plane-api `POST /v1/jobs` -> acknowledgement comment as `replicant-bot` (edited in place on duplicates);
* `pull_request.closed` on a PR owned by a job -> Signal `pr.event` (`pull_request_merged` | `pull_request_closed`);
* review submitted / PR comment on a PR owned by a job, from a linked human with write access -> comment bodies to S3
  (`events/pr/<delivery>.json`) + Signal `pr.event` (`review_submitted` | `issue_comment_created` | `replicant_command`)
  (D-15, pr-replies.md); bots are ignored, unlinked humans get one link-your-account reply;
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

from github_integration.clients import S3, Broker, ControlPlane, Temporal
from github_integration.config import Settings
from github_integration.inventory import Scanner, push_touches_watched
from replicant_ids import dev_principal, ulid
from replicant_scm import canonical_host
from replicant_scm.events import NormalizedEvent
from replicant_scm.gitea import GiteaClient, normalize
from replicant_telemetry.common import iso

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 50
JOB_BRANCH_RE = re.compile(r"^replicant/(job_[0-9a-hjkmnp-tv-z]{26})$")
WORKER_ID = f"{socket.gethostname()}:{ulid('wrk')[-8:]}"


class Retry(Exception):
    """Transient failure: back off and retry the delivery."""


def backoff_s(attempts: int) -> float:
    return min(600.0, float(2 ** min(attempts, 10)))


class Consumer:
    def __init__(self, s: Settings, engine: AsyncEngine, cp: ControlPlane, broker: Broker, s3: S3,
                 temporal: Temporal, gitea_transport: Any = None) -> None:
        self.s, self.engine, self.cp, self.broker, self.s3, self.temporal = s, engine, cp, broker, s3, temporal
        self._gitea_transport = gitea_transport
        self._bots = frozenset(s.bot_logins)
        self.scanner = Scanner(lambda repo: self._gitea(repo, "inventory.scan", permissions={"contents": "read"}),
                               lambda repo_id, profile: self.cp.put_profile(repo_id, profile))

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
        if ev.kind == "push":
            return await self._push(ev, payload)
        if ev.sender is None or ev.sender.is_bot:
            if self._is_pr_feedback(ev):
                log.info(json.dumps({"event": "pr_feedback.ignored", "reason": "bot", "delivery_id": ev.delivery_id,
                                     "login": ev.sender.login if ev.sender else None}))
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
        if self._is_pr_feedback(ev):
            return await self._pr_feedback(ev, repo, r.received_at)
        return "ignored_event"

    async def _push(self, ev: NormalizedEvent, payload: dict[str, Any]) -> str:
        """Inventory: a push to the default branch that touches a watched path (or of a repo this process has not
        profiled yet) queues a rescan. Pushes by anyone count: the default branch is what the profile describes."""
        repo = await self._repo(ev)
        if repo is None:
            return "ignored_not_installed"
        if repo["archived"] or repo["removed_at"] is not None:
            return "ignored_archived"
        branch = repo["default_branch"] or (ev.repo.default_branch if ev.repo else None) or "main"
        if (ev.extra or {}).get("ref") != f"refs/heads/{branch}":
            return "ignored_push_branch"
        if not (push_touches_watched(payload) or not self.scanner.known(repo["repo_id"])):
            return "ignored_push_unwatched"
        self.scanner.request(repo, "push")
        return "inventory_scan_queued"

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
                branch_changed = bool(ev.repo.default_branch and row.default_branch != ev.repo.default_branch)
                if row.full_name != ev.repo.full_name or branch_changed:
                    await conn.execute(text("UPDATE github_app.repos SET full_name = :f, default_branch = coalesce(:b, "
                                            "default_branch) WHERE provider = 'gitea' AND host = :h AND repo_id = :id"),
                                       {"f": ev.repo.full_name, "b": ev.repo.default_branch, "h": ev.repo.host,
                                        "id": ev.repo.repo_id})
                out = dict(row._mapping)
                if branch_changed:
                    out.update(full_name=ev.repo.full_name, default_branch=ev.repo.default_branch)
                    self.scanner.request(out, "default_branch_changed")
                return out
            inst = (await conn.execute(text(
                "SELECT installation_id, tenant_id FROM github_app.installations WHERE provider = 'gitea' AND host = :h "
                "AND lower(account_login) = lower(:o) AND status = 'active'"), {"h": ev.repo.host, "o": ev.repo.owner})
            ).first()
            if inst is None or inst.tenant_id is None:
                return None
            await register_repo(conn, ev.repo.host, ev.repo.repo_id, inst.installation_id, ev.repo.full_name,
                                inst.tenant_id, ev.repo.default_branch, "svc_github-integration")
        self.scanner.request({"repo_id": ev.repo.repo_id, "installation_id": inst.installation_id,
                              "tenant_id": inst.tenant_id, "full_name": ev.repo.full_name,
                              "default_branch": ev.repo.default_branch}, "registered")
        return await self._repo(ev)

    async def _issue_trigger(self, ev: NormalizedEvent, repo: dict[str, Any], label: str, received_at: Any) -> str:
        assert ev.repo and ev.issue and ev.sender
        principal_id = await self.linked_principal(ev.sender.id)
        if principal_id is None:
            await self._unlinked_reply(ev, repo)
            return "unlinked"
        tenant = repo["tenant_id"]
        jr = build_job_request(ev, tenant=tenant, installation_id=repo["installation_id"], label=label,
                               default_branch=repo["default_branch"] or ev.repo.default_branch or "main",
                               received_at=received_at, principal_id=principal_id, settings=repo["settings"] or {})
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

    async def linked_principal(self, gitea_user_id: int) -> str | None:
        """CONFIG: who may trigger comes from identity links (kind gitea), not REPLICANT_DEV_LINKED_LOGINS; whether the
        linked principal may start a job on the repo is control-plane-api's job:create check (project bindings)."""
        async with self.engine.begin() as conn:
            return (await conn.execute(text(
                "SELECT principal_id FROM identity.links WHERE kind = 'gitea' AND external_id = :e AND status = 'active' "
                "ORDER BY created_at DESC LIMIT 1"), {"e": str(gitea_user_id)})).scalar()

    async def _gitea(self, repo: dict[str, Any], purpose: str, job_id: str | None = None,
                     permissions: dict[str, str] | None = None) -> GiteaClient:
        tok = await self.broker.token(repo["installation_id"], repo["repo_id"], purpose, job_id, permissions)
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

    async def _job_for_pr(self, repo: dict[str, Any], number: int, head_ref: str | None) -> str | None:
        """The job that owns PR `number` (jobs_read.job_prs, else the `replicant/<job_id>` head branch)."""
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('replicant.tenant_id', :t, true)"), {"t": repo["tenant_id"]})
            row = (await conn.execute(text("SELECT job_id FROM jobs_read.job_prs WHERE repo_id = :r AND pr_number = :n"),
                                      {"r": repo["repo_id"], "n": number})).first()
        if row is not None:
            return row.job_id
        m = JOB_BRANCH_RE.match(head_ref or "")
        return m.group(1) if m else None

    async def _pr_closed(self, ev: NormalizedEvent, repo: dict[str, Any], received_at: Any) -> str:
        assert ev.repo and ev.pull_request and ev.sender
        tenant = repo["tenant_id"]
        job_id = await self._job_for_pr(repo, ev.pull_request.number, ev.pull_request.head_ref)
        if job_id is None:
            return "ignored_not_job_pr"
        event = "pull_request_merged" if ev.pull_request.merged else "pull_request_closed"
        key = f"tenants/{tenant}/jobs/{job_id}/events/pr/{ev.delivery_id}.json"
        bucket = self.s.bucket("artifacts")
        payload = {"schema": "replicant.pr-event-payload/v1", "provider": "gitea", "delivery_id": ev.delivery_id,
                   "event": event, "repo": ev.repo.ref, "pr_number": ev.pull_request.number,
                   "head_ref": ev.pull_request.head_ref, "head_sha": ev.pull_request.head_sha,
                   "actor": {"id": ev.sender.id, "login": ev.sender.login}}
        if ev.pull_request.merged:  # MEM-PR-D20: who pushed the PR's human commits, linked and with what access
            payload["commit_authors"] = await self._commit_authors(repo, ev.pull_request.number)
        if self.s.s3_endpoint:
            try:
                await self.s3.put(bucket, key, json.dumps(payload, sort_keys=True).encode())
            except Exception as e:  # noqa: BLE001
                raise Retry(f"pr_event_payload write failed: {type(e).__name__}") from None
        linked_pid = await self.linked_principal(ev.sender.id)
        actor: dict[str, Any] = {"github_user_id": max(1, ev.sender.id), "login": ev.sender.login[:64],
                                 "is_bot": ev.sender.is_bot, "linked": linked_pid is not None}
        if linked_pid:
            actor["principal_id"] = linked_pid
        sig: dict[str, Any] = {"schema": "replicant.msg.pr-event/v1", "delivery_id": ev.delivery_id[:64], "event": event,
                               "job_id": job_id, "repo": ev.repo.ref, "pr_number": ev.pull_request.number,
                               "actor": actor, "payload_uri": f"s3://{bucket}/{key}", "received_at": _received_iso(received_at)}
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


    async def _commit_authors(self, repo: dict[str, Any], number: int) -> list[dict[str, Any]]:
        """Distinct non-bot commit authors of a merged PR (<= 10): {id, login, linked, principal_id?,
        repo_permission?}. The worker turns their commits into human_edit memory feedback only for linked writers
        (MEM-PR-D20). Best effort: [] when Gitea is unavailable."""
        owner, _, name = str(repo["full_name"]).partition("/")
        try:
            tok = await self.broker.token(repo["installation_id"], repo["repo_id"], "memory.commit_authors",
                                          permissions={"metadata": "read"})
            async with GiteaClient(self.s.gitea_base_url, token=tok, transport=self._gitea_transport) as g:
                r = await g.request("GET", f"/repos/{owner}/{name}/pulls/{int(number)}/commits", params={"limit": 50})
                commits = r.json() if r.status_code == 200 else []
        except Exception as e:  # noqa: BLE001
            log.warning(json.dumps({"event": "pr_merged.commit_authors_unavailable", "error": type(e).__name__}))
            return []
        seen: dict[int, dict[str, Any]] = {}
        for c in commits if isinstance(commits, list) else []:
            au = c.get("author") or {}
            msg = (c.get("commit") or {}).get("message") or ""
            if not au.get("id") or au.get("login") == "replicant-bot" or "Replicant-Activity:" in msg:
                continue
            if int(au["id"]) not in seen and len(seen) < 10:
                seen[int(au["id"])] = {"id": int(au["id"]), "login": str(au.get("login") or "")[:64]}
        out = []
        for uid, a in seen.items():
            pid = await self.linked_principal(uid)
            a["linked"] = pid is not None
            if pid:
                a["principal_id"] = pid
                try:
                    a["repo_permission"] = await self.repo_permission(repo, a["login"])
                except Exception:  # noqa: BLE001
                    a["repo_permission"] = "read"
            out.append(a)
        return out

    # ------------------------------------------------------------------------------- PR review feedback (D-15)
    @staticmethod
    def _is_pr_feedback(ev: NormalizedEvent) -> bool:
        if ev.kind == "pull_request_review.submitted" and ev.pull_request is not None:
            return ev.review_state in ("commented", "changes_requested", "approved")
        return ev.kind in ("issue_comment.created", "pull_request_review_comment.created") and \
            ev.issue is not None and ev.issue.is_pull

    async def repo_permission(self, repo: dict[str, Any], login: str) -> str:
        """C-MSG actor.repo_permission of `login` on the repo: `admin` for an Owners-type team, `write` for a team with
        code write or a direct collaborator (Gitea's collaborator permission read needs repo admin, so a direct
        collaborator counts as write: MEM-PR-D2), else `read`. Live, never stored."""
        owner, _, name = str(repo["full_name"]).partition("/")
        tok = await self.broker.token(repo["installation_id"], repo["repo_id"], "identity.repo_permission",
                                      permissions={"metadata": "read"})
        async with GiteaClient(self.s.gitea_base_url, token=tok, transport=self._gitea_transport) as g:
            best = "read"
            for t in await g.repo_teams(owner, name):
                level = (t.get("units_map") or {}).get("repo.code") or t.get("permission") or "none"
                lvl = "admin" if level in ("owner", "admin") else "write" if level == "write" else None
                if lvl and (best == "read" or lvl == "admin") and await g.is_team_member(int(t["id"]), login):
                    best = lvl
                    if best == "admin":
                        return best
            if best == "read" and await g.is_collaborator(owner, name, login):
                best = "write"
            return best

    def _ignored(self, ev: NormalizedEvent, reason: str, **kw: Any) -> str:
        log.info(json.dumps({"event": "pr_feedback.ignored", "reason": reason, "delivery_id": ev.delivery_id,
                             "login": ev.sender.login if ev.sender else None, **kw}))
        return f"ignored_{reason}"

    async def _pr_feedback(self, ev: NormalizedEvent, repo: dict[str, Any], received_at: Any) -> str:
        assert ev.repo and ev.sender
        is_review = ev.kind == "pull_request_review.submitted"
        number = ev.pull_request.number if is_review and ev.pull_request else (ev.issue.number if ev.issue else 0)
        head_ref = ev.pull_request.head_ref if ev.pull_request else None
        job_id = await self._job_for_pr(repo, number, head_ref)
        if job_id is None:
            return self._ignored(ev, "not_job_pr", pr_number=number)
        principal_id = await self.linked_principal(ev.sender.id)
        if principal_id is None:
            await self._pr_unlinked_reply(ev, repo, number, job_id)
            return self._ignored(ev, "unlinked", pr_number=number, job_id=job_id)
        perm = await self.repo_permission(repo, ev.sender.login)
        if perm not in ("admin", "maintain", "write"):
            return self._ignored(ev, "no_write", pr_number=number, job_id=job_id, permission=perm)
        comments = await self._feedback_comments(ev, repo, number, is_review)
        if not comments:
            return self._ignored(ev, "empty", pr_number=number, job_id=job_id)
        for c in comments:
            c.update(parse_commands(c["body"]))
        command_only = all(c["command_only"] for c in comments)
        event = "replicant_command" if command_only else ("review_submitted" if is_review else "issue_comment_created")
        tenant = repo["tenant_id"]
        key = f"tenants/{tenant}/jobs/{job_id}/events/pr/{ev.delivery_id}.json"
        bucket = self.s.bucket("artifacts")
        actor_doc = {"id": ev.sender.id, "login": ev.sender.login, "principal_id": principal_id, "repo_permission": perm}
        payload = {"schema": "replicant.pr-event-payload/v1", "provider": "gitea", "delivery_id": ev.delivery_id,
                   "event": event, "repo": ev.repo.ref, "pr_number": number, "review_state": ev.review_state,
                   "actor": actor_doc, "comments": comments, "received_at": _received_iso(received_at)}
        if self.s.s3_endpoint:
            try:
                await self.s3.put(bucket, key, json.dumps(payload, sort_keys=True).encode())
            except Exception as e:  # noqa: BLE001
                raise Retry(f"pr_event_payload write failed: {type(e).__name__}") from None
        refs = [{k: v for k, v in {"comment_id": int(c["comment_id"]), "kind": c["kind"], "path": c.get("path"),
                                   "line": c.get("line")}.items() if v not in (None, "", 0)}
                for c in comments if int(c.get("comment_id") or 0) >= 1][:100]
        sig: dict[str, Any] = {"schema": "replicant.msg.pr-event/v1", "delivery_id": ev.delivery_id[:64], "event": event,
                               "job_id": job_id, "repo": ev.repo.ref, "pr_number": number,
                               "actor": {"github_user_id": max(1, ev.sender.id), "login": ev.sender.login[:64],
                                         "is_bot": False, "linked": True, "principal_id": principal_id,
                                         "repo_permission": perm},
                               "payload_uri": f"s3://{bucket}/{key}", "received_at": _received_iso(received_at)}
        if refs:
            sig["comment_refs"] = refs
        if is_review and ev.review_state:
            sig["review_state"] = ev.review_state
        try:
            await self.temporal.signal(job_id, "pr.event", sig)
        except Exception as e:  # noqa: BLE001
            from temporalio.service import RPCError, RPCStatusCode
            if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:
                return self._ignored(ev, "closed_job", job_id=job_id)
            raise Retry(f"signal failed: {type(e).__name__}") from None
        log.info(json.dumps({"event": "pr_feedback.signalled", "delivery_id": ev.delivery_id, "job_id": job_id,
                             "pr_number": number, "kind": event, "comments": [c["comment_id"] for c in comments]}))
        return "pr_feedback_signalled"

    async def _feedback_comments(self, ev: NormalizedEvent, repo: dict[str, Any], number: int,
                                 is_review: bool) -> list[dict[str, Any]]:
        """Comment items (bodies capped at 8 KiB). A review: its body plus its line comments, read from the API (the
        webhook carries only `review.content`); the review is the sender's latest on the PR."""
        def item(cid: int, kind: str, body: str, **kw: Any) -> dict[str, Any]:
            return {"comment_id": cid, "kind": kind, "body": body[:8192], **{k: v for k, v in kw.items() if v}}
        assert ev.repo and ev.sender
        if not is_review:
            c = ev.comment
            return [item(c.id, "issue_comment", c.body, url=f"{ev.issue.html_url}#issuecomment-{c.id}"
                         if ev.issue and ev.issue.html_url else None)] if c and c.body.strip() else []
        out: list[dict[str, Any]] = []
        g = await self._gitea(repo, "github.read_review")
        async with g:
            reviews = [r for r in await g.list_pull_reviews(ev.repo.owner, ev.repo.name, number)
                       if (r.get("user") or {}).get("id") == ev.sender.id]
            rv = max(reviews, key=lambda r: int(r.get("id") or 0)) if reviews else None
            review_id = int(rv["id"]) if rv else 0
            body = (ev.comment.body if ev.comment else "") or (rv or {}).get("body") or ""
            if body.strip():
                out.append(item(review_id, "review_body", body, review_id=review_id, url=(rv or {}).get("html_url")))
            if rv and int(rv.get("comments_count") or 0) > 0:
                for c in await g.review_comments(ev.repo.owner, ev.repo.name, number, review_id):
                    if (c.get("body") or "").strip():
                        out.append(item(int(c["id"]), "review_comment", c["body"], review_id=review_id,
                                        path=c.get("path"), line=int(c.get("position") or c.get("original_position")
                                                                     or 0) or None, url=c.get("html_url")))
        return out

    async def _pr_unlinked_reply(self, ev: NormalizedEvent, repo: dict[str, Any], number: int, job_id: str) -> None:
        """One link-your-account reply per (user, repo) per 24 h, on the PR (WS-07 unlinked reply)."""
        assert ev.sender and ev.repo
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
        body = (f"@{ev.sender.login}, the Replicant only acts on review feedback from linked accounts. Link yours at "
                f"{self.s.web_origin.rstrip('/')}/settings/identity, then comment again.")
        g = await self._gitea(repo, "github.post_reply", job_id)
        async with g:
            c = await g.create_issue_comment(ev.repo.owner, ev.repo.name, number, body)
        async with self.engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO github_app.comment_refs (provider, repo_id, number, purpose, job_id, external_id, "
                "delivery_guid) VALUES ('gitea', :r, :n, 'unlinked_reply', :j, :c, :g) ON CONFLICT DO NOTHING"),
                {"r": repo["repo_id"], "n": number, "j": job_id, "c": int(c["id"]), "g": ev.delivery_id})


COMMAND_RE = re.compile(r"^\s*/replicant\s+(no-memory|remember)\b", re.M | re.I)


def parse_commands(body: str) -> dict[str, bool]:
    """`/replicant no-memory` and `/replicant remember` (pr-replies.md §5) on a line of their own; `command_only` when
    nothing but commands is left."""
    found = {m.group(1).lower() for m in COMMAND_RE.finditer(body or "")}
    rest = COMMAND_RE.sub("", body or "")
    rest = re.sub(r"^\s*/replicant\b.*$", "", rest, flags=re.M)
    return {"no_memory": "no-memory" in found, "remember": "remember" in found and "no-memory" not in found,
            "command_only": bool(found) and not rest.strip()}


def _received_iso(v: Any) -> str:
    """`received_at` in UTC with milliseconds; now when it is not a datetime."""
    return iso(v if isinstance(v, dt.datetime) else dt.datetime.now(dt.UTC), utc=True, timespec="milliseconds")


def requested_type(settings: dict[str, Any]) -> str:
    """CONFIG: `allowed_workflow_types` of the repo settings; `auto` lets the router decide, else the first allowed."""
    allowed = list(settings.get("allowed_workflow_types") or ["auto"])
    return "auto" if "auto" in allowed else str(allowed[0])


def build_job_request(ev: NormalizedEvent, *, tenant: str, installation_id: int, label: str, default_branch: str,
                      received_at: Any, principal_id: str | None = None,
                      settings: dict[str, Any] | None = None) -> dict[str, Any]:
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
        "requester": {"kind": "user", "principal_id": principal_id or dev_principal(ev.sender.login),
                      "channel_identity": {"kind": "gitea", "id": str(ev.sender.id), "login": ev.sender.login}},
        "targets": [{"repo": {"provider": "gitea", "host": ev.repo.host, "owner": ev.repo.owner, "name": ev.repo.name,
                              "repo_id": ev.repo.repo_id},
                     "base_ref": default_branch, "issue": {"number": ev.issue.number}}],
        "intent": {"title": (ev.issue.title or f"Issue #{ev.issue.number}")[:256], "text": text_, "format": "markdown",
                   "untrusted": True},
        "workflow_type": requested_type(settings or {}),
        "priority": (settings or {}).get("default_priority") if (settings or {}).get("default_priority") in
        ("low", "normal", "high") else "normal",
        "received_at": _received_iso(received_at),
    }
    if ev.issue.html_url:
        jr["source"]["url"] = ev.issue.html_url[:2048]
    return jr


async def register_repo(conn: Any, host: str, repo_id: str, installation_id: int, full_name: str, tenant_id: str,
                        default_branch: str | None, by: str) -> bool:
    """Registers or updates the repo; True when it was new or its default branch changed (inventory rescan)."""
    prev = (await conn.execute(text(
        "SELECT default_branch FROM github_app.repos WHERE provider = 'gitea' AND host = :h AND repo_id = :id"),
        {"h": host, "id": repo_id})).first()
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
    return prev is None or bool(default_branch and prev.default_branch != default_branch)
