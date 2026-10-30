"""Read-only PR and repository inspection for worker activities (MEM-D20, MEM-PR-D20): Gitea REST API with a
`contents:read`/`metadata:read` token from github-token-broker (caller `replicant-worker`).

Helpers, not activities: call them from inside an activity (memory.verify_scan_candidates, memory.ingest_human_edits,
memory.record_outcome). Everything read here is untrusted repository data: callers cap it, redact it and quote it as
data, never as instructions.

    g = await pr_inspect.connect(repo_ref, job_id=..., purpose="memory.human_edits")
    await g.pr_heads(number, replicant_head_sha=s.pr["head_sha"]) -> PR heads (merged head, Replicant's last push)
    await g.commits_between(base_sha, head_sha)                  -> [commit] (oldest first; author login, bot flag)
    await g.diff_summary(base_sha, head_sha, exclude_bot=True)   -> {files, commits, truncated} (capped, redacted)
    await g.file_at(sha, path) / await g.tree(sha)               -> text | None / [paths]

Pure helpers: is_bot_commit, summarize_patch, redact.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from temporalio.exceptions import ApplicationError

from replicant_worker.activities.base import http
from replicant_worker.activities.config import env, read_secret

BOT_LOGINS = frozenset({"replicant-bot"})
BOT_EMAILS = frozenset({"replicant-bot@replicant.localhost"})
BOT_TRAILER = "Replicant-Activity:"  # every publisher commit carries it (publisher_worker.activities.commit_message)
PERMS = {"contents": "read", "metadata": "read"}
MAX_COMMITS = 50

# diff summary caps (human_edit feedback items; safety.md: untrusted, capped, redacted)
MAX_FILES = 20
MAX_HUNKS_PER_FILE = 4
MAX_HUNK_LINES = 30
MAX_SUMMARY_CHARS = 6000

_tokens: dict[tuple[str, str], tuple[str, float]] = {}


def redact(text: str) -> str:
    try:
        from replicant_telemetry.redact import redact_text
    except ImportError:  # pragma: no cover - replicant-telemetry is a worker dependency
        return text
    return redact_text(text)


def is_bot_commit(c: dict[str, Any]) -> bool:
    """A commit Replicant pushed: author/committer login replicant-bot, the bot e-mail, or the publisher trailer."""
    logins = {c.get("author_login"), c.get("committer_login")}
    return bool(logins & BOT_LOGINS) or (c.get("author_email") or "").lower() in BOT_EMAILS \
        or BOT_TRAILER in (c.get("message") or "")


def _commit(d: dict[str, Any]) -> dict[str, Any]:
    cm = d.get("commit") or {}
    au, co = d.get("author") or {}, d.get("committer") or {}
    out = {"sha": d.get("sha"), "message": (cm.get("message") or "")[:2000],
           "author_login": au.get("login") or None, "author_id": au.get("id"),
           "author_name": (cm.get("author") or {}).get("name"), "author_email": (cm.get("author") or {}).get("email"),
           "committer_login": co.get("login") or None, "authored_at": (cm.get("author") or {}).get("date"),
           "parents": [p.get("sha") for p in d.get("parents") or [] if p.get("sha")]}
    out["is_bot"] = is_bot_commit(out)
    return out


_HUNK = re.compile(r"^@@ .* @@")


def summarize_patch(patch: str, *, max_files: int = MAX_FILES, max_hunks: int = MAX_HUNKS_PER_FILE,
                    max_lines: int = MAX_HUNK_LINES) -> tuple[list[dict[str, Any]], bool]:
    """A unified diff -> ([{path, status, additions, deletions, hunks: [text]}], truncated). Binary files keep no hunks;
    hunk text is capped per hunk and per file, and redacted."""
    files: list[dict[str, Any]] = []
    truncated = False
    cur: dict[str, Any] | None = None
    hunk: list[str] | None = None

    def close_hunk() -> None:
        nonlocal hunk, truncated
        if cur is not None and hunk is not None:
            if len(cur["hunks"]) < max_hunks:
                body = hunk[:max_lines]
                if len(hunk) > max_lines:
                    body.append(f"… [{len(hunk) - max_lines} more lines]")
                    truncated = True
                cur["hunks"].append(redact("\n".join(body)))
            else:
                cur["hunks_omitted"] = cur.get("hunks_omitted", 0) + 1
                truncated = True
        hunk = None

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            close_hunk()
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            path = m.group(2) if m else line[11:]
            cur = {"path": path, "status": "modified", "additions": 0, "deletions": 0, "hunks": []}
            files.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur["status"] = "added"
        elif line.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif line.startswith("rename from "):
            cur["status"] = "renamed"
            cur["old_path"] = line[12:]
        elif line.startswith("Binary files"):
            cur["binary"] = True
        elif _HUNK.match(line):
            close_hunk()
            hunk = [line]
        elif hunk is not None and not line.startswith(("--- ", "+++ ")):
            hunk.append(line[:300])
            if line.startswith("+"):
                cur["additions"] += 1
            elif line.startswith("-"):
                cur["deletions"] += 1
    close_hunk()
    if len(files) > max_files:
        truncated = True
        files = files[:max_files]
    return files, truncated


@dataclass
class Gitea:
    """One repository on the Gitea API v1, read-only. `repo` is a repo_ref ({host, owner, name, repo_id?})."""
    base: str
    owner: str
    name: str
    token: str

    async def _get(self, path: str, *, params: dict[str, Any] | None = None, raw: bool = False,
                   missing: Any = None) -> Any:
        url = f"{self.base}/api/v1/repos/{self.owner}/{self.name}{path}"
        try:
            r = await http().get(url, params=params, headers={"Authorization": f"token {self.token}"},
                                 timeout=httpx.Timeout(30.0, connect=5.0))
        except httpx.HTTPError as e:
            raise ApplicationError(f"gitea GET {path.split('?')[0][:80]}: {type(e).__name__}",
                                   type="ScmUnavailable") from None
        if r.status_code in (404, 422):
            return missing
        if r.status_code in (401, 403):
            raise ApplicationError(f"gitea {path[:80]}: HTTP {r.status_code}", type="PermissionDenied",
                                   non_retryable=True)
        if r.status_code >= 400:
            raise ApplicationError(f"gitea {path[:80]}: HTTP {r.status_code}", type="ScmUnavailable",
                                   non_retryable=r.status_code < 500 and r.status_code != 429)
        return r.text if raw else r.json()

    async def pr(self, number: int) -> dict[str, Any] | None:
        return await self._get(f"/pulls/{int(number)}")

    async def pr_heads(self, number: int, *, replicant_head_sha: str | None = None) -> dict[str, Any] | None:
        """{number, state, merged, merged_head_sha (the PR branch head at merge, or now), merge_commit_sha, base_sha,
        base_ref, head_ref, replicant_head_sha, human_pushed (head moved past Replicant's last push)}."""
        p = await self.pr(number)
        if not p:
            return None
        head = (p.get("head") or {}).get("sha")
        rh = replicant_head_sha or head
        return {"number": int(number), "state": p.get("state"), "merged": bool(p.get("merged")),
                "merged_head_sha": head, "merge_commit_sha": p.get("merge_commit_sha"),
                "base_sha": p.get("merge_base") or (p.get("base") or {}).get("sha"),
                "base_ref": (p.get("base") or {}).get("ref"), "head_ref": (p.get("head") or {}).get("ref"),
                "replicant_head_sha": rh, "human_pushed": bool(head and rh and head != rh)}

    async def commit(self, sha: str) -> dict[str, Any] | None:
        d = await self._get(f"/git/commits/{quote(sha, safe='')}", params={"stat": "false", "files": "false",
                                                                            "verification": "false"})
        return _commit(d) if d else None

    async def commits_between(self, base_sha: str, head_sha: str, *, limit: int = MAX_COMMITS) -> list[dict[str, Any]]:
        """Commits reachable from head_sha down to (excluding) base_sha along first parents, oldest first. Stops at
        `limit` (the list is then marked by a final {"truncated": true} only through len == limit)."""
        out: list[dict[str, Any]] = []
        sha = head_sha
        while sha and sha != base_sha and len(out) < limit:
            c = await self.commit(sha)
            if c is None:
                break
            out.append(c)
            sha = c["parents"][0] if c["parents"] else None
        return list(reversed(out))

    async def commit_patch(self, sha: str) -> str:
        return await self._get(f"/git/commits/{quote(sha, safe='')}.diff", raw=True, missing="") or ""

    async def diff_summary(self, base_sha: str, head_sha: str, *, exclude_bot: bool = False,
                           commits: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Per-commit patches of (base_sha, head_sha] (bot commits dropped when exclude_bot) summarized and capped:
        {commits: [{sha, author_login, message}], files: [...summarize_patch], truncated}. Messages are redacted."""
        cs = commits if commits is not None else await self.commits_between(base_sha, head_sha)
        keep = [c for c in cs if not (exclude_bot and c.get("is_bot"))]
        files: dict[str, dict[str, Any]] = {}
        truncated = len(cs) >= MAX_COMMITS
        for c in keep[:10]:
            fs, t = summarize_patch(await self.commit_patch(c["sha"]))
            truncated = truncated or t
            for f in fs:
                prev = files.get(f["path"])
                if prev is None:
                    files[f["path"]] = f
                else:
                    prev["additions"] += f["additions"]
                    prev["deletions"] += f["deletions"]
                    room = MAX_HUNKS_PER_FILE - len(prev["hunks"])
                    prev["hunks"] += f["hunks"][:max(room, 0)]
                    truncated = truncated or len(f["hunks"]) > room
        flist = list(files.values())
        if len(flist) > MAX_FILES:
            flist, truncated = flist[:MAX_FILES], True
        return {"base_sha": base_sha, "head_sha": head_sha, "truncated": truncated or len(keep) > 10,
                "commits": [{"sha": c["sha"], "author_login": c.get("author_login"), "author_id": c.get("author_id"),
                             "message": redact((c.get("message") or "")[:500])} for c in keep[:10]],
                "files": flist}

    async def file_at(self, sha: str, path: str, *, max_bytes: int = 65536) -> str | None:
        """The file's text at `sha`, None when missing; capped at max_bytes, None for binary content."""
        t = await self._get(f"/raw/{quote(path)}", params={"ref": sha}, raw=True)
        if t is None or "\x00" in t[:4096]:
            return None
        return t[:max_bytes]

    async def tree(self, sha: str, *, max_entries: int = 20000) -> list[str]:
        """Blob paths of the tree at `sha` (recursive)."""
        out: list[str] = []
        for page in range(1, 50):
            d = await self._get(f"/git/trees/{quote(sha, safe='')}", params={"recursive": "true", "page": page,
                                                                              "per_page": 1000})
            if not d:
                break
            out += [e["path"] for e in d.get("tree") or [] if e.get("type") == "blob" and e.get("path")]
            if not d.get("truncated") or len(out) >= max_entries:
                break
        return out[:max_entries]


async def token(repo: dict[str, Any], *, job_id: str | None, purpose: str) -> str:
    """A read-only installation token from github-token-broker (caller replicant-worker), cached until 10 min before
    expiry. Never logged."""
    rid = repo.get("repo_id")
    rid = int(rid) if rid is not None and str(rid).isdigit() else None
    key = (f"{repo.get('host')}/{repo.get('owner')}/{repo.get('name')}", purpose)
    hit = _tokens.get(key)
    if hit and hit[1] > time.time() + 600:
        return hit[0]
    caller = read_secret(env("REPLICANT_TOKEN_BROKER_TOKEN_FILE"))
    if not caller:
        raise ApplicationError("no token-broker caller token (REPLICANT_TOKEN_BROKER_TOKEN_FILE)",
                               type="PermissionDenied", non_retryable=True)
    body: dict[str, Any] = {"installation_id": int(env("REPLICANT_SCM_INSTALLATION_ID", "1") or "1"),
                            "repository_ids": [rid] if rid is not None else [], "permissions": PERMS,
                            "purpose": purpose}
    if job_id:
        body["job_id"] = job_id
    url = (env("REPLICANT_TOKEN_BROKER_URL", "http://github-token-broker:8080") or "").rstrip("/")
    try:
        r = await http().post(f"{url}/v1/installation-tokens", json=body, headers={"Authorization": f"Bearer {caller}"})
    except httpx.HTTPError as e:
        raise ApplicationError(f"token broker unreachable: {type(e).__name__}", type="BrokerUnavailable") from None
    if r.status_code in (401, 403, 404):
        raise ApplicationError(f"token broker refused replicant-worker: HTTP {r.status_code}",
                               type="PermissionDenied", non_retryable=True)
    if r.status_code != 200:
        raise ApplicationError(f"token broker HTTP {r.status_code}", type="BrokerUnavailable")
    d = r.json()
    exp = time.time() + 3000
    try:
        from datetime import datetime
        exp = datetime.fromisoformat(str(d["expires_at"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        pass
    _tokens[key] = (d["token"], exp)
    return d["token"]


def split_repo(repo: str | dict[str, Any]) -> dict[str, Any]:
    """'host/owner/name' (or a repo_ref) -> repo_ref."""
    if isinstance(repo, dict):
        return repo
    host, owner, name = str(repo).split("/", 2)
    return {"host": host, "owner": owner, "name": name}


async def connect(repo: str | dict[str, Any], *, job_id: str | None = None, purpose: str = "worker.pr_inspect") -> Gitea:
    ref = split_repo(repo)
    base = (env("REPLICANT_GITEA_BASE_URL") or f"http://{ref['host']}").rstrip("/")
    return Gitea(base, ref["owner"], ref["name"], await token(ref, job_id=job_id, purpose=purpose))
