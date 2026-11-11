import hashlib
import hmac

from replicant_scm import canonical_host, repo_ref, verify_gitea_signature, verify_github_signature
from replicant_scm.gitea import normalize

REPO = {"id": 3, "name": "ledger-api", "full_name": "acme-payments/ledger-api", "default_branch": "main",
        "owner": {"id": 2, "login": "acme-payments", "username": "acme-payments"},
        "html_url": "http://git.replicant.localhost:3300/acme-payments/ledger-api"}
ALICE = {"id": 5, "login": "alice", "username": "alice"}
BOT = {"id": 9, "login": "replicant-bot", "username": "replicant-bot"}
HOST = "http://git.replicant.localhost:3300"


def test_canonical_host():
    assert canonical_host("Git.Replicant.Localhost:3300") == "git.replicant.localhost:3300"
    assert canonical_host("http://git.replicant.localhost:3300/") == "git.replicant.localhost:3300"
    assert canonical_host("https://github.com:443") == "github.com"
    assert canonical_host("http://example.com:80/x") == "example.com"
    assert repo_ref("GIT.replicant.localhost:3300", "o", "n") == "git.replicant.localhost:3300/o/n"


def test_signatures():
    body = b'{"a":1}'
    sec = b"s3cret"
    hexsig = hmac.new(sec, body, hashlib.sha256).hexdigest()
    assert verify_gitea_signature(hexsig, body, sec)
    assert verify_gitea_signature(hexsig.upper(), body, [b"old", sec])
    assert not verify_gitea_signature(hexsig, body + b" ", sec)
    assert not verify_gitea_signature(None, body, sec)
    assert not verify_gitea_signature("abc", body, sec)
    assert verify_github_signature("sha256=" + hexsig, body, sec)
    assert not verify_github_signature(hexsig, body, sec)
    assert not verify_gitea_signature(hexsig, body, b"")


def test_issue_label_updated():
    ev = normalize({"X-Gitea-Event": "issues", "X-Gitea-Event-Type": "issue_label", "X-Gitea-Delivery": "d-1"},
                   {"action": "label_updated", "number": 7, "repository": REPO, "sender": ALICE,
                    "issue": {"number": 7, "title": "T", "body": "B", "labels": [{"id": 1, "name": "replicant"}],
                              "html_url": "http://git.replicant.localhost:3300/acme-payments/ledger-api/issues/7"}},
                   host=HOST)
    assert ev.kind == "issues.labeled" and ev.delivery_id == "d-1"
    assert ev.repo.repo_id == "3" and ev.repo.host == "git.replicant.localhost:3300" and ev.repo.owner == "acme-payments"
    assert ev.issue.labels == ("replicant",) and not ev.sender.is_bot and ev.sender.login == "alice"


def test_issue_opened_and_bot():
    ev = normalize({"X-Gitea-Event": "issues", "X-Gitea-Delivery": "d-2"},
                   {"action": "opened", "repository": REPO, "sender": BOT,
                    "issue": {"number": 8, "title": "T", "body": None, "labels": None}}, host=HOST)
    assert ev.kind == "issues.opened" and ev.sender.is_bot and ev.issue.labels == () and ev.issue.body == ""


def test_pull_request_closed_merged():
    ev = normalize({"X-Gitea-Event": "pull_request", "X-Gitea-Event-Type": "pull_request", "X-Gitea-Delivery": "d-3"},
                   {"action": "closed", "number": 2, "repository": REPO, "sender": ALICE,
                    "pull_request": {"number": 2, "merged": True, "state": "closed",
                                     "head": {"ref": "replicant/job_01m5p7x7r0bt69j0h0fbxepw90", "sha": "a" * 40},
                                     "base": {"ref": "main"}}}, host=HOST)
    assert ev.kind == "pull_request.closed" and ev.pull_request.merged and ev.pull_request.head_ref.startswith("replicant/")


def test_review_and_comment():
    ev = normalize({"X-Gitea-Event": "pull_request", "X-Gitea-Event-Type": "pull_request_review_approved",
                    "X-Gitea-Delivery": "d-4"},
                   {"action": "reviewed", "repository": REPO, "sender": ALICE, "review": {"content": "lgtm"},
                    "pull_request": {"number": 2, "state": "open", "head": {}, "base": {}}}, host=HOST)
    assert ev.kind == "pull_request_review.submitted" and ev.review_state == "approved"
    ev = normalize({"X-Gitea-Event": "issue_comment", "X-Gitea-Delivery": "d-5"},
                   {"action": "created", "repository": REPO, "sender": ALICE, "is_pull": True,
                    "issue": {"number": 2, "title": "x"}, "comment": {"id": 11, "body": "/replicant status", "user": ALICE}},
                   host=HOST)
    assert ev.kind == "issue_comment.created" and ev.issue.is_pull and ev.comment.id == 11


