"""The run's repository as a git bundle in its inputs (SIMPLIFY-D40; replaces repo-mirror and agentd's git route).

`context.stage_inputs` calls `stage()`: the worker fetches the commits a sandbox's `prepare` checks out (the run plan's
`workspace.base_sha`, plus `config_base_sha` for the base-branch configuration restore) from the SCM with a read-only
github-token-broker token (caller `replicant-worker`, as pr_inspect), writes one `git bundle` with the refs below and
stages it as the manifest object `repo_bundle` (place `repo_bundle`). `prepare` downloads it through agentd's inputs
route (:7803) and fetches from it; the sandbox has no git endpoint.

Reuse: the bundle is built once per (job, repo, commits) at `tenants/<t>/jobs/<j>/repo-bundles/<digest>.bundle`
(write-once, `x-amz-meta-sha256`) and server-side copied under each run's own `inputs/` prefix (runs read only their
own inputs, C-RUNNER). A new base commit (base_check after the base moved, a repair on a new base) gets a new bundle.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from replicant_worker.activities import pr_inspect, s3
from replicant_worker.activities.config import env

CONTENT_TYPE = "application/x-git-bundle"
REF_BASE = "refs/replicant/base"
REF_CONFIG = "refs/replicant/config"
PURPOSE = "worker.repo_bundle"
GIT_TIMEOUT = 15 * 60


def clone_url(repo: dict[str, Any]) -> str:
    """The SCM's smart-HTTP URL of repo ({provider?, host, owner, name}). REPLICANT_GITEA_BASE_URL (local: the
    in-network Gitea) wins over the repo's public host, as in pr_inspect.connect."""
    base = env("REPLICANT_GITEA_BASE_URL")
    if not base:
        scheme = "https" if str(repo.get("provider") or "") == "github" else "http"
        base = f"{scheme}://{repo['host']}"
    return f"{base.rstrip('/')}/{repo['owner']}/{repo['name']}.git"


def cache_key(tenant: str, job: str, repo: dict[str, Any], refs: dict[str, str]) -> str:
    d = hashlib.sha256(json.dumps({"repo": f"{repo['host']}/{repo['owner']}/{repo['name']}", "refs": refs},
                                  sort_keys=True).encode()).hexdigest()[:40]
    return f"tenants/{tenant}/jobs/{job}/repo-bundles/{d}.bundle"


def wanted_refs(plan_doc: dict[str, Any], fallback_base: str | None = None) -> dict[str, str]:
    """ref -> commit the sandbox needs: the base commit, and the default-branch commit whose repo configuration
    prepare restores (repoconfig.go) when the plan carries one."""
    ws = plan_doc.get("workspace") or {}
    out: dict[str, str] = {}
    base = ws.get("base_sha") or fallback_base
    if base:
        out[REF_BASE] = str(base)
    cfg = plan_doc.get("config_base_sha")
    if cfg and cfg != base:
        out[REF_CONFIG] = str(cfg)
    return out


def _heartbeat(detail: str) -> None:
    try:
        activity.heartbeat(detail)
    except RuntimeError:  # outside an activity (unit tests)
        pass


