"""Integration tests against a migrated replicant Postgres (tests/smoke/env.sh up) with a fake Temporal gateway.

    CTL_DB_DSN=postgresql+psycopg://svc_control_plane_api@127.0.0.1:55432/replicant \
    CTL_DB_PASSWORD_FILE=$CTL_HOME/secrets/db_svc_control_plane_api pytest tests/test_db_integration.py
"""
from __future__ import annotations

import os
import secrets
from typing import Any

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
    from control_plane_api import registry
    from replicant_cdef import read_bundle_spec
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


# ------------------------------------------------------------------------------------------- WEB-UIPREF
async def test_ui_preferences_versioned_per_user(env):
    c, *_ = env
    login = "erin"
    db = c._transport.app.state.db  # type: ignore[attr-defined]
    async with db.tx(role="svc_cpa_notify") as conn:          # fresh state for the test user
        await conn.execute(text("DELETE FROM web.ui_preferences WHERE principal_id = :p"),
                           {"p": dev_user_principal(login)})
    h = {"X-Replicant-Dev-User": login}
    r = await c.get("/v1/me", headers=h)
    assert r.status_code == 200 and r.json()["ui_preferences"] == {"job_view": "simple"}
    r = await c.get("/v1/me/ui-preferences", headers=h)
    assert r.json() == {"job_view": "simple", "version": 0} and r.headers["etag"] == '"0"'
    r = await c.put("/v1/me/ui-preferences", headers={**h, "If-Match": '"0"'}, json={"job_view": "detailed"})
    assert r.status_code == 200 and r.json() == {"job_view": "detailed", "version": 1}, r.text
    # a stale version (body or If-Match) is refused, not silently applied
    r = await c.put("/v1/me/ui-preferences", headers=h, json={"job_view": "simple", "version": 0})
    assert r.status_code == 412 and r.json()["code"] == "version_mismatch" and r.headers["etag"] == '"1"'
    r = await c.put("/v1/me/ui-preferences", headers={**h, "If-Match": '"7"'}, json={"job_view": "simple"})
    assert r.status_code == 412
    assert (await c.get("/v1/me", headers=h)).json()["ui_preferences"] == {"job_view": "detailed"}
    r = await c.put("/v1/me/ui-preferences", headers=h, json={"job_view": "simple", "version": 1})
    assert r.status_code == 200 and r.json()["version"] == 2
    r = await c.put("/v1/me/ui-preferences", headers=h, json={"job_view": "detailed"})   # no version: upsert
    assert r.status_code == 200 and r.json()["version"] == 3
    r = await c.put("/v1/me/ui-preferences", headers=h, json={"job_view": "nope"})
    assert r.status_code == 422
    # per user: another user is unaffected
    other = {"X-Replicant-Dev-User": "alice"}
    async with db.tx(role="svc_cpa_notify") as conn:
        await conn.execute(text("DELETE FROM web.ui_preferences WHERE principal_id = :p"),
                           {"p": dev_user_principal("alice")})
    assert (await c.get("/v1/me", headers=other)).json()["ui_preferences"] == {"job_view": "simple"}


