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
from urllib.parse import quote

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

    # A plain comment on a PR arrives as event `issue_comment` with type `pull_request_comment`, which is also a review
    # type name: the event header decides first. A submitted review arrives as event `pull_request_comment` (or
    # `pull_request_approved` / `pull_request_rejected`) with type `pull_request_review_*`; its line comments are not
    # in the payload (Gitea 1.2x), only `review.content`.
    if event != "issue_comment" and (event_type in _REVIEW_EVENTS or event in _REVIEW_EVENTS):
        event_type = event_type if event_type in _REVIEW_EVENTS else event
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
    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(f"gitea {status}: {message}")
        self.status = status
        self.body = body  # the parsed JSON error body, when there is one


class GiteaClient:
    """Minimal Gitea API v1 client. `token` is a replicant-bot access token (from github-token-broker) or `basic` is a
    (user, password) pair (the broker's own bootstrap credential). `http` shares an existing AsyncClient (its pool and
    timeouts; not closed by `aclose`) instead of creating one. Never logs credentials."""

    def __init__(self, base_url: str, *, token: str | None = None, basic: tuple[str, str] | None = None,
                 timeout: float | httpx.Timeout = 10.0, transport: httpx.AsyncBaseTransport | None = None,
                 http: httpx.AsyncClient | None = None) -> None:
        self._root = base_url.rstrip("/") + "/api"
        self._headers = {"Accept": "application/json"}
        if token:
            self._headers["Authorization"] = f"token {token}"
        self._auth = httpx.BasicAuth(*basic) if basic else None
        self._own = http is None
        self._c = http if http is not None else httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        if self._own:
            await self._c.aclose()

    async def __aenter__(self) -> "GiteaClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """The raw response of `method /api/v1<path>` (no status handling)."""
        if self._auth is not None:
            kw.setdefault("auth", self._auth)
        return await self._c.request(method, self._root + "/v1" + path, headers=self._headers, **kw)

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        r = await self.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = None
            raise GiteaError(r.status_code, r.text[:300], body)
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

    async def list_issue_comments(self, owner: str, name: str, number: int, limit: int = 50) -> list[dict[str, Any]]:
        return await self._req("GET", f"/repos/{owner}/{name}/issues/{number}/comments", params={"limit": limit}) or []

    # branches, commits and pull requests (the Publisher)
    async def get_branch(self, owner: str, name: str, branch: str) -> dict[str, Any] | None:
        """None when the branch does not exist."""
        try:
            return await self._req("GET", f"/repos/{owner}/{name}/branches/{quote(branch, safe='')}")
        except GiteaError as e:
            if e.status == 404:
                return None
            raise

    async def get_commit(self, owner: str, name: str, sha: str) -> dict[str, Any] | None:
        """None when the commit is unknown (404) or the SHA is malformed (422)."""
        try:
            return await self._req("GET", f"/repos/{owner}/{name}/git/commits/{sha}")
        except GiteaError as e:
            if e.status in (404, 422):
                return None
            raise

    async def list_pulls(self, owner: str, name: str, *, state: str = "all", page: int = 1,
                         limit: int = 50) -> list[dict[str, Any]]:
        return await self._req("GET", f"/repos/{owner}/{name}/pulls",
                               params={"state": state, "limit": limit, "page": page}) or []

    async def create_pull(self, owner: str, name: str, *, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        return await self._req("POST", f"/repos/{owner}/{name}/pulls",
                               json={"head": head, "base": base, "title": title, "body": body})

    async def edit_pull(self, owner: str, name: str, number: int, **fields: Any) -> dict[str, Any]:
        return await self._req("PATCH", f"/repos/{owner}/{name}/pulls/{number}", json=fields)

    # pull request reviews (line comments of a submitted review are not in the webhook payload)
    async def list_pull_reviews(self, owner: str, name: str, number: int) -> list[dict[str, Any]]:
        return await self._req("GET", f"/repos/{owner}/{name}/pulls/{number}/reviews", params={"limit": 50}) or []

    async def review_comments(self, owner: str, name: str, number: int, review_id: int) -> list[dict[str, Any]]:
        return await self._req("GET", f"/repos/{owner}/{name}/pulls/{number}/reviews/{review_id}/comments") or []

    # repository permission of a user, readable with a read:repository + read:organization token (no repo admin)
    async def is_collaborator(self, owner: str, name: str, login: str) -> bool:
        r = await self.request("GET", f"/repos/{owner}/{name}/collaborators/{login}")
        return r.status_code == 204

    async def repo_teams(self, owner: str, name: str) -> list[dict[str, Any]]:
        return await self._req("GET", f"/repos/{owner}/{name}/teams") or []

    async def is_team_member(self, team_id: int, login: str) -> bool:
        r = await self.request("GET", f"/teams/{team_id}/members/{login}")
        return r.status_code in (200, 204)

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
        r = await self._c.get(self._root + "/healthz", headers=self._headers, auth=self._auth or httpx.USE_CLIENT_DEFAULT)
        return r.status_code == 200
