import json
import os
import time

import httpx
import pytest

from github_token_broker.app import Policy, Settings, create_app, parse_ttl, validate_request

CALLERS = os.path.join(os.path.dirname(__file__), "broker-callers.yaml")


class FakeGitea:
    def __init__(self) -> None:
        self.tokens: dict[int, dict] = {}
        self.next = 1
        self.auth_seen: list[str] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.auth_seen.append(req.headers.get("authorization", "")[:6])
        if req.url.path == "/api/healthz":
            return httpx.Response(200, json={"status": "pass"})
        if req.url.path == "/api/v1/version":
            return httpx.Response(200, json={"version": "28.1.0"})
        if req.url.path == "/api/v1/users/replicant-bot/tokens" and req.method == "POST":
            body = json.loads(req.content)
            t = {"id": self.next, "name": body["name"], "sha1": f"tok{self.next:037d}", "scopes": body["scopes"]}
            self.tokens[self.next] = t
            self.next += 1
            return httpx.Response(201, json=t)
        if req.url.path == "/api/v1/users/replicant-bot/tokens" and req.method == "GET":
            return httpx.Response(200, json=[{"id": t["id"], "name": t["name"]} for t in self.tokens.values()])
        if req.url.path.startswith("/api/v1/users/replicant-bot/tokens/") and req.method == "DELETE":
            self.tokens.pop(int(req.url.path.rsplit("/", 1)[1]), None)
            return httpx.Response(204)
        return httpx.Response(404)


@pytest.fixture
def setup(tmp_path):
    (tmp_path / "pw").write_text("bot-password")
    (tmp_path / "callers").write_text("pubtok: publisher-worker\nmirtok: repo-mirror\ngitok: github-integration\n")
    s = Settings(callers_file=CALLERS, caller_tokens_file=str(tmp_path / "callers"), bot_password_file=str(tmp_path / "pw"),
                 gitea_base_url="http://gitea", ttl_s=3600)
    g = FakeGitea()
    os.environ["REPLICANT_BROKER_REAPER"] = "false"
    app = create_app(s, transport=httpx.MockTransport(g.handler))
    return app, g


async def _post(app, tok, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b") as c:
        return await c.post("/v1/installation-tokens", json=body, headers={"Authorization": f"Bearer {tok}"} if tok else {})


def test_policy_and_scopes():
    p = Policy.load(CALLERS)
    assert p.check("publisher-worker", {"contents": "write", "pull_requests": "write"}, 1) is None
    assert p.check("publisher-worker", {"issues": "write"}, 1) == "permission_not_allowed"
    assert p.check("repo-mirror", {"contents": "write"}, 1) == "permission_not_allowed"
    assert p.check("repo-mirror", {"contents": "read"}, 6) == "too_many_repos"
    assert p.check("nobody", {"contents": "read"}, 1) == "caller_not_allowed"
    assert p.gitea_scopes({"contents": "write", "pull_requests": "write"}) == ["write:issue", "write:repository"]
    assert p.gitea_scopes({"contents": "read", "metadata": "read"}) == ["read:organization", "read:repository"]
    assert parse_ttl("3600s") == 3600 and parse_ttl("2h") == 7200


def test_validate_request():
    ok = {"installation_id": 1, "repository_ids": [3], "permissions": {"contents": "read"}, "purpose": "mirror.fetch"}
    assert validate_request(ok) is None
    assert validate_request({**ok, "permissions": {"workflows": "write"}}) == "permission_not_allowed"
    assert validate_request({**ok, "permissions": {"metadata": "write"}}) == "permission_not_allowed"
    assert validate_request({**ok, "purpose": "X"}) == "invalid"
    assert validate_request({**ok, "repository_ids": ["3"]}) == "invalid"


async def test_mint_cache_and_refusals(setup):
    app, g = setup
    body = {"installation_id": 1, "repository_ids": [3], "permissions": {"contents": "write", "pull_requests": "write"},
            "purpose": "publisher.publish", "job_id": "job_01m5p7x7r0bt69j0h0fbxepw90"}
    r = await _post(app, "pubtok", body)
    assert r.status_code == 200, r.text
    tok = r.json()
    assert tok["token"].startswith("tok") and tok["cached"] is False and tok["repository_ids"] == [3]
    assert g.tokens[1]["scopes"] == ["write:issue", "write:repository"] and g.tokens[1]["name"].startswith("replicant-broker-")
    r2 = await _post(app, "pubtok", body)
    assert r2.json()["cached"] is True and r2.json()["token"] == tok["token"] and len(g.tokens) == 1
    assert (await _post(app, "mirtok", body)).status_code == 403
    assert (await _post(app, "badtok", body)).status_code == 401
    assert (await _post(app, None, body)).status_code == 401
    r = await _post(app, "mirtok", {**body, "permissions": {"contents": "read", "metadata": "read"},
                                     "purpose": "mirror.fetch"})
    assert r.status_code == 200 and len(g.tokens) == 2


async def test_reaper_deletes_expired(setup):
    app, g = setup
    broker = app.state.broker
    g.tokens[90] = {"id": 90, "name": f"replicant-broker-{int(time.time()) - 7200}-abcd", "sha1": "x"}
    g.tokens[91] = {"id": 91, "name": f"replicant-broker-{int(time.time())}-abcd", "sha1": "y"}
    g.tokens[92] = {"id": 92, "name": "someone-else", "sha1": "z"}
    assert await broker.reap() == 1
    assert set(g.tokens) == {91, 92}