async def _git(cwd: Path, *args: str, secret: str = "", auth: list[str] | None = None) -> str:
    """git with a clean environment (no system/global config, no prompts); `auth` -c options are passed on the
    command line only and `secret` is scrubbed from any error text. Heartbeats while the command runs."""
    genv = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd), "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"):
        if os.environ.get(k):
            genv[k] = os.environ[k]
    p = await asyncio.create_subprocess_exec("git", *(auth or []), *args, cwd=str(cwd), env=genv,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    comm = asyncio.ensure_future(p.communicate())
    waited = 0
    while not comm.done():
        done, _ = await asyncio.wait({comm}, timeout=20)
        if done:
            break
        waited += 20
        _heartbeat(f"git {args[0]}")
        if waited >= GIT_TIMEOUT:
            p.kill()
            await comm
            raise ApplicationError(f"git {args[0]} timed out", type="RepoFetchFailed")
    out, err = comm.result()
    if p.returncode != 0:
        msg = (err or out).decode(errors="replace").strip()[-600:]
        if secret:
            msg = msg.replace(secret, "[REDACTED]")
        raise ApplicationError(f"git {args[0]}: {msg}", type="RepoFetchFailed")
    return out.decode(errors="replace")


async def build(repo: dict[str, Any], refs: dict[str, str], dest: Path, *, job_id: str | None) -> None:
    """Fetch the wanted commits from the SCM into a scratch bare repository and write `git bundle` to dest."""
    tok = await pr_inspect.token(repo, job_id=job_id, purpose=PURPOSE)
    user = "x-access-token" if str(repo.get("provider") or "") == "github" else "replicant"
    auth = ["-c", "http.extraHeader=Authorization: Basic " + base64.b64encode(f"{user}:{tok}".encode()).decode()]
    url = clone_url(repo)
    fmt = "sha256" if any(len(s) == 64 for s in refs.values()) else "sha1"
    work = Path(tempfile.mkdtemp(prefix="repo-bundle-", dir=env("REPLICANT_REPO_BUNDLE_TMP")))
    try:
        await _git(work, "init", "-q", "--bare", f"--object-format={fmt}", "repo.git")
        g = work / "repo.git"
        specs = [f"{sha}:{ref}" for ref, sha in refs.items()]
        try:  # protocol v2: wants are not limited to advertised tips
            await _git(g, "fetch", "-q", "--no-tags", "--no-write-fetch-head", url, *specs, secret=tok,
                       auth=[*auth, "-c", "protocol.version=2"])
        except ApplicationError:  # servers that only serve advertised tips: every branch, tag and PR head
            await _git(g, "fetch", "-q", "--no-write-fetch-head", url, "+refs/heads/*:refs/fetched/heads/*",
                       "+refs/tags/*:refs/fetched/tags/*", "+refs/pull/*:refs/fetched/pull/*", secret=tok, auth=auth)
            for ref, sha in refs.items():
                try:
                    await _git(g, "update-ref", ref, sha)
                except ApplicationError:
                    raise ApplicationError(f"commit {sha} is not in {repo['owner']}/{repo['name']}",
                                           type="RepoCommitNotFound", non_retryable=True) from None
        for ref, sha in refs.items():  # the bundle's refs name exactly the wanted commits
            got = (await _git(g, "rev-parse", f"{ref}^{{commit}}")).strip()
            if got != sha:
                raise ApplicationError(f"{ref} is {got}, not {sha}", type="RepoFetchFailed", non_retryable=True)
        await _git(g, "bundle", "create", "-q", str(dest), *refs.keys())
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _digest(path: Path) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _head(r: s3.Ref) -> tuple[str, int] | None:
    try:
        h = s3.client().head_object(Bucket=r.bucket, Key=r.key)
    except Exception as e:  # noqa: BLE001
        if s3._code(e) in ("NoSuchKey", "NoSuchBucket", "404", "NotFound"):
            return None
        raise
    sha = (h.get("Metadata") or {}).get("sha256")
    if sha:
        return sha, int(h["ContentLength"])
    body = s3.client().get_object(Bucket=r.bucket, Key=r.key)["Body"]  # no digest metadata: stream it
    d, n = hashlib.sha256(), 0
    for chunk in iter(lambda: body.read(1 << 20), b""):
        d.update(chunk)
        n += len(chunk)
    return d.hexdigest(), n


def _put_file_once(r: s3.Ref, path: Path) -> tuple[str, int]:
    """Write-once upload of a (possibly large) file with its digest as metadata. A concurrent writer's object wins
    (412): bundles of the same commits are equivalent even when their bytes differ."""
    sha, size = _digest(path)
    try:
        with path.open("rb") as f:
            s3.client().put_object(Bucket=r.bucket, Key=r.key, Body=f, ContentLength=size, ContentType=CONTENT_TYPE,
                                   Metadata={"sha256": sha}, IfNoneMatch="*")
    except Exception as e:  # noqa: BLE001
        if s3._code(e) not in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
            raise
        got = _head(r)
        if got is None:
            raise
        return got
    return sha, size


async def stage(*, tenant: str, job: str, run_prefix: str, repo: dict[str, Any] | None,
                refs: dict[str, str]) -> dict[str, Any] | None:
    """The manifest object for the run's repo bundle (None when the run has no repository or base commit)."""
    if not repo or not repo.get("host") or not repo.get("owner") or not repo.get("name") or REF_BASE not in refs:
        activity.logger.warning("no repository or base commit for the run: no repo bundle staged")
        return None
    cached = s3.ref("artifacts", cache_key(tenant, job, repo, refs))
    got = _head(cached)
    if got is None:
        tmp = Path(tempfile.mkdtemp(prefix="repo-bundle-out-", dir=env("REPLICANT_REPO_BUNDLE_TMP")))
        try:
            out = tmp / "repo.bundle"
            await build(repo, refs, out, job_id=job)
            got = await asyncio.to_thread(_put_file_once, cached, out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    sha, size = got
    dst = s3.ref("artifacts", run_prefix + "inputs/repo.bundle")
    if not s3.exists(dst):  # a retried attempt finds its earlier copy of the same write-once object
        s3.copy(cached, dst)
    return {"name": "repo_bundle", "type": "run_inputs", "bucket_class": "artifacts", "key": dst.key, "sha256": sha,
            "size_bytes": size, "content_type": CONTENT_TYPE, "place": "repo_bundle"}
