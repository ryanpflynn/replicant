"""Integration tests against a migrated replicant Postgres (tests/smoke/env.sh up) with a fake Temporal gateway.

    CTL_DB_DSN=postgresql+psycopg://svc_control_plane_api@127.0.0.1:55432/replicant \
    CTL_DB_PASSWORD_FILE=$CTL_HOME/secrets/db_svc_control_plane_api pytest tests/test_db_integration.py
"""
from __future__ import annotations

import os
import secrets

import httpx
import pytest
from sqlalchemy import text

from control_plane_api.app import create_app
from control_plane_api.config import Settings
from control_plane_api.ids import dev_user_principal, ulid
from control_plane_api.temporal_client import UpdateOutcome

DSN = os.environ.get("CTL_DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="CTL_DB_DSN not set (start tests/smoke/env.sh)")


class FakeTemporal:
    def __init__(self) -> None:
        self.starts: list[dict] = []
        self.updates: list[tuple] = []
        self.fail_start = False
        self.update_outcome: UpdateOutcome | None = None

    async def reachable(self) -> bool:
        return True

    async def start_job(self, **kw) -> str:
        if self.fail_start:
            raise RuntimeError("temporal down")
        self.starts.append(kw)
        return "run-1"

    async def execute_update(self, wf, name, arg, uid, timeout_s=10.0):
        self.updates.append((wf, name, arg, uid))
        if self.update_outcome is not None:
            return self.update_outcome
        return UpdateOutcome("result", result={"schema": "replicant.msg.gate-decide-result/v1", "outcome": "accepted",
                                               "gate_id": arg["gate_id"], "decision_id": arg["decision_id"]})


@pytest.fixture
async def env(tmp_path):
    seed = secrets.token_urlsafe(16)
    shim = secrets.token_urlsafe(16)
    (tmp_path / "seed").write_text(seed)
    (tmp_path / "callers").write_text(f"{shim} svc_replicant-gateway-shim\n")
    s = Settings(replicant_env="local", db_dsn=DSN, db_password_file=os.environ.get("CTL_DB_PASSWORD_FILE"),
                 dev_seed_token_file=str(tmp_path / "seed"), internal_caller_tokens_file=str(tmp_path / "callers"),
                 background_tasks=False, worker_build_id="b20261024-000000000000")
    fake = FakeTemporal()
    app = create_app(s, temporal=fake)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cpa") as c:
        seed_h = {"Authorization": f"Bearer {seed}", "Idempotency-Key": ulid("x")}
        r = await c.put("/v1/admin/platform-caps", headers=seed_h, json={"tenant_daily_usd_default": 10,
                                                                          "tenant_monthly_usd_default": 100,
                                                                          "platform_daily_usd_max": 50,
                                                                          "platform_monthly_usd_max": 500})
        assert r.status_code == 200, r.text
        r = await c.post("/v1/admin/tenants", headers=seed_h, json={"tenant_id": "payments", "display_name": "payments"})
        assert r.status_code in (200, 201), r.text
        r = await c.post("/v1/admin/dev/release-bundles", headers=seed_h, json={"bootstrap": True})
        assert r.status_code == 200, r.text
        yield c, fake, seed_h, shim, app
    await app.state.db.dispose()


def job_request(login: str = "alice", tenant: str = "payments", **over) -> dict:
    jr = {"schema_version": "replicant.job-request/v1", "request_id": ulid("jrq"),
          "dedupe_key": f"ui:form:{dev_user_principal(login)}:{ulid('x')[2:]}", "tenant_id": tenant,
          "source": {"channel": "ui", "event_type": "ui.form", "event_id": ulid("x")},
          "requester": {"kind": "user", "principal_id": dev_user_principal(login)},
          "targets": [{"repo": {"provider": "gitea", "host": "git.replicant.localhost:3300", "owner": "acme-payments",
                                "name": "ledger-api", "repo_id": "3"}, "base_ref": "main"}],
          "intent": {"title": "t", "text": "b", "format": "markdown", "untrusted": True},
          "workflow_type": "auto", "priority": "normal", "received_at": "2026-10-24T08:00:00Z"}
    jr.update(over)
    return jr


async def test_intake_creates_row_and_starts_pinned(env):
    c, fake, *_ = env
    jr = job_request()
    r = await c.post("/v1/jobs", json=jr, headers={"X-Replicant-Dev-User": "alice"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] and body["start_status"] == "started" and body["release_bundle_id"].startswith("rb_")
    st = fake.starts[-1]
    assert st["job_id"] == body["job_id"] and st["build_id"] == "b20261024-000000000000"
    assert st["workflow_type"] == "ReplicantJob" and st["job_request"]["dedupe_key"] == jr["dedupe_key"]
    r2 = await c.post("/v1/jobs", json=jr, headers={"X-Replicant-Dev-User": "alice"})
    assert r2.status_code == 200 and r2.json()["job_id"] == body["job_id"] and not r2.json()["created"]
    db = c._transport.app.state.db  # type: ignore[attr-defined]
    async with db.tx(tenant="payments") as conn:
        row = (await conn.execute(text("SELECT start_status, assignment_reason, release_cohort, submitted_by FROM "
                                       "intake.job_requests WHERE job_id = :j"), {"j": body["job_id"]})).first()
    assert tuple(row) == ("started", "default", "default", dev_user_principal("alice"))


async def test_intake_refusals(env):
    c, *_ = env
    r = await c.post("/v1/jobs", json=job_request(), headers={"X-Replicant-Dev-User": "bob"})
    assert r.status_code == 403 and r.json()["code"] == "requester_mismatch"
    r = await c.post("/v1/jobs", json=job_request(login="dave"), headers={"X-Replicant-Dev-User": "dave"})
    assert r.status_code == 403 and r.json()["code"] == "missing_permission"
    r = await c.post("/v1/jobs", json=job_request(tenant="nosuchtenant"), headers={"X-Replicant-Dev-User": "grace"})
    assert r.status_code in (403, 422)
    r = await c.post("/v1/jobs", json=job_request(priority="asap"), headers={"X-Replicant-Dev-User": "alice"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_job_request"
    r = await c.post("/v1/jobs", json=job_request(requested_release_bundle_id="rb_" + "0" * 26),
                     headers={"X-Replicant-Dev-User": "alice"})
    assert r.status_code == 403 and r.json()["code"] == "release_pin_required"
    r = await c.post("/v1/jobs", json=job_request())
    assert r.status_code == 401


async def test_start_failure_leaves_pending_and_retrier_starts(env):
    c, fake, *_ = env
    app = c._transport.app  # type: ignore[attr-defined]
    fake.fail_start = True
    r = await c.post("/v1/jobs", json=job_request(), headers={"X-Replicant-Dev-User": "alice"})
    assert r.status_code == 201 and r.json()["start_status"] == "pending"
    fake.fail_start = False
    assert await app.state.intake.retry_pending() >= 1
    assert any(s["job_id"] == r.json()["job_id"] for s in fake.starts)


async def test_service_principal_channel_rule(env, tmp_path):
    c, *_ = env
    app = c._transport.app  # type: ignore[attr-defined]
    from control_plane_api.auth import service_principal
    tok = secrets.token_urlsafe(8)
    p = service_principal("svc_github-integration")
    app.state.auth._internal[tok] = p
    app.state.auth.directory.add(p)
    jr = job_request(source={"channel": "github", "event_type": "issues.labeled", "event_id": ulid("x"),
                             "github": {"installation_id": 1, "app_registration": "dev"}})
    r = await c.post("/v1/jobs", json=jr, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 201, r.text
    r = await c.post("/v1/jobs", json=job_request(), headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403 and r.json()["code"] == "service_channel_not_allowed"


async def test_gateway_quota(env):
    c, _, _, shim, _ = env
    r = await c.get("/internal/v1/tenants/payments/gateway-quota", headers={"Authorization": f"Bearer {shim}"})
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["daily_budget_usd"] >= 0 and q["platform_daily_usd_max"] == 50 and q["tenant_id"] == "payments"
    assert (await c.get("/internal/v1/tenants/nosuch/gateway-quota", headers={"Authorization": f"Bearer {shim}"})
            ).status_code == 404
    assert (await c.get("/internal/v1/tenants/payments/gateway-quota", headers={"X-Replicant-Dev-User": "grace"})
            ).status_code == 403


async def test_gate_decision_outbox(env):
    c, fake, *_ = env
    app = c._transport.app  # type: ignore[attr-defined]
    r = await c.post("/v1/jobs", json=job_request(), headers={"X-Replicant-Dev-User": "alice"})
    job = r.json()["job_id"]
    gate = "gate_" + ulid("x")[2:]
    digest = "sha256:" + "c" * 64
    # stand-in for replicant-projector: the API login cannot write approvals, so use a superuser DSN
    su = os.environ.get("CTL_SU_DSN")
    if not su:
        pytest.skip("CTL_SU_DSN not set")
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(su)
    async with eng.begin() as conn:
        await conn.execute(text(
            "INSERT INTO approvals.gates (gate_id, job_id, tenant_id, seq, kind, state, requester, opened_at, "
            "packet_revision, presented_digest, packet_key, channels) VALUES (:g, :j, 'payments', 1, 'publish_approval', "
            "'open', :req, now(), 1, :d, 'k', '{ui,cli}')"), {"g": gate, "j": job, "req": dev_user_principal("alice"),
                                                           "d": digest})
    await eng.dispose()
    h = {"X-Replicant-Dev-User": "grace", "Idempotency-Key": ulid("k")[2:]}
    r = await c.post(f"/v1/gates/{gate}/decisions", headers=h, json={"decision": "approve", "presented_digest": digest})
    assert r.status_code == 200 and r.json()["outcome"] == "accepted", r.text
    wf, name, arg, uid = fake.updates[-1]
    assert (wf, name) == (job, "gate.decide") and uid == arg["decision_id"] and arg["channel"] == "ui"
    fake.update_outcome = UpdateOutcome("unavailable", error_type="timeout")
    h2 = {"X-Replicant-Dev-User": "grace", "Idempotency-Key": ulid("k")[2:]}
    r = await c.post(f"/v1/gates/{gate}/decisions", headers=h2, json={"decision": "approve", "presented_digest": digest})
    assert r.status_code == 202 and r.json()["state"] == "recording"
    fake.update_outcome = None
    r = await c.post(f"/v1/gates/{gate}/decisions", headers=h2, json={"decision": "approve", "presented_digest": digest})
    assert r.status_code == 200
    # F1: an idempotency key reused for a different action must not return the first action's outcome.
    from control_plane_api.problems import ProblemError
    ref = fake.updates[-1][3]
    with pytest.raises(ProblemError) as ei:
        await app.state.outbox.submit(action_ref=ref, kind="job.cancel", workflow_id=job, tenant_id="payments",
                                      actor=dev_user_principal("grace"), payload={})
    assert ei.value.status == 409 and ei.value.code == "idempotency_key_reused"
    # The same key from a different principal on this gate derives a different decision_id.
    h3 = {"X-Replicant-Dev-User": "bob", "Idempotency-Key": h2["Idempotency-Key"]}
    r = await c.post(f"/v1/gates/{gate}/decisions", headers=h3, json={"decision": "approve", "presented_digest": digest})
    assert fake.updates[-1][3] != ref, r.text


AGENT_YAML = """apiVersion: replicant.example.com/v1
kind: AgentDefinition
metadata: {{name: {name}, scope: {{level: global}}, owners: [replicant-platform]}}
spec: {{cli: claude-code, cliImage: {{image: localhost/replicant/cli-claude-code:dev, version: 2.1.310}},
       model: {{primary: anthropic/claude-sonnet-5-5}}, note: "{note}"}}
"""


async def test_registry_publish_and_build(env):
    c, _, seed_h, *_ = env
    import hashlib
    name = "ctl-test-" + ulid("x")[-6:]
    docs = [{"path": f"agents/{name}.yaml", "content_yaml": AGENT_YAML.format(name=name, note="v1")}]
    pid = lambda s: "pub_" + hashlib.sha256(s.encode()).hexdigest()[:26].translate(str.maketrans("ilou", "0123"))  # noqa: E731
    body = {"publish_id": pid(name + "1"), "commit_sha": "a" * 40, "source": "config_repo", "dry_run": True,
            "documents": docs}
    r = await c.post("/v1/registry/publish", json=body, headers=seed_h)
    assert r.status_code == 200 and r.json()["items"][0]["ref"] == f"agent/{name}@1", r.text
    r = await c.post("/v1/registry/publish", json={**body, "dry_run": False}, headers=seed_h)
    assert r.status_code == 200 and r.json()["items"][0]["created"] is True
    item = r.json()["items"][0]
    r = await c.post("/v1/registry/publish", json={**body, "dry_run": False, "publish_id": pid(name + "2")},
                     headers=seed_h)
    assert r.json()["items"][0] == {**item, "created": False}            # same content -> same version, no new row
    docs2 = [{"path": f"agents/{name}.yaml", "content_yaml": AGENT_YAML.format(name=name, note="v2")}]
    r = await c.post("/v1/registry/publish", json={**body, "dry_run": False, "publish_id": pid(name + "3"),
                                                   "documents": docs2}, headers=seed_h)
    assert r.json()["items"][0]["ref"] == f"agent/{name}@2"
    bad = [{"path": "x.yaml", "content_yaml": "apiVersion: replicant.example.com/v1\nkind: Nope\n"}]
    r = await c.post("/v1/registry/publish", json={**body, "publish_id": pid(name + "4"), "documents": bad},
                     headers=seed_h)
    assert r.status_code == 422 and r.json()["code"] == "publish_rejected"
    r = await c.post("/v1/registry/bundles:build", headers=seed_h,
                     json={"request": {"kind": "ReleaseBundleRequest", "spec": {"mode": "overlay", "overlay": {}}}})
    assert r.status_code == 422


async def test_bundle_registry_objects_follow_cdef(env):
    """Writer side of the shared replicant_cdef vectors: what a reader (runner-local, replicant-worker) dereferences."""
    from replicant_cdef import read_bundle_spec
    from control_plane_api import registry
    c, *_ = env
    app = c._transport.app  # type: ignore[attr-defined]
    store: dict[str, bytes] = {}

    async def w(key: str, body: bytes) -> None:
        store[key] = body
    spec = registry.default_dev_spec("b20261024-abcdefabcdef", "sha256:" + "0" * 64)
    doc = registry.bundle_document(spec, "svc_dev-seeder")
    async with app.state.db.tx(role="svc_cpa_registry") as conn:
        rb, digest, _ = await registry.register_bundle(conn, app.state.db, doc, "svc_dev-seeder", w)
    assert read_bundle_spec(store[f"bundles/{rb}.json"], store.__getitem__) == (rb, spec)
