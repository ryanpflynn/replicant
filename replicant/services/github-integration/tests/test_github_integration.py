"""Unit tests plus DB-backed tests (CTL_GI_DB_DSN, tests/smoke/env.sh of control-plane-api) with contract fakes for
control-plane-api, github-token-broker, Gitea and Temporal."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

import httpx
import pytest

from replicant_ids import ulid
from replicant_scm.gitea import normalize
from github_integration.consumer import build_job_request, dev_principal

SCHEMA = Path(__file__).resolve().parents[2] / "control-plane-api/src/control_plane_api/schemas/job-request.schema.json"
DSN = os.environ.get("CTL_GI_DB_DSN")


def repo(rid: int = 3) -> dict:
    return {"id": rid, "name": "ledger-api", "full_name": "acme-payments/ledger-api", "default_branch": "main",
            "owner": {"id": 2, "login": "acme-payments", "username": "acme-payments"},
            "html_url": "http://git.replicant.localhost:3300/acme-payments/ledger-api"}


GITEA_IDS = {"alice": 5, "mallory": 6, "replicant-bot": 7}  # alice is linked (identity.links), mallory is not
ALICE_PID = "usr_01knk8dfr0fvjc7v0b9vhvkzbp"


async def _link_alice() -> None:
    """Who may trigger comes from identity.links (kind gitea): link alice's Gitea id, as `replicant dev seed` does."""
    su = os.environ.get("CTL_SU_DSN")
    if not su:
        pytest.skip("CTL_SU_DSN not set (needed to seed the identity link)")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(su)
    async with eng.begin() as conn:
        await conn.execute(text(
            "INSERT INTO identity.users (principal_id, okta_sub, user_name, display_name, status) VALUES "
            "(:p, 'dev|gi-test-alice', 'gi-test-alice@acme.test', 'Alice', 'active') ON CONFLICT DO NOTHING"), {"p": ALICE_PID})
        await conn.execute(text(
            "INSERT INTO identity.links (link_id, principal_id, kind, namespace, external_id, login, method, status, "
            "verified_at) SELECT 'lnk_gitestalice', :p, 'gitea', 'git.replicant.localhost:3300', :e, 'alice', 'dev_seed', "
            "'active', now() WHERE NOT EXISTS (SELECT 1 FROM identity.links WHERE kind = 'gitea' AND external_id = :e "
            "AND status = 'active')"), {"p": ALICE_PID, "e": str(GITEA_IDS["alice"])})
    await eng.dispose()


def issue_payload(number: int, rid: int = 3, sender: str = "alice", action: str = "label_updated") -> dict:
    return {"action": action, "number": number, "repository": repo(rid),
            "sender": {"id": GITEA_IDS.get(sender, 99), "login": sender, "username": sender},
            "issue": {"number": number, "title": "Refund ledger entries double-count FX fees", "body": "Repro ...",
                      "labels": [{"id": 1, "name": "replicant"}],
                      "html_url": f"http://git.replicant.localhost:3300/acme-payments/ledger-api/issues/{number}"}}


def test_job_request_from_gitea_issue_validates_against_c_jobreq():
    from jsonschema import Draft202012Validator
    v = Draft202012Validator(json.loads(SCHEMA.read_text()))
    ev = normalize({"X-Gitea-Event": "issues", "X-Gitea-Event-Type": "issue_label", "X-Gitea-Delivery": "6f1b4a40-d"},
                   issue_payload(7), host="http://git.replicant.localhost:3300")
    jr = build_job_request(ev, tenant="payments", installation_id=1, label="replicant", default_branch="main",
                           received_at=dt.datetime.now(dt.UTC))
    assert list(v.iter_errors(jr)) == []
    assert jr["dedupe_key"] == "gitea:issue-label:3:7:replicant"
    assert jr["requester"]["principal_id"] == dev_principal("alice")
    assert jr["targets"][0]["repo"] == {"provider": "gitea", "host": "git.replicant.localhost:3300",
                                        "owner": "acme-payments", "name": "ledger-api", "repo_id": "3"}
    assert jr["source"]["channel"] == "github" and jr["source"]["github"] == {"installation_id": 1,
                                                                              "app_registration": "dev"}


# ------------------------------------------------------------------------------------------------- DB-backed
pytestmark_db = pytest.mark.skipif(not DSN, reason="CTL_GI_DB_DSN not set")


