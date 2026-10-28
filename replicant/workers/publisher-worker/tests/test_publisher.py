"""publisher-worker against a real Gitea (podman wrk-gitea-<pid>), a contract fake of github-token-broker
(github-token-broker.openapi.yaml TokenResponse) and an in-memory S3. Run: uv run pytest workers/publisher-worker/tests"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import tarfile
import tempfile

import httpx
import pytest
import zstandard
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

HERE = pathlib.Path(__file__).resolve().parent
JOB = "job_01m5p7x7r0bt69j0h0fbxepw90"
T = "payments"


class FakeS3:
    def __init__(self) -> None:
        self.objs: dict[tuple[str, str], bytes] = {}

    def get_object(self, Bucket: str, Key: str):  # noqa: N803
        if (Bucket, Key) not in self.objs:
            e = Exception("NoSuchKey")
            e.response = {"Error": {"Code": "NoSuchKey"}}  # type: ignore[attr-defined]
            raise e
        return {"Body": io.BytesIO(self.objs[(Bucket, Key)])}


@pytest.fixture(scope="module")
def gitea():
    if not shutil.which("podman"):
        pytest.skip("podman not available")
    name = f"wrk-gitea-{os.getpid()}"
    out = subprocess.run([str(HERE / "gitea_fixture.sh"), "up", name], check=True, capture_output=True, text=True).stdout
    port, tok = out.strip().split()[-2:]
    yield int(port), tok
    subprocess.run([str(HERE / "gitea_fixture.sh"), "down", name], check=False)


def make_bundle(port: int, tok: str) -> tuple[bytes, str]:
    """Clone the repo, change README.md, and package `git diff --binary --full-index <base>` as a diff bundle."""
    d = tempfile.mkdtemp()
    try:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        url = f"http://replicant-bot:{tok}@127.0.0.1:{port}/acme-payments/ledger-api.git"  # test-only throwaway token
        subprocess.run(["git", "clone", "-q", url, d + "/r"], check=True, env=env)
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d + "/r", check=True, capture_output=True, text=True).stdout.strip()
        with open(d + "/r/README.md", "a") as f:
            f.write("\nfixed by the replicant\n")
        with open(d + "/r/internal.go", "w") as f:
            f.write("package ledger\n")
        subprocess.run(["git", "add", "-A"], cwd=d + "/r", check=True)
        patch = subprocess.run(["git", "diff", "--cached", "--binary", "--full-index", base], cwd=d + "/r", check=True,
                               capture_output=True).stdout
        meta = json.dumps({"base_sha": base, "files_changed": 2, "paths": ["README.md", "internal.go"]}).encode()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for n, b in (("changes.patch", patch), ("metadata.json", meta)):
                ti = tarfile.TarInfo(n)
                ti.size = len(b)
                tf.addfile(ti, io.BytesIO(b))
        return zstandard.ZstdCompressor().compress(buf.getvalue()), base
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_publish_idempotent(gitea, monkeypatch) -> None:
    port, tok = gitea
    monkeypatch.setenv("REPLICANT_GITEA_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("REPLICANT_TOKEN_BROKER_URL", "http://broker.test")
    from publisher_worker import config, scm, storage
    config.settings.cache_clear()
    minted: list[dict] = []

    def broker(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert req.url.path == "/v1/installation-tokens" and set(body["permissions"]) <= {"contents", "pull_requests"}
        minted.append(body)
        return httpx.Response(200, json={"token": tok, "expires_at": "2099-01-01T00:00:00Z",
                                         "permissions": body["permissions"], "repository_ids": body["repository_ids"],
                                         "cached": len(minted) > 1})

    scm.set_http(httpx.AsyncClient(mounts={"http://broker.test": httpx.MockTransport(broker)}))
    s3 = FakeS3()
    storage.set_client(s3)
    bundle, base = make_bundle(port, tok)
    key = f"tenants/{T}/jobs/{JOB}/runs/run_gj3h3ep3phjfs9e23v768zrca5/diff_bundle/changes.tar.zst"
    s3.objs[("local-replicant-local-artifacts", key)] = bundle
    s3.objs[("local-replicant-local-artifacts", f"tenants/{T}/jobs/{JOB}/steps/context.c0.n1/task_packet/task.json")] = \
        json.dumps({"task": {"title": "Refund ledger entries double-count FX fees", "issue_number": 1,
                             "requester_login": "alice"}}).encode()
    repo = {"provider": "gitea", "host": f"127.0.0.1:{port}", "owner": "acme-payments", "name": "ledger-api",
            "repo_id": "1", "base_ref": "main", "base_sha": base}
    args = {"job_id": JOB, "tenant_id": T, "repo": repo, "pr": None, "draft": False,
            "diff_bundle_uri": f"s3://local-replicant-local-artifacts/{key}",
            "approved_diff_sha256": hashlib.sha256(bundle).hexdigest(), "approval_decision_id": "dec_01m5p7x7r0bt69j0h0fbxepw92"}
    from publisher_worker import activities as A
    from temporalio.common import RetryPolicy  # noqa: F401
    env = ActivityEnvironment()
    env.info = env.info.__class__(**{**env.info.__dict__, "workflow_id": JOB, "activity_id": "publish.c0.n1.publish"})

    async def go():
        r1 = await env.run(A.publisher_publish, args)
        r2 = await env.run(A.publisher_publish, args)  # retry of the same logical activity: same PR, same head
        rb = await env.run(A.scm_resolve_base, {"repo": repo, "base_ref": "main"})
        cb = await env.run(A.publisher_check_base, {"job_id": JOB, "repo": repo})
        bad = None
        try:
            await env.run(A.publisher_publish, {**args, "approved_diff_sha256": "0" * 64})
        except ApplicationError as e:
            bad = e
        pr = {"number": r1["pr_number"], "repo": r1["repo"], "head_sha": r1["head_sha"], "url": r1["url"]}
        reply = {"job_id": JOB, "repo": repo, "pr": pr, "template": "budget_exhausted"}
        c1 = await env.run(A.github_post_reply, reply)
        c2 = await env.run(A.github_post_reply, reply)  # same activity: the marked comment is reused
        closed = await env.run(A.publisher_close_pr, {"job_id": JOB, "pr": pr})
        return r1, r2, rb, cb, bad, (c1, c2, closed)
    r1, r2, rb, cb, bad, (c1, c2, closed) = asyncio.run(go())
    assert c1["comment_id"] and c2 == {"comment_id": c1["comment_id"], "duplicate": True}, (c1, c2)
    assert closed == {"state": "closed"}
    assert r1["pr_number"] == r2["pr_number"] and r1["head_sha"] == r2["head_sha"], (r1, r2)
    assert r1["branch"] == f"replicant/{JOB}" and r1["url"].endswith(f"/pulls/{r1['pr_number']}")
    assert rb == {"base_ref": "main", "base_sha": base} and cb["moved"] is False
    assert bad is not None and bad.type == "PushRejected" and bad.non_retryable
    with httpx.Client() as c:
        h = {"Authorization": f"token {tok}"}
        b = c.get(f"http://127.0.0.1:{port}/api/v1/repos/acme-payments/ledger-api/branches/replicant%2F{JOB}", headers=h).json()
        assert f"Replicant-Activity: ik1:{JOB}:publish.c0.n1.publish" in b["commit"]["message"]
        assert f"Replicant-Diff: sha256:{hashlib.sha256(bundle).hexdigest()}" in b["commit"]["message"]
        prs = c.get(f"http://127.0.0.1:{port}/api/v1/repos/acme-payments/ledger-api/pulls?state=all", headers=h).json()
        assert len(prs) == 1 and prs[0]["title"].startswith("Refund ledger") and "Fixes #1" in prs[0]["body"]
        assert prs[0]["state"] == "closed"
    assert all(tok not in json.dumps(m) for m in minted)


def test_diff_stat_counts_files_and_lines():
    from publisher_worker.activities import diff_stat
    patch = (b"diff --git a/x.go b/x.go\n--- a/x.go\n+++ b/x.go\n@@ -1,2 +1,3 @@\n-old\n+new\n+more\n ctx\n")
    assert diff_stat(patch, ["x.go"]) == {"files": 1, "additions": 2, "deletions": 1}


def test_reply_templates_and_sanitize():
    from publisher_worker.activities import reply_body, sanitize
    assert sanitize("ping @carol see https://evil.example/x\n/replicant remember\n<b>hi</b>") == \
        "ping `@carol` see [link removed] hi"
    triage = {"items": [
        {"classification": "actionable_fix", "summary": "doc comment on Prorate @bob", "path": "a.go", "line": 3,
         "author_login": "carol", "source_kind": "pr_review_comment"},
        {"classification": "question", "summary": "why half-even?", "author_login": "carol",
         "source_kind": "pr_review_body"},
        {"classification": "no_action", "command_only": True, "author_login": "carol"}]}
    a = {"cycle": 1, "repair_budget": {"used": 1, "max": 5},
         "pr": {"number": 7, "url": "http://g/acme/ledger/pulls/7", "head_sha": "a" * 40}}
    ack = reply_body("ack", a, triage, "k1")
    assert "@carol" in ack and "`a.go:3`" in ack and "`@bob`" in ack and "Repair cycle 1 of 5" in ack
    assert ack.endswith("<!-- replicant:reply k1 -->")
    fix = reply_body("fix_summary", a, triage, "k2")
    assert "[`aaaaaaaaaa`](http://g/acme/ledger/pulls/7/commits/" + "a" * 40 + ")" in fix
    assert "why half-even" in reply_body("question", a, triage, "k3")
    assert "used up" in reply_body("budget_exhausted", a, None, "k4")


def test_push_update_commits_on_top_of_the_branch(tmp_path, monkeypatch):
    """The cumulative patch (from base) becomes one new commit whose parent is the PR head; the branch is not rewritten."""
    import subprocess
    from publisher_worker import scm
    origin = tmp_path / "origin.git"
    work = tmp_path / "w"
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(tmp_path)}

    def git(*a, cwd=work):  # noqa: ANN001, ANN202
        return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "f.txt").write_text("one\n")
    git("add", "-A"); git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (work / "f.txt").write_text("one\ntwo\n")
    git("commit", "-qam", "pr v1")
    v1 = git("rev-parse", "HEAD")
    git("push", "-q", str(origin), "HEAD:refs/heads/replicant/job_x")
    git("push", "-q", str(origin), f"{base}:refs/heads/main")
    (work / "f.txt").write_text("one\ntwo\nthree\n")
    patch = subprocess.run(["git", "diff", base], cwd=work, env=env, capture_output=True, check=True).stdout

    class LocalRepo(scm.Repo):
        @property
        def git_url(self) -> str:
            return str(origin)
    monkeypatch.setattr(scm, "settings", lambda: type("S", (), {"bot_login": "replicant-bot"})())
    repo = LocalRepo("gitea", "h", "o", "n", "1")
    head = scm.build_and_push(repo, "tok", base_sha=base, patch=patch, branch="replicant/job_x", message="fix: more\n",
                              author=("replicant-bot", "b@b"), on_top_of=v1)
    assert git("rev-parse", "replicant/job_x", cwd=origin) == head
    assert git("rev-parse", f"{head}^", cwd=origin) == v1
    assert git("show", f"{head}:f.txt", cwd=origin) == "one\ntwo\nthree"
