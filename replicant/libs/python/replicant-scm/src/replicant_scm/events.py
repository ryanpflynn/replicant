"""Normalized SCM event model (github-app.md G-9). Provider adapters produce these; routing consumes only these.

`kind` uses GitHub's `<event>.<action>` vocabulary (github-app.md §3.4 routing table), whatever the provider, so that
`JobRequest.source.event_type` and the routing rules are provider-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ScmActor:
    id: int
    login: str
    is_bot: bool = False


@dataclass(frozen=True)
class ScmRepo:
    provider: str            # github | gitea (C-JOBREQ repo.provider)
    host: str                # canonical authority (XD-FIX-R1-273)
    owner: str
    name: str
    repo_id: str             # immutable provider id as a string (Gitea: numeric id)
    default_branch: str | None = None
    html_url: str | None = None
    archived: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def ref(self) -> str:
        return f"{self.host}/{self.owner}/{self.name}"


@dataclass(frozen=True)
class ScmIssue:
    number: int
    title: str
    body: str
    labels: tuple[str, ...] = ()
    html_url: str | None = None
    is_pull: bool = False
    state: str = "open"


@dataclass(frozen=True)
class ScmPullRequest:
    number: int
    head_ref: str | None
    head_sha: str | None
    base_ref: str | None
    merged: bool
    state: str
    html_url: str | None = None


@dataclass(frozen=True)
class ScmComment:
    id: int
    body: str
    user: ScmActor | None = None


@dataclass(frozen=True)
class NormalizedEvent:
    provider: str
    delivery_id: str
    kind: str                       # e.g. issues.labeled, issues.opened, issue_comment.created, pull_request.closed
    repo: ScmRepo | None
    sender: ScmActor | None
    issue: ScmIssue | None = None
    pull_request: ScmPullRequest | None = None
    comment: ScmComment | None = None
    review_state: str | None = None  # approved | changes_requested | commented | dismissed
    org_login: str | None = None
    raw_event: str = ""
    raw_action: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def event(self) -> str:
        return self.kind.split(".", 1)[0]

    @property
    def action(self) -> str | None:
        return self.kind.split(".", 1)[1] if "." in self.kind else None