class Fakes:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.comments: list[tuple] = []
        self.edits: list[tuple] = []
        self.tokens: list[dict] = []
        self.signals: list[tuple] = []
        self.dedupe: dict[str, str] = {}

    def cp(self, req: httpx.Request) -> httpx.Response:
        jr = json.loads(req.content)
        self.jobs.append({"jr": jr, "auth": req.headers.get("authorization")})
        if jr["dedupe_key"] in self.dedupe:
            return httpx.Response(200, json={"job_id": self.dedupe[jr["dedupe_key"]], "status": "queued", "created": False,
                                             "links": {"ui": "x"}})
        job = ulid("job")
        self.dedupe[jr["dedupe_key"]] = job
        return httpx.Response(201, json={"job_id": job, "status": "queued", "created": True, "start_status": "started",
                                         "links": {"ui": "x"}})

    def broker(self, req: httpx.Request) -> httpx.Response:
        self.tokens.append(json.loads(req.content))
        return httpx.Response(200, json={"token": "t" * 40, "expires_at": "2026-10-24T09:00:00Z", "permissions": {},
                                         "repository_ids": [3], "cached": False})

    def gitea(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/comments"):
            self.comments.append((req.url.path, json.loads(req.content)["body"], req.headers.get("authorization")))
            return httpx.Response(201, json={"id": 100 + len(self.comments)})
        if req.method == "PATCH":
            self.edits.append((req.url.path, json.loads(req.content)["body"]))
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(404)


class FakeTemporal:
    def __init__(self, f: Fakes) -> None:
        self.f = f

    async def signal(self, wf: str, name: str, arg: dict) -> None:
        self.f.signals.append((wf, name, arg))


@pytest.fixture
async def gi(tmp_path):
    from github_integration.app import create_app
    from github_integration.clients import Broker, ControlPlane
    from github_integration.config import Settings
    hook, seed, cpt = secrets.token_urlsafe(16), secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    for n, v in (("hook", hook), ("seed", seed), ("cpt", cpt), ("bt", "brokertok")):
        (tmp_path / n).write_text(v)
    s = Settings(db_dsn=DSN, db_password_file=os.environ.get("CTL_GI_DB_PASSWORD_FILE"),
                 gitea_webhook_secret_file=str(tmp_path / "hook"), dev_seed_token_file=str(tmp_path / "seed"),
                 control_plane_token_file=str(tmp_path / "cpt"), token_broker_token_file=str(tmp_path / "bt"),
                 control_plane_url="http://cpa", token_broker_url="http://broker")
    await _link_alice()
    f = Fakes()
    app = create_app(s, cp=ControlPlane(s, transport=httpx.MockTransport(f.cp)),
                     broker=Broker(s, transport=httpx.MockTransport(f.broker)), temporal=FakeTemporal(f),
                     gitea_transport=httpx.MockTransport(f.gitea), run_consumer=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gi") as c:
        org = f"acme-{secrets.token_hex(3)}"
        r = await c.post("/v1/admin/scm-installations", headers={"Authorization": f"Bearer {seed}"},
                         json={"provider": "gitea", "host": "git.replicant.localhost:3300", "org": org, "tenant_id": "payments"})
        assert r.status_code in (200, 201), r.text
        yield c, f, app, hook, org, r.json()["installation_id"], cpt
    await app.state.engine.dispose()


def signed(hook: str, payload: dict, event: str, etype: str, guid: str) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    return body, {"X-Gitea-Event": event, "X-Gitea-Event-Type": etype, "X-Gitea-Delivery": guid,
                  "X-Gitea-Signature": hmac.new(hook.encode(), body, hashlib.sha256).hexdigest(),
                  "Content-Type": "application/json"}


def org_payload(p: dict, org: str, rid: int) -> dict:
    p["repository"]["owner"]["login"] = p["repository"]["owner"]["username"] = org
    p["repository"]["full_name"] = f"{org}/ledger-api"
    p["repository"]["id"] = rid
    return p


@pytestmark_db
async def test_webhook_signature_and_dedupe(gi):
    c, f, app, hook, org, inst, _ = gi
    body, h = signed(hook, {"x": 1}, "issues", "issues", "guid-" + secrets.token_hex(4))
    assert (await c.post("/webhooks/gitea", content=body, headers={**h, "X-Gitea-Signature": "0" * 64})).status_code == 401
    assert (await c.post("/webhooks/gitea", content=body, headers={k: v for k, v in h.items()
                                                                   if k != "X-Gitea-Signature"})).status_code == 401
    assert (await c.post("/webhooks/gitea", content=body, headers=h)).status_code == 202
    assert (await c.post("/webhooks/gitea", content=body, headers=h)).status_code == 202   # duplicate GUID


@pytestmark_db
async def test_labelled_issue_becomes_job_and_ack(gi):
    c, f, app, hook, org, inst, cpt = gi
    rid = int.from_bytes(secrets.token_bytes(3), "big")
    guid = "guid-" + secrets.token_hex(6)
    body, h = signed(hook, org_payload(issue_payload(7, rid), org, rid), "issues", "issue_label", guid)
    assert (await c.post("/webhooks/gitea", content=body, headers=h)).status_code == 202
    assert await app.state.consumer.run_once() >= 1
    jr = next(j for j in f.jobs if j["jr"]["source"]["event_id"] == guid)
    assert jr["auth"] == f"Bearer {cpt}" and jr["jr"]["source"]["github"]["installation_id"] == inst
    assert jr["jr"]["dedupe_key"] == f"gitea:issue-label:{rid}:7:replicant"
    assert f.comments and "Replicant job started" in f.comments[-1][1] and f.comments[-1][2] == "token " + "t" * 40
    assert f.tokens[-1]["permissions"] == {"issues": "write", "pull_requests": "write"}
    # a second delivery for the same issue (e.g. re-label) dedupes at intake (200) and posts no second comment
    body2, h2 = signed(hook, org_payload(issue_payload(7, rid, action="opened"), org, rid), "issues", "issues",
                       "guid-" + secrets.token_hex(6))
    await c.post("/webhooks/gitea", content=body2, headers=h2)
    n_comments = len(f.comments)
    await app.state.consumer.run_once()
    assert len(f.comments) == n_comments
    assert [j["jr"]["dedupe_key"] for j in f.jobs[-2:]] == [f"gitea:issue-label:{rid}:7:replicant"] * 2


@pytestmark_db
async def test_bot_unlinked_and_unlabelled_are_not_jobs(gi):
    c, f, app, hook, org, inst, _ = gi
    rid = int.from_bytes(secrets.token_bytes(3), "big")
    n = len(f.jobs)
    for sender, labels in (("replicant-bot", True), ("mallory", True), ("alice", False)):
        p = org_payload(issue_payload(9, rid, sender=sender), org, rid)
        if not labels:
            p["issue"]["labels"] = []
        body, h = signed(hook, p, "issues", "issue_label", "guid-" + secrets.token_hex(6))
        await c.post("/webhooks/gitea", content=body, headers=h)
    await app.state.consumer.run_once()
    assert len(f.jobs) == n
    assert any("link your account" in cm[1] for cm in f.comments)   # mallory got the one-time reply


@pytestmark_db
async def test_pr_closed_merged_signals_pr_event(gi):
    c, f, app, hook, org, inst, _ = gi
    rid = int.from_bytes(secrets.token_bytes(3), "big")
    job = ulid("job")
    p = {"action": "closed", "number": 4, "repository": repo(rid), "sender": {"id": 5, "login": "alice"},
         "pull_request": {"number": 4, "merged": True, "state": "closed", "head": {"ref": f"replicant/{job}",
                                                                                   "sha": "a" * 40}, "base": {"ref": "main"}}}
    body, h = signed(hook, org_payload(p, org, rid), "pull_request", "pull_request", "guid-" + secrets.token_hex(6))
    await c.post("/webhooks/gitea", content=body, headers=h)
    await app.state.consumer.run_once()
    wf, name, sig = f.signals[-1]
    assert (wf, name) == (job, "pr.event") and sig["event"] == "pull_request_merged" and sig["pr_number"] == 4
    assert sig["repo"] == f"git.replicant.localhost:3300/{org}/ledger-api" and sig["actor"]["linked"] is True
    assert sig["payload_uri"].startswith(f"s3://local-replicant-local-artifacts/tenants/payments/jobs/{job}/events/pr/")
    from jsonschema import Draft202012Validator
    wm = json.loads((SCHEMA.parent / "workflow-messages.schema.json").read_text())
    v = Draft202012Validator({"$defs": wm["$defs"], "$ref": "#/$defs/PrEventSignal"})
    assert list(v.iter_errors(sig)) == []
