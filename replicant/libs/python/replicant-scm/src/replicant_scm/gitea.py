"""Gitea adapter (local profile; ADR-11-002, parity P-10).

* `normalize()` turns a Gitea webhook (headers + parsed JSON) into a `NormalizedEvent`.
* `GiteaClient` is a thin async REST client (API v1) for the calls Replicant makes as `replicant-bot`.

Gitea facts used (ADR-11-002, V11-06/07): headers `X-Gitea-Event`, `X-Gitea-Event-Type`, `X-Gitea-Delivery`,
`X-Gitea-Signature`; label changes arrive as event `issues` with action `label_updated` (event type `issue_label`);
review approvals as `pull_request_approved` / `pull_request_rejected` / `pull_request_comment`; tokens have no expiry
and scopes are not per repository.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from replicant_scm.events import NormalizedEvent, ScmActor, ScmComment, ScmIssue, ScmPullRequest, ScmRepo
from replicant_scm.hosts import canonical_host

PROVIDER = "gitea"

_ISSUE_ACTIONS = {
    "opened": "opened", "edited": "edited", "closed": "closed", "reopened": "reopened",
    "assigned": "assigned", "unassigned": "unassigned", "label_updated": "labeled", "label_cleared": "unlabeled",
}
_PR_ACTIONS = {
    "opened": "opened", "edited": "edited", "closed": "closed", "reopened": "reopened",
    "synchronized": "synchronize", "label_updated": "labeled", "review_requested": "review_requested",
}
_REVIEW_EVENTS = {
    "pull_request_approved": "approved", "pull_request_review_approved": "approved",
    "pull_request_rejected": "changes_requested", "pull_request_review_rejected": "changes_requested",
    "pull_request_comment": "commented", "pull_request_review_comment": "commented",
}


def _h(headers: Mapping[str, str], name: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def _actor(u: Mapping[str, Any] | None, bot_logins: frozenset[str]) -> ScmActor | None:
    if not u:
        return None
    login = u.get("login") or u.get("username") or ""
    is_bot = login in bot_logins or str(u.get("type", "")).lower() == "bot" or login.endswith("[bot]")
    return ScmActor(id=int(u.get("id") or 0), login=login, is_bot=is_bot)


def _repo(r: Mapping[str, Any] | None, host: str) -> ScmRepo | None:
    if not r:
        return None
    owner = (r.get("owner") or {}).get("login") or (r.get("owner") or {}).get("username") or r["full_name"].split("/")[0]
    return ScmRepo(provider=PROVIDER, host=host, owner=owner, name=r["name"], repo_id=str(r["id"]),
                   default_branch=r.get("default_branch"), html_url=r.get("html_url"), archived=bool(r.get("archived")))


def _issue(i: Mapping[str, Any] | None) -> ScmIssue | None:
    if not i:
        return None
    return ScmIssue(number=int(i["number"]), title=i.get("title") or "", body=i.get("body") or "",
                    labels=tuple(sorted({(lbl or {}).get("name", "") for lbl in (i.get("labels") or []) if lbl})),
                    html_url=i.get("html_url"), is_pull=bool(i.get("pull_request")), state=i.get("state") or "open")


def _pr(p: Mapping[str, Any] | None) -> ScmPullRequest | None:
    if not p:
        return None
    head, base = p.get("head") or {}, p.get("base") or {}
    return ScmPullRequest(number=int(p["number"]), head_ref=head.get("ref"), head_sha=head.get("sha"),
                          base_ref=base.get("ref"), merged=bool(p.get("merged")), state=p.get("state") or "open",
                          html_url=p.get("html_url"))


def normalize(headers: Mapping[str, str], payload: Mapping[str, Any], *, host: str,
              bot_logins: frozenset[str] = frozenset({"replicant-bot"})) -> NormalizedEvent:
    event = (_h(headers, "X-Gitea-Event") or _h(headers, "X-GitHub-Event") or "").strip()
    event_type = (_h(headers, "X-Gitea-Event-Type") or event).strip()
    delivery = (_h(headers, "X-Gitea-Delivery") or _h(headers, "X-GitHub-Delivery") or "").strip()
    action = payload.get("action")
    host = canonical_host(host)
    repo = _repo(payload.get("repository"), host)
    sender = _actor(payload.get("sender"), bot_logins)
    common = dict(provider=PROVIDER, delivery_id=delivery, repo=repo, sender=sender, raw_event=event_type,
                  raw_action=action, org_login=(payload.get("organization") or {}).get("username")
                  or (repo.owner if repo else None))

    if event_type in _REVIEW_EVENTS or (event == "pull_request" and event_type in _REVIEW_EVENTS):
        review = payload.get("review") or {}
        return NormalizedEvent(kind="pull_request_review.submitted", pull_request=_pr(payload.get("pull_request")),
                               review_state=_REVIEW_EVENTS[event_type],
                               comment=ScmComment(id=0, body=review.get("content") or "", user=sender), **common)
    if event == "issues" or event_type in ("issues", "issue_label", "issue_assign", "issue_milestone"):
        kind = f"issues.{_ISSUE_ACTIONS.get(action or '', action or 'unknown')}"
        return NormalizedEvent(kind=kind, issue=_issue(payload.get("issue")), **common)
    if event == "issue_comment" or event_type in ("issue_comment", "pull_request_comment"):
        c = payload.get("comment") or {}
        issue = _issue(payload.get("issue"))
        if issue is not None and payload.get("is_pull"):
            issue = ScmIssue(**{**issue.__dict__, "is_pull": True})
        return NormalizedEvent(kind=f"issue_comment.{action or 'created'}", issue=issue,
                               comment=ScmComment(id=int(c.get("id") or 0), body=c.get("body") or "",
                                                  user=_actor(c.get("user"), bot_logins)), **common)
    if event == "pull_request" or event_type.startswith("pull_request"):
        kind = "pull_request.synchronize" if event_type == "pull_request_sync" else \
            f"pull_request.{_PR_ACTIONS.get(action or '', action or 'unknown')}"
        return NormalizedEvent(kind=kind, pull_request=_pr(payload.get("pull_request")), **common)
    if event == "push":
        return NormalizedEvent(kind="push", extra={"ref": payload.get("ref"), "after": payload.get("after")}, **common)
    return NormalizedEvent(kind=event_type or "unknown", **common)


class GiteaError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"gitea {status}: {message}")
        self.status = status


class GiteaClient:
    """Minimal Gitea API v1 client. `token` is a replicant-bot access token (from github-token-broker) or `basic` is a
    (user, password) pair (the broker's own bootstrap credential). Never logs credentials."""

    def __init__(self, base_url: str, *, token: str | None = None, basic: tuple[str, str] | None = None,
                 timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"token {token}"
        self._c = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/api/v1", headers=headers, timeout=timeout,
                                    auth=httpx.BasicAuth(*basic) if basic else None, transport=transport)

    async def aclose(self) -> None:
        await self._c.aclose()

    async def __aenter__(self) -> "GiteaClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        r = await self._c.request(method, path, **kw)
        if r.status_code >= 400:
            raise GiteaError(r.status_code, r.text[:300])
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    # repositories and orgs
    async def get_repo(self, owner: str, name: str) -> dict[str, Any]:
        return await self._req("GET", f"/repos/{owner}/{name}")

    async def list_org_repos(self, org: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = await self._req("GET", f"/orgs/{org}/repos", params={"page": page, "limit": 50})
            out.extend(batch or [])
            if not batch or len(batch) < 50:
                return out
            page += 1

    async def get_org(self, org: str) -> dict[str, Any]:
        return await self._req("GET", f"/orgs/{org}")

    # comments
    async def create_issue_comment(self, owner: str, name: str, number: int, body: str) -> dict[str, Any]:
        return await self._req("POST", f"/repos/{owner}/{name}/issues/{number}/comments", json={"body": body})

    async def edit_issue_comment(self, owner: str, name: str, comment_id: int, body: str) -> dict[str, Any]:
        return await self._req("PATCH", f"/repos/{owner}/{name}/issues/comments/{comment_id}", json={"body": body})

    # tokens (broker backend; requires basic auth as the token's owner)
    async def create_token(self, user: str, name: str, scopes: list[str]) -> dict[str, Any]:
        return await self._req("POST", f"/users/{user}/tokens", json={"name": name, "scopes": scopes})

    async def list_tokens(self, user: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = await self._req("GET", f"/users/{user}/tokens", params={"page": page, "limit": 50})
            out.extend(batch or [])
            if not batch or len(batch) < 50:
                return out
            page += 1

    async def delete_token(self, user: str, token_id_or_name: int | str) -> None:
        await self._req("DELETE", f"/users/{user}/tokens/{token_id_or_name}")

    async def version(self) -> str:
        return (await self._req("GET", "/version") or {}).get("version", "")

    async def healthy(self) -> bool:
        """`GET /api/healthz` (unauthenticated even with REQUIRE_SIGNIN_VIEW=true, unlike /api/v1/version)."""
        r = await self._c.get(str(self._c.base_url).rstrip("/").removesuffix("/v1") + "/healthz")
        return r.status_code == 200
