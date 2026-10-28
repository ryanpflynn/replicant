"""SCM access for the Publisher: tokens from github-token-broker (CONTROL-03), the Gitea REST API, and git over HTTP.

Tokens never appear in argv, URLs, logs or exceptions: git reads them through GIT_ASKPASS from the child process
environment, and REST calls send `Authorization: token <t>`."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from replicant_scm.gitea import GiteaClient, GiteaError
from temporalio.exceptions import ApplicationError

from publisher_worker.config import read_secret, settings

_http: httpx.AsyncClient | None = None
_tokens: dict[tuple, tuple[str, float]] = {}


def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    return _http


def set_http(c: httpx.AsyncClient | None) -> None:
    global _http
    _http = c


@dataclass(frozen=True)
class Repo:
    provider: str
    host: str
    owner: str
    name: str
    repo_id: str | None

    @classmethod
    def of(cls, d: dict[str, Any]) -> "Repo":
        return cls(d.get("provider", "gitea"), d["host"], d["owner"], d["name"], d.get("repo_id"))

    @property
    def full(self) -> str:
        return f"{self.host}/{self.owner}/{self.name}"

    @property
    def web(self) -> str:
        return settings().gitea_base_url or f"http://{self.host}"

    @property
    def git_url(self) -> str:
        return f"{self.web}/{self.owner}/{self.name}.git"


async def token(repo: Repo, *, purpose: str, job_id: str | None, activity_id: str | None,
                permissions: dict[str, str] | None = None) -> str:
    """POST /v1/installation-tokens (github-token-broker.openapi.yaml TokenRequest). Cached until 10 min before expiry."""
    perms = permissions or {"contents": "write", "pull_requests": "write"}
    rid = int(repo.repo_id) if repo.repo_id and str(repo.repo_id).isdigit() else None
    ck = (repo.full, tuple(sorted(perms.items())))
    hit = _tokens.get(ck)
    if hit and hit[1] > time.time() + 600:
        return hit[0]
    s = settings()
    body: dict[str, Any] = {"installation_id": s.installation_id, "repository_ids": [rid] if rid is not None else [],
                            "permissions": perms, "purpose": purpose}
    if job_id:
        body["job_id"] = job_id
    if activity_id:
        body["activity_id"] = activity_id
    caller = read_secret(s.broker_token_file)
    headers = {"Authorization": f"Bearer {caller}"} if caller else {}
    try:
        r = await http().post(f"{s.broker_url}/v1/installation-tokens", json=body, headers=headers)
    except httpx.HTTPError as e:
        raise ApplicationError(f"token broker unreachable: {type(e).__name__}", type="BrokerUnavailable") from None
    if r.status_code in (401, 403):
        raise ApplicationError(f"token broker refused the publisher: HTTP {r.status_code}", type="PermissionDenied",
                               non_retryable=True)
    if r.status_code == 404:
        raise ApplicationError("installation unavailable", type="PermissionDenied", non_retryable=True)
    if r.status_code != 200:
        raise ApplicationError(f"token broker HTTP {r.status_code}", type="BrokerUnavailable")
    d = r.json()
    exp = time.time() + 3000
    try:
        from datetime import datetime
        exp = datetime.fromisoformat(str(d["expires_at"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        pass
    _tokens[ck] = (d["token"], exp)
    return d["token"]


_BODY = object()


class Gitea:
    """The Publisher's view of one repository on `replicant_scm.GiteaClient` (sharing this module's HTTP client), with
    Gitea failures mapped to the Temporal error types of the publisher activities."""

    def __init__(self, repo: Repo, tok: str) -> None:
        self.repo = repo
        self.client = GiteaClient(repo.web, token=tok, http=http())

    async def _call(self, what: str, fn: Any, *a: Any, other_4xx: Any = _BODY, raise_for: tuple[int, ...] = (),
                    **kw: Any) -> Any:
        """401/403 -> PermissionDenied (non-retryable), 429/5xx and transport errors -> ScmUnavailable; any other 4xx
        returns `other_4xx` (default: the error document, as the Publisher has always read it) unless in `raise_for`."""
        try:
            return await fn(self.repo.owner, self.repo.name, *a, **kw)
        except httpx.HTTPError as e:
            raise ApplicationError(f"gitea {what}: {type(e).__name__}", type="ScmUnavailable") from None
        except GiteaError as e:
            if e.status in (401, 403):
                raise ApplicationError(f"gitea {what}: HTTP {e.status}", type="PermissionDenied",
                                       non_retryable=True) from None
            if e.status >= 500 or e.status == 429:
                raise ApplicationError(f"gitea {what}: HTTP {e.status}", type="ScmUnavailable") from None
            if e.status in raise_for:
                raise
            return e.body if other_4xx is _BODY else other_4xx

    async def repo_info(self) -> dict[str, Any]:
        return await self._call("GET ", self.client.get_repo)

    async def branch(self, name: str) -> dict[str, Any] | None:
        return await self._call(f"GET /branches/{quote(name, safe='')}", self.client.get_branch, name, other_4xx=None)

    async def commit(self, sha: str) -> dict[str, Any] | None:
        return await self._call(f"GET /git/commits/{sha}", self.client.get_commit, sha, other_4xx=None)

    async def pulls_for_head(self, head: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 11):
            items = await self._call("GET /pulls", self.client.list_pulls, state="all", page=page, limit=50) or []
            out += [p for p in items if (p.get("head") or {}).get("ref") == head]
            if len(items) < 50:
                break
        return out

    async def create_pull(self, *, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        try:
            return await self._call("POST /pulls", self.client.create_pull, head=head, base=base, title=title,
                                    body=body, raise_for=(409, 422))
        except GiteaError as e:  # "already exists": re-read and return (§10.1 step 8)
            ex = [p for p in await self.pulls_for_head(head) if p.get("state") == "open"]
            if ex:
                return ex[0]
            raise ApplicationError(f"gitea refused the PR: HTTP {e.status}", type="InvalidInput",
                                   non_retryable=True) from None

    async def edit_pull(self, number: int, **fields: Any) -> dict[str, Any]:
        return await self._call(f"PATCH /pulls/{number}", self.client.edit_pull, number, **fields)

    async def comments(self, number: int) -> list[dict[str, Any]]:
        return await self._call(f"GET /issues/{number}/comments", self.client.list_issue_comments, number) or []

    async def comment(self, number: int, body: str) -> dict[str, Any]:
        return await self._call(f"POST /issues/{number}/comments", self.client.create_issue_comment, number, body)


# ------------------------------------------------------------------------------------------------ git
ASKPASS = "#!/bin/sh\ncase \"$1\" in *sername*) echo \"$REPLICANT_GIT_USER\" ;; *) echo \"$REPLICANT_GIT_TOKEN\" ;; esac\n"


class GitError(Exception):
    pass


def _git(cwd: str, *args: str, env: dict[str, str], input_: bytes | None = None, timeout: int = 300) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, env=env, input=input_, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace")
        tok = env.get("REPLICANT_GIT_TOKEN")
        if tok:
            err = err.replace(tok, "***")
        raise GitError(f"git {args[0]} failed: {err.strip()[-800:]}")
    return p.stdout.decode("utf-8", "replace").strip()


def build_and_push(repo: Repo, tok: str, *, base_sha: str, patch: bytes, branch: str, message: str,
                   author: tuple[str, str], parent: str | None = None, on_top_of: str | None = None) -> str:
    """Clone, check out `parent or base_sha`, apply the patch with `git apply --index --binary`, commit and push
    `HEAD:refs/heads/<branch>` (force: the branch is replicant-owned, §10.1 step 6.2). Returns the new head SHA."""
    work = tempfile.mkdtemp(prefix="pub-")
    try:
        ap = os.path.join(work, "askpass.sh")
        with open(ap, "w") as f:
            f.write(ASKPASS)
        os.chmod(ap, 0o700)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": work, "GIT_ASKPASS": ap,
               "GIT_TERMINAL_PROMPT": "0", "REPLICANT_GIT_USER": settings().bot_login, "REPLICANT_GIT_TOKEN": tok,
               "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1], "GIT_COMMITTER_NAME": author[0],
               "GIT_COMMITTER_EMAIL": author[1], "GIT_CONFIG_NOSYSTEM": "1"}
        repo_dir = os.path.join(work, "repo")
        _git(work, "clone", "--quiet", "--no-checkout", repo.git_url, repo_dir, env=env)
        start = parent or base_sha
        try:
            _git(repo_dir, "cat-file", "-e", f"{start}^{{commit}}", env=env)
        except GitError:
            _git(repo_dir, "fetch", "--quiet", "origin", start, env=env)
        _git(repo_dir, "checkout", "--quiet", "--detach", start, env=env)
        try:
            _git(repo_dir, "apply", "--index", "--binary", "--whitespace=nowarn", "-", env=env, input_=patch)
        except GitError as e:
            raise ApplicationError(f"the approved patch does not apply: {str(e)[:300]}", type="MergeConflict",
                                   non_retryable=True) from None
        if on_top_of:
            # push_update (D-15): the approved patch is cumulative from base_sha; its tree becomes a new commit on top of
            # the PR branch head, so reviewers see an incremental commit and the branch is never rewritten.
            try:
                _git(repo_dir, "cat-file", "-e", f"{on_top_of}^{{commit}}", env=env)
            except GitError:
                _git(repo_dir, "fetch", "--quiet", "origin", on_top_of, env=env)
            tree = _git(repo_dir, "write-tree", env=env)
            head = _git(repo_dir, "commit-tree", tree, "-p", on_top_of, "-F", "-", env=env, input_=message.encode())
        else:
            _git(repo_dir, "commit", "--quiet", "--no-verify", "-F", "-", env=env, input_=message.encode())
            head = _git(repo_dir, "rev-parse", "HEAD", env=env)
        try:
            _git(repo_dir, "push", "--quiet", *([] if on_top_of else ["--force"]), "origin",
                 f"{head}:refs/heads/{branch}", env=env)
        except GitError as e:
            msg = str(e)
            if "protected" in msg.lower():
                raise ApplicationError("branch protection rejected the push", type="BranchProtected", non_retryable=True) from None
            if "403" in msg or "denied" in msg.lower():
                raise ApplicationError("push denied", type="PermissionDenied", non_retryable=True) from None
            raise ApplicationError(f"push failed: {msg[-300:]}", type="ScmUnavailable") from None
        return head
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def build_and_push_async(*a: Any, **kw: Any) -> str:
    return await asyncio.to_thread(build_and_push, *a, **kw)