# ------------------------------------------------------------------------------------------- CONFIG-01
async def test_project_config_versions_if_match_rbac_and_rollback(env):
    c, fake, seed_h, *_ = env
    pid, repo = f"prj_t{secrets.token_hex(4)}", str(900000 + secrets.randbelow(99999))
    r = await c.post("/v1/tenants/payments/projects", headers=seed_h, json={
        "project_id": pid, "name": "Test", "repos": [{"repo_id": repo, "full_name": "acme-payments/t"}]})
    assert r.status_code == 201 and r.json()["repos"][0]["repo_id"] == repo, r.text
    r = await c.post("/v1/tenants/payments/projects", headers=seed_h, json={
        "project_id": f"{pid}b", "name": "Other", "repos": [{"repo_id": repo, "full_name": "acme-payments/t"}]})
    assert r.status_code == 409 and r.json()["code"] == "repo_in_other_project"
    erin, dave, alice = ({"X-Replicant-Dev-User": u} for u in ("erin", "dave", "alice"))
    doc = (await c.get(f"/v1/projects/{pid}/config", headers=dave)).json()
    assert doc["version"] == 0 and doc["can_edit"] == {"live": False, "pinned": False}
    assert (await c.get(f"/v1/projects/{pid}/config", headers=erin)).json()["can_edit"] == {"live": True, "pinned": True}
    v1 = {"gates": {"plan": "always"}, "risk": {"large_change_files": 0}}
    r = await c.put(f"/v1/projects/{pid}/config", headers=erin, json={"config": v1, "reason": "r"})
    assert r.status_code == 428
    r = await c.put(f"/v1/projects/{pid}/config", headers={**erin, "If-Match": "0"}, json={"config": v1, "reason": "r1"})
    assert r.status_code == 200 and r.json()["written_version"] == 1, r.text
    assert {ch["applies"] for ch in r.json()["changes"]} == {"new_jobs"}
    r = await c.put(f"/v1/projects/{pid}/config", headers={**erin, "If-Match": "0"}, json={"config": {}, "reason": "x"})
    assert r.status_code == 412 and r.json()["current_version"] == 1
    r = await c.put(f"/v1/projects/{pid}/config", headers={**dave, "If-Match": "1"}, json={"config": {}, "reason": "x"})
    assert r.status_code == 403
    r = await c.put(f"/v1/projects/{pid}/config", headers={**erin, "If-Match": "1"},
                    json={"config": {"gates": {"publish": "off"}}, "reason": "x"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_config"
    r = await c.put(f"/v1/projects/{pid}/config", headers={**erin, "If-Match": "1"}, json={"config": {}, "reason": "r2"})
    assert r.json()["written_version"] == 2 and r.json()["effective"]["gates"]["plan"] == "off"
    r = await c.post(f"/v1/projects/{pid}/config:rollback", headers=erin, json={"version": 1, "reason": "back"})
    assert r.status_code == 200 and r.json()["written_version"] == 3 and r.json()["config"] == v1
    hist = r.json()["history"]
    assert [h["version"] for h in hist] == [3, 2, 1] and hist[0]["rolled_back_from"] == 1 and hist[0]["is_current"]
    r = await c.post(f"/v1/projects/{pid}/config:validate", headers=alice, json={"config": {"triggers": {"label": "go"}}})
    assert r.status_code == 200 and {ch["path"]: ch["applies"] for ch in r.json()["changes"]}["triggers.label"] == "live"
    # live: min_trigger_role is enforced at intake for the project's repo (alice is an engineer)
    r = await c.put(f"/v1/projects/{pid}/config", headers={**erin, "If-Match": "3"},
                    json={"config": {"triggers": {"min_trigger_role": "approver"}}, "reason": "approvers only"})
    assert r.status_code == 200
    jr = job_request("alice")
    jr["targets"][0]["repo"]["repo_id"] = repo
    r = await c.post("/v1/jobs", headers={"X-Replicant-Dev-User": "alice", "Idempotency-Key": ulid("x")}, json=jr)
    assert r.status_code == 403 and "higher role" in r.json().get("detail", ""), r.text
    assert (await c.get(f"/v1/projects/{pid}", headers=dave)).status_code == 200
    assert (await c.get("/v1/projects/prj_nope", headers=erin)).status_code == 404


async def _reset_bindings(su: str, baseline: bool) -> None:
    """Role bindings switch off the static dev tenant roles once a version exists: start from a baseline that keeps
    erin tenant admin, and restore the no-bindings state afterwards so other tests keep their static roles."""
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(su)
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM identity.role_bindings"))
        await conn.execute(text("DELETE FROM identity.role_binding_versions"))
        if baseline:
            for login, group in (("erin", "payments-admins"), ("frank", "ledger-admins")):
                pid = dev_user_principal(login)
                await conn.execute(text(
                    "INSERT INTO identity.users (principal_id, okta_sub, user_name, display_name) VALUES "
                    "(:p, :s, :u, :u) ON CONFLICT DO NOTHING"), {"p": pid, "s": f"test-{login}", "u": f"test-{login}"})
                await conn.execute(text("INSERT INTO identity.groups (group_id, display_name) VALUES (:g, :n) "
                                        "ON CONFLICT DO NOTHING"), {"g": f"grp_test_{group}", "n": group})
                await conn.execute(text(
                    "INSERT INTO identity.memberships (group_id, principal_id) SELECT group_id, :p FROM identity.groups "
                    "WHERE display_name = :n ON CONFLICT DO NOTHING"), {"p": pid, "n": group})
            await conn.execute(text("INSERT INTO identity.role_binding_versions (version, is_current, created_by, reason) "
                                    "VALUES (1, true, 'svc_test', 'baseline')"))
            await conn.execute(text("INSERT INTO identity.role_bindings (version, binding_id, group_name, scope, tenant_id, "
                                    "role) VALUES (1, 1, 'payments-admins', 'tenant', 'payments', 'tenant_admin')"))
    await eng.dispose()


async def test_job_lists_scoped_to_project_bindings(env):
    """A member bound only on project A sees project A's jobs in the job list, not the rest of the tenant."""
    c, fake, seed_h, *_ = env
    su = os.environ.get("CTL_SU_DSN")
    if not su:
        pytest.skip("CTL_SU_DSN not set")
    await _reset_bindings(su, baseline=True)
    try:
        await _scoped_lists(c, seed_h, su)
    finally:
        await _reset_bindings(su, baseline=False)


async def _scoped_lists(c: Any, seed_h: dict[str, str], su: str) -> None:
    tag = secrets.token_hex(3)
    pa, pb = f"prj_a{tag}", f"prj_b{tag}"
    ra, rb = (str(800000 + secrets.randbelow(99999)) for _ in range(2))
    for pid, repo in ((pa, ra), (pb, rb)):
        r = await c.post("/v1/tenants/payments/projects", headers=seed_h, json={
            "project_id": pid, "name": pid, "repos": [{"repo_id": repo, "full_name": f"acme-payments/{pid}"}]})
        assert r.status_code == 201, r.text
    erin, frank = {"X-Replicant-Dev-User": "erin"}, {"X-Replicant-Dev-User": "frank"}
    r = await c.put(f"/v1/projects/{pa}/access", headers=erin,
                    json={"bindings": [{"group": "ledger-admins", "role": "project_admin"}], "reason": "scope test"})
    assert r.status_code == 200, r.text
    ja, jb = "job_" + ulid("x")[-26:], "job_" + ulid("y")[-26:]
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(su)
    async with eng.begin() as conn:
        for job, repo in ((ja, ra), (jb, rb)):
            await conn.execute(text(
                "INSERT INTO jobs_read.jobs (job_id, tenant_id, seq, as_of, workflow_type, status, phase, requester, "
                "release_bundle_id, created_at, updated_at, snapshot, repo_ids, title) VALUES (:j, 'payments', 1, now(), "
                "'bugfix', 'running', 'implementing', :req, 'rb_" + "a" * 26 + "', now(), now(), '{}', ARRAY[:r], :t)"),
                {"j": job, "r": repo, "req": dev_user_principal("alice"), "t": f"scope {tag}"})
    await eng.dispose()
    rf = await c.get(f"/v1/jobs?q={tag}", headers=frank)
    assert rf.status_code == 200, rf.text
    ids = {i["job_id"] for i in rf.json()["items"]}
    assert ids == {ja}, ids
    assert (await c.get(f"/v1/jobs/{jb}", headers=frank)).status_code == 404
    ids = {i["job_id"] for i in (await c.get(f"/v1/jobs?q={tag}", headers=erin)).json()["items"]}
    assert ids == {ja, jb}, ids


async def test_first_project_binding_keeps_static_roles_and_audits(env):
    """With no bindings seeded, a first project-only access change must not strip the static tenant roles (erin stays
    tenant admin, alice can still read jobs), and project/config/access changes land in audit.events."""
    c, fake, seed_h, *_ = env
    su = os.environ.get("CTL_SU_DSN")
    if not su:
        pytest.skip("CTL_SU_DSN not set")
    await _reset_bindings(su, baseline=False)
    try:
        pid = f"prj_s{secrets.token_hex(3)}"
        r = await c.post("/v1/tenants/payments/projects", headers=seed_h, json={
            "project_id": pid, "name": pid, "repos": [{"repo_id": str(700000 + secrets.randbelow(99999)),
                                                       "full_name": f"acme-payments/{pid}"}]})
        assert r.status_code == 201, r.text
        erin = {"X-Replicant-Dev-User": "erin"}
        for _ in range(2):   # the second write is authorized by the roles in effect after the first
            r = await c.put(f"/v1/projects/{pid}/access", headers=erin,
                            json={"bindings": [{"group": "ledger-admins", "role": "project_admin"}], "reason": "first"})
            assert r.status_code == 200, r.text
        r = await c.put(f"/v1/projects/{pid}/config", headers={**erin, "If-Match": "0"},
                        json={"config": {"risk": {"large_change_files": 5}}, "reason": "audit"})
        assert r.status_code == 200, r.text
        assert (await c.get("/v1/jobs", headers={"X-Replicant-Dev-User": "alice"})).status_code == 200
        from sqlalchemy.ext.asyncio import create_async_engine
        eng = create_async_engine(su)
        async with eng.begin() as conn:
            kinds = {r.event_type for r in (await conn.execute(text(
                "SELECT event_type FROM audit.events WHERE target_id = :p"), {"p": pid})).all()}
        await eng.dispose()
        assert {"admin.project_created", "admin.role_binding_changed", "admin.project_config_changed"} <= kinds, kinds
    finally:
        await _reset_bindings(su, baseline=False)



async def test_notification_preferences_versioned(env):
    """SEC-D3: PUT /v1/me/notification-preferences honours If-Match / body version like the UI preferences."""
    c, *_ = env
    su = os.environ.get("CTL_SU_DSN")
    if not su:
        pytest.skip("CTL_SU_DSN not set")
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(su)
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM notify.preferences WHERE principal_id = :p"), {"p": dev_user_principal("dave")})
    await eng.dispose()
    h = {"X-Replicant-Dev-User": "dave"}
    qh = {"quiet_hours": {"start": "22:00", "end": "07:00", "timezone": "UTC"}}
    r = await c.get("/v1/me/notification-preferences", headers=h)
    assert r.status_code == 200 and r.json()["version"] == 1 and r.headers["etag"] == '"1"'
    # two clients both read version 1 (nothing stored); the first write wins, the second is refused
    r = await c.put("/v1/me/notification-preferences", headers={**h, "If-Match": '"1"'}, json=qh)
    assert r.status_code == 200 and r.json()["version"] == 2 and r.headers["etag"] == '"2"', r.text
    r = await c.put("/v1/me/notification-preferences", headers=h, json={"version": 1, "digest": {"enabled": False}})
    assert r.status_code == 412 and r.json()["code"] == "version_mismatch" and r.json()["current_version"] == 2
    assert r.headers["etag"] == '"2"'
    # body version (If-Match wins over it)
    r = await c.put("/v1/me/notification-preferences", headers=h, json={**qh, "version": 2})
    assert r.status_code == 200 and r.json()["version"] == 3
    r = await c.put("/v1/me/notification-preferences", headers={**h, "If-Match": "2"}, json={**qh, "version": 3})
    assert r.status_code == 412 and r.json()["current_version"] == 3
    r = await c.get("/v1/me/notification-preferences", headers=h)
    assert r.json()["version"] == 3 and r.headers["etag"] == '"3"' and r.json()["quiet_hours"]["start"] == "22:00"
    # no version: last-writer-wins, as before
    r = await c.put("/v1/me/notification-preferences", headers=h, json={"digest": {"enabled": False}})
    assert r.status_code == 200 and r.json()["version"] == 4
    r = await c.put("/v1/me/notification-preferences", headers=h, json={"version": "x"})
    assert r.status_code == 422
    r = await c.put("/v1/me/notification-preferences", headers={**h, "If-Match": "abc"}, json={})
    assert r.status_code == 400 and r.json()["code"] == "invalid_if_match"
    # a row written before SEC-D3 can sit at version 1: version 1 then updates it rather than looping on 412
    eng = create_async_engine(su)
    async with eng.begin() as conn:
        await conn.execute(text("UPDATE notify.preferences SET version = 1 WHERE principal_id = :p"),
                           {"p": dev_user_principal("dave")})
    await eng.dispose()
    r = await c.put("/v1/me/notification-preferences", headers={**h, "If-Match": "1"}, json=qh)
    assert r.status_code == 200 and r.json()["version"] == 2, r.text