def test_gitea_review_and_pr_comment_headers():
    """Gitea 1.2x as observed: a submitted review is event `pull_request_comment` + type `pull_request_review_comment`;
    a plain PR comment is event `issue_comment` + type `pull_request_comment` (must not be read as a review)."""
    ev = normalize({"X-Gitea-Event": "pull_request_comment", "X-Gitea-Event-Type": "pull_request_review_comment",
                    "X-Gitea-Delivery": "d-6"},
                   {"action": "reviewed", "repository": REPO, "sender": ALICE, "review": {"content": "use decimal"},
                    "pull_request": {"number": 2, "state": "open", "head": {"ref": "replicant/x"}, "base": {}}}, host=HOST)
    assert ev.kind == "pull_request_review.submitted" and ev.review_state == "commented"
    assert ev.comment.body == "use decimal" and ev.pull_request.number == 2
    ev = normalize({"X-Gitea-Event": "pull_request_rejected", "X-Gitea-Event-Type": "pull_request_review_rejected",
                    "X-Gitea-Delivery": "d-7"},
                   {"action": "reviewed", "repository": REPO, "sender": ALICE, "review": {"content": "no"},
                    "pull_request": {"number": 2, "state": "open", "head": {}, "base": {}}}, host=HOST)
    assert ev.review_state == "changes_requested"
    ev = normalize({"X-Gitea-Event": "issue_comment", "X-Gitea-Event-Type": "pull_request_comment",
                    "X-Gitea-Delivery": "d-8"},
                   {"action": "created", "repository": REPO, "sender": ALICE,
                    "issue": {"number": 2, "title": "x", "pull_request": {"merged": False}},
                    "comment": {"id": 12, "body": "please rename", "user": ALICE}}, host=HOST)
    assert ev.kind == "issue_comment.created" and ev.issue.is_pull and ev.comment.body == "please rename"


def _client(handler):
    import httpx

    from replicant_scm.gitea import GiteaClient
    return GiteaClient("http://gitea:3000", token="t", transport=httpx.MockTransport(handler))


def test_file_at_caps_rejects_binary_and_missing():
    import asyncio

    import httpx
    seen = []

    def handler(req):
        seen.append(req)
        path = req.url.path
        if path.endswith("/raw/big.txt"):
            return httpx.Response(200, content=b"a" * 100)
        if path.endswith("/raw/logo.png"):
            return httpx.Response(200, content=b"\x89PNG\x00\x00rest")
        if path.endswith("/raw/dir/CLAUDE.md"):
            return httpx.Response(200, content="héllo".encode())
        if path.endswith("/raw/bad"):
            return httpx.Response(422)
        return httpx.Response(404)

    async def go():
        async with _client(handler) as g:
            return (await g.file_at("o", "r", "a" * 40, "big.txt", max_bytes=10),
                    await g.file_at("o", "r", "a" * 40, "logo.png"),
                    await g.file_at("o", "r", "a" * 40, "dir/CLAUDE.md"),
                    await g.file_at("o", "r", "a" * 40, "missing"),
                    await g.file_at("o", "r", "a" * 40, "bad"))
    big, png, md, missing, bad = asyncio.run(go())
    assert big == "a" * 10 and png is None and md == "héllo" and missing is None and bad is None
    assert seen[0].url.params["ref"] == "a" * 40 and seen[0].headers["authorization"] == "token t"
    assert seen[2].url.path == "/api/v1/repos/o/r/raw/dir/CLAUDE.md"


def test_file_at_raises_on_server_error():
    import asyncio

    import httpx
    import pytest

    from replicant_scm.gitea import GiteaError

    async def go():
        async with _client(lambda req: httpx.Response(500, text="boom")) as g:
            await g.file_at("o", "r", "a" * 40, "x")
    with pytest.raises(GiteaError):
        asyncio.run(go())


def test_tree_pages_blobs_only_and_caps():
    import asyncio

    import httpx
    pages = []

    def handler(req):
        page = int(req.url.params["page"])
        pages.append(page)
        if req.url.path.endswith("/git/trees/" + "f" * 40):
            return httpx.Response(404)
        tree = [{"path": f"p{page}/f{i}", "type": "blob", "size": i, "sha": "s"} for i in range(3)]
        tree.append({"path": f"p{page}", "type": "tree"})
        return httpx.Response(200, json={"tree": tree, "truncated": page < 2})

    async def go():
        async with _client(handler) as g:
            return (await g.tree("o", "r", "a" * 40), await g.tree_entries("o", "r", "a" * 40, max_entries=2),
                    await g.tree("o", "r", "f" * 40))
    paths, capped, unknown = asyncio.run(go())
    assert paths == ["p1/f0", "p1/f1", "p1/f2", "p2/f0", "p2/f1", "p2/f2"]
    assert capped == [{"path": "p1/f0", "size": 0, "sha": "s"}, {"path": "p1/f1", "size": 1, "sha": "s"}]
    assert unknown == []
