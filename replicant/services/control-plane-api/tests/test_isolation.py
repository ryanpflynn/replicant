"""Tenant and project isolation (v0 assessment F7, v0-system-design §8a/§9, DEVIATIONS SEC-D1/SEC-D2).

DB-gated like test_db_integration.py (same env vars; CTL_SU_DSN is required for seeding other tenants' rows):

* the schema invariant: every tenant table (and partition) has RLS enabled and forced;
* RLS behaviour: under a tenant-scoped connection another tenant's rows are invisible;
* the API matrix: control-plane-api reads run under OPERATOR_SCOPE (RLS bypassed), so isolation rests on `check()` and
  `scope_filter`. Another tenant's (or, inside one tenant, another project's) jobs, gates and projects answer 404,
  never 403, and never appear in a list. Writes to them are refused.
"""
from __future__ import annotations

import os
import secrets
from typing import Any

import pytest
from sqlalchemy import text
from test_db_integration import _reset_bindings, env  # noqa: F401  (fixture)

from control_plane_api.db import OPERATOR_SCOPE
from control_plane_api.ids import dev_user_principal, ulid

DSN = os.environ.get("CTL_DB_DSN")
SU = os.environ.get("CTL_SU_DSN")
pytestmark = pytest.mark.skipif(not (DSN and SU), reason="CTL_DB_DSN / CTL_SU_DSN not set (start tests/smoke/env.sh)")

OTHER = "acme-retail"
BUNDLE = "rb_" + "a" * 26
DIGEST = "sha256:" + "e" * 64

# Tables with a tenant_id column that are deliberately not tenant-isolated by RLS. Empty: the only former exception,
# audit.events, is forced since 202610270030_audit_events_force (SEC-D1). Add an entry only for a genuinely global
# table, with the reason.
RLS_ALLOWLIST: dict[str, str] = {}

# schemas holding platform machinery rather than tenant data
NON_TENANT_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def _id(prefix: str) -> str:
    return f"{prefix}_" + ulid("x")[-26:]


async def _su_exec(sql_params: list[tuple[str, dict[str, Any]]]) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(SU)
    try:
        async with eng.begin() as conn:
            for sql, p in sql_params:
                await conn.execute(text(sql), p)
    finally:
        await eng.dispose()


def _job_rows(job: str, tenant: str, repo: str, run: str, gate: str, title: str) -> list[tuple[str, dict[str, Any]]]:
    """A projected job with one run, one open publish gate, its intake row and a notification delivery."""
    req = dev_user_principal("isolation-requester")   # nobody logs in as it: gates stay decidable by others
    return [
        ("INSERT INTO jobs_read.jobs (job_id, tenant_id, seq, as_of, workflow_type, status, phase, requester, "
         "release_bundle_id, created_at, updated_at, snapshot, repo_ids, title) VALUES (:j, :t, 1, now(), 'bugfix', "
         "'running', 'implementing', :req, :b, now(), now(), '{}', ARRAY[:r], :ti)",
         {"j": job, "t": tenant, "r": repo, "req": req, "b": BUNDLE, "ti": title}),
        ("INSERT INTO jobs_read.job_runs (run_id, job_id, tenant_id, seq, node_key, cycle, ordinal, role, phase) "
         "VALUES (:run, :j, :t, 1, 'implement', 0, 0, 'implementer', 'Running')", {"run": run, "j": job, "t": tenant}),
        ("INSERT INTO approvals.gates (gate_id, job_id, tenant_id, seq, kind, state, requester, opened_at, "
         "packet_revision, presented_digest, packet_key, channels, title) VALUES (:g, :j, :t, 1, 'publish_approval', "
         "'open', :req, now(), 1, :d, 'k', '{ui,cli}', :ti)",
         {"g": gate, "j": job, "t": tenant, "req": req, "d": DIGEST, "ti": title}),
        ("INSERT INTO intake.job_requests (job_id, tenant_id, request_id, dedupe_key, channel, requester, submitted_by, "
         "workflow_type, request, release_bundle_id, assignment_reason, release_cohort, assignment_version, received_at, "
         "start_status) VALUES (:j, :t, :rq, :dk, 'ui', :req, :req, 'auto', '{}', :b, 'default', 'default', 1, now(), "
         "'started')",       # 'started': the intake retrier must not pick these fixtures up
         {"j": job, "t": tenant, "rq": _id("jrq"), "dk": f"ui:iso:{job}", "req": req, "b": BUNDLE}),
        ("INSERT INTO notify.deliveries (delivery_id, dedupe_key, tenant_id, event_key, recipient, channel, state, job_id) "
         "VALUES (:dl, :dk, :t, 'gate.requested', :rcp, 'ui', 'sent', :j)",
         {"dl": _id("dlv"), "dk": f"iso:{job}", "t": tenant, "rcp": dev_user_principal("bob"), "j": job}),
    ]


async def _other_tenant(c: Any, seed_h: dict[str, str]) -> dict[str, str]:
    """acme-retail with a project (and a config version), a job, a run and an open gate."""
    r = await c.post("/v1/admin/tenants", headers=seed_h, json={"tenant_id": OTHER, "display_name": OTHER})
    assert r.status_code in (200, 201, 409), r.text
    tag = secrets.token_hex(3)
    w = {"tag": tag, "project": f"prj_x{tag}", "repo": str(600000 + secrets.randbelow(99999)),
         "job": _id("job"), "run": _id("run"), "gate": _id("gate")}
    r = await c.post(f"/v1/tenants/{OTHER}/projects", headers=seed_h, json={
        "project_id": w["project"], "name": w["project"],
        "repos": [{"repo_id": w["repo"], "full_name": f"acme-retail/{w['project']}"}]})
    assert r.status_code == 201, r.text
    r = await c.put(f"/v1/projects/{w['project']}/config", headers={**seed_h, "If-Match": "0"},
                    json={"config": {"risk": {"large_change_files": 7}}, "reason": "isolation"})
    assert r.status_code == 200, r.text
    await _su_exec(_job_rows(w["job"], OTHER, w["repo"], w["run"], w["gate"], f"iso {tag}"))
    return w


# ------------------------------------------------------------------------------------------------ a. schema invariant
async def test_every_tenant_table_has_forced_rls(env):  # noqa: F811
    c, *_ = env
    db = c._transport.app.state.db  # type: ignore[attr-defined]
    async with db.tx() as conn:
        rows = (await conn.execute(text(
            "SELECT n.nspname || '.' || c.relname AS rel, c.relkind, c.relispartition, c.relrowsecurity, "
            "c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped "
            "WHERE c.relkind IN ('r', 'p') AND n.nspname <> ALL (:skip) ORDER BY 1"),
            {"skip": list(NON_TENANT_SCHEMAS)})).all()
    assert len(rows) > 50, "expected the migrated schema"           # guards against an empty or wrong database
    assert any(r.relispartition for r in rows), "partitions must be covered"
    bad = [f"{r.rel} (rls={r.relrowsecurity}, force={r.relforcerowsecurity})" for r in rows
           if r.rel not in RLS_ALLOWLIST and not (r.relrowsecurity and r.relforcerowsecurity)]
    assert not bad, "tenant tables without ENABLE+FORCE row-level security: " + ", ".join(bad)


async def test_audit_emit_still_writes_with_forced_rls(env):  # noqa: F811
    """SEC-D1 forced RLS on audit.events: audit.emit (SECURITY DEFINER, audit_owner) must still insert."""
    c, _, seed_h, *_ = env
    pid = f"prj_e{secrets.token_hex(3)}"
    r = await c.post("/v1/tenants/payments/projects", headers=seed_h, json={
        "project_id": pid, "name": pid, "repos": [{"repo_id": str(500000 + secrets.randbelow(99999)),
                                                   "full_name": f"acme-payments/{pid}"}]})
    assert r.status_code == 201, r.text
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(SU)
    async with eng.begin() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM audit.events WHERE target_id = :p AND event_type = "
                                     "'admin.project_created'"), {"p": pid})).scalar()
        forced = (await conn.execute(text("SELECT relforcerowsecurity FROM pg_class WHERE oid = "
                                          "'audit.events'::regclass"))).scalar()
    await eng.dispose()
    assert forced and n == 1


# ------------------------------------------------------------------------------------------------ b. RLS behaviour
RLS_TABLES = {"intake.job_requests": "job_id", "jobs_read.jobs": "job_id", "approvals.gates": "gate_id",
              "notify.deliveries": "job_id", "tenancy.project_config_versions": "project_id"}


async def test_rls_hides_other_tenants_rows(env):  # noqa: F811
    c, _, seed_h, *_ = env
    w = await _other_tenant(c, seed_h)
    key = {"job_id": w["job"], "gate_id": w["gate"], "project_id": w["project"]}
    db = c._transport.app.state.db  # type: ignore[attr-defined]
    for table, col in RLS_TABLES.items():
        q = text(f"SELECT tenant_id FROM {table} WHERE {col} = :k")
        async with db.tx(tenant="payments") as conn:
            assert (await conn.execute(q, {"k": key[col]})).all() == [], f"{table}: acme-retail row visible to payments"
            tenants = {r[0] for r in (await conn.execute(text(f"SELECT DISTINCT tenant_id FROM {table}"))).all()}
            assert tenants <= {"payments"}, f"{table}: {tenants}"
        async with db.tx(tenant=OTHER) as conn:          # the row exists: RLS filtered it, it is not just missing
            assert [r[0] for r in (await conn.execute(q, {"k": key[col]})).all()] == [OTHER], table
        async with db.tx() as conn:                       # no tenant and no scope: nothing at all
            assert (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar() == 0, table
        async with db.tx(scope=OPERATOR_SCOPE) as conn:   # the operator scope the API reads use does see it
            assert (await conn.execute(q, {"k": key[col]})).first() is not None, table


# ------------------------------------------------------------------------------------------------ c/d. API matrices
def _h(login: str, **extra: str) -> dict[str, str]:
    return {"X-Replicant-Dev-User": login, **extra}


async def _assert_hidden(c: Any, login: str, w: dict[str, str], *, can_write_projects: bool) -> None:
    """`login` must get 404 on every read and write of w's job, gate and project, and never see them in a list."""
    h = _h(login)
    job, gate, prj = w["job"], w["gate"], w["project"]
    for path in (f"/v1/jobs/{job}", f"/v1/jobs/{job}/timeline", f"/v1/jobs/{job}/runs", f"/v1/jobs/{job}/artifacts",
                 f"/v1/jobs/{job}/graph", f"/v1/jobs/{job}/runs/{w['run']}/activity", f"/v1/gates/{gate}",
                 f"/v1/gates/{gate}/packet/refs/diff", f"/v1/projects/{prj}", f"/v1/projects/{prj}/config",
                 f"/v1/projects/{prj}/access"):
        r = await c.get(path, headers=h)
        assert r.status_code == 404, f"{login} GET {path}: {r.status_code} {r.text[:200]}"
    r = await c.post(f"/v1/gates/{gate}/decisions", headers=_h(login, **{"Idempotency-Key": ulid("k")[2:]}),
                     json={"decision": "approve", "presented_digest": DIGEST})
    # 403 only from the route dependency, when the caller may decide no gate anywhere (says nothing about this gate)
    assert r.status_code == 404 or (r.status_code == 403 and r.json()["code"] == "missing_permission"), \
        f"{login} decide: {r.status_code} {r.text[:200]}"
    # job control and steering
    for path, body in ((f"/v1/jobs/{job}:cancel", {"action_id": _id("act"), "reason": "x"}),
                       (f"/v1/jobs/{job}:pause", {"action_id": _id("act")}),
                       (f"/v1/jobs/{job}:resume", {"action_id": _id("act")}),
                       (f"/v1/jobs/{job}/steering", {"message_id": _id("msg"), "text": "hi"})):
        r = await c.post(path, headers=_h(login, **{"Idempotency-Key": ulid("k")[2:]}), json=body)
        assert r.status_code in (403, 404), f"{login} POST {path}: {r.status_code}"
        if r.status_code == 403:      # only when the caller holds no control permission anywhere (route dependency)
            assert r.json()["code"] == "missing_permission", r.text
    # project writes
    writes = [("PATCH", f"/v1/projects/{prj}", {}, {"name": "pwned"}),
              ("PUT", f"/v1/projects/{prj}/config", {"If-Match": "1"}, {"config": {}, "reason": "x"}),
              ("PUT", f"/v1/projects/{prj}/repos/{w['repo']}/config", {"If-Match": "0"}, {"config": {}, "reason": "x"}),
              ("POST", f"/v1/projects/{prj}/config:rollback", {}, {"version": 1, "reason": "x"}),
              ("PUT", f"/v1/projects/{prj}/access", {}, {"bindings": [], "reason": "x"})]
    for method, path, hx, body in writes:
        r = await c.request(method, path, headers={**h, **hx}, json=body)
        want = (404,) if can_write_projects else (403, 404)
        assert r.status_code in want, f"{login} {method} {path}: {r.status_code} {r.text[:200]}"
        if r.status_code == 403:
            assert r.json()["code"] == "missing_permission", r.text
    # lists
    r = await c.get(f"/v1/jobs?q={w['tag']}", headers=h)
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert job not in {i["job_id"] for i in r.json()["items"]}, f"{login} lists {job}"
    r = await c.get("/v1/jobs?limit=200", headers=h)
    if r.status_code == 200:
        assert job not in {i["job_id"] for i in r.json()["items"]}
    for path in ("/v1/approvals", "/v1/approvals?view=decided", "/v1/me/waiting"):
        r = await c.get(path, headers=h)
        assert r.status_code == 200, r.text
        assert gate not in {i.get("gate_id") for i in r.json()["items"]}, f"{login} {path} lists {gate}"
        assert job not in {i.get("job_id") for i in r.json()["items"]}, f"{login} {path} lists {job}"
    r = await c.get("/v1/projects", headers=h)
    if r.status_code == 200:
        assert prj not in {i["project_id"] for i in r.json()["items"]}, f"{login} lists {prj}"


async def test_cross_tenant_api_matrix(env):  # noqa: F811
    c, _, seed_h, *_ = env
    await _reset_bindings(SU, baseline=False)          # static dev roles: payments users are payments members
    w = await _other_tenant(c, seed_h)
    # the objects exist and are visible to the platform operator
    g = _h("grace")
    assert (await c.get(f"/v1/jobs/{w['job']}", headers=g)).status_code == 200
    assert (await c.get(f"/v1/gates/{w['gate']}", headers=g)).status_code == 200
    assert (await c.get(f"/v1/projects/{w['project']}", headers=g)).status_code == 200
    assert w["gate"] in {i["gate_id"] for i in (await c.get("/v1/approvals", headers=g)).json()["items"]}
    for login in ("alice", "bob", "dave", "erin", "frank"):
        await _assert_hidden(c, login, w, can_write_projects=login == "erin")
    # tenant-scoped listing of the other tenant
    r = await c.get(f"/v1/tenants/{OTHER}/projects", headers=_h("erin"))
    assert r.status_code == 200 and r.json()["items"] == [], r.text


async def test_cross_tenant_repo_settings_refused(env):  # noqa: F811
    c, _, seed_h, *_ = env
    await _reset_bindings(SU, baseline=False)
    w = await _other_tenant(c, seed_h)
    body = {"repo_id": w["repo"], "full_name": "acme-retail/x"}
    r = await c.put(f"/v1/tenants/{OTHER}/github/repos/{w['repo']}/settings", headers=_h("erin"), json=body)
    assert r.status_code == 403, r.text           # erin holds tenant:ops:edit on payments only
    # through her own tenant's path: the repo belongs to acme-retail's project
    r = await c.put(f"/v1/tenants/payments/github/repos/{w['repo']}/settings", headers=_h("erin"), json=body)
    assert r.status_code == 404, r.text
    r = await c.put(f"/v1/tenants/payments/github/repos/{w['repo']}/settings", headers=seed_h, json=body)
    assert r.status_code == 404, r.text            # even the seeder cannot file a repo under the wrong tenant


async def test_cross_project_api_matrix(env):  # noqa: F811
    """frank (group ledger-admins) is project_admin of project A only: project B of the same tenant is invisible."""
    c, _, seed_h, *_ = env
    await _reset_bindings(SU, baseline=True)
    try:
        tag = secrets.token_hex(3)
        ws = {}
        for name in ("a", "b"):
            w = {"tag": tag, "project": f"prj_{name}{tag}", "repo": str(400000 + secrets.randbelow(99999)),
                 "job": _id("job"), "run": _id("run"), "gate": _id("gate")}
            r = await c.post("/v1/tenants/payments/projects", headers=seed_h, json={
                "project_id": w["project"], "name": w["project"],
                "repos": [{"repo_id": w["repo"], "full_name": f"acme-payments/{w['project']}"}]})
            assert r.status_code == 201, r.text
            r = await c.put(f"/v1/projects/{w['project']}/config", headers={**seed_h, "If-Match": "0"},
                            json={"config": {"risk": {"large_change_files": 3}}, "reason": "isolation"})
            assert r.status_code == 200, r.text
            await _su_exec(_job_rows(w["job"], "payments", w["repo"], w["run"], w["gate"], f"iso {tag}"))
            ws[name] = w
        r = await c.put(f"/v1/projects/{ws['a']['project']}/access", headers=_h("erin"),
                        json={"bindings": [{"group": "ledger-admins", "role": "project_admin"}], "reason": "iso"})
        assert r.status_code == 200, r.text
        f = _h("frank")
        a = ws["a"]
        # project A is fully visible to frank
        for path in (f"/v1/jobs/{a['job']}", f"/v1/jobs/{a['job']}/runs", f"/v1/gates/{a['gate']}",
                     f"/v1/projects/{a['project']}", f"/v1/projects/{a['project']}/config",
                     f"/v1/projects/{a['project']}/access"):
            assert (await c.get(path, headers=f)).status_code == 200, path
        assert a["job"] in {i["job_id"] for i in (await c.get(f"/v1/jobs?q={tag}", headers=f)).json()["items"]}
        assert a["project"] in {i["project_id"] for i in (await c.get("/v1/projects", headers=f)).json()["items"]}
        r = await c.put(f"/v1/projects/{a['project']}/config", headers={**f, "If-Match": "1"},
                        json={"config": {"risk": {"large_change_files": 4}}, "reason": "mine"})
        assert r.status_code == 200, r.text
        r = await c.post(f"/v1/gates/{a['gate']}/decisions", headers={**f, "Idempotency-Key": ulid("k")[2:]},
                         json={"decision": "approve", "presented_digest": DIGEST})
        assert r.status_code != 404, r.text     # a gate of a project frank can read is not hidden from him
        # project B is not
        await _assert_hidden(c, "frank", ws["b"], can_write_projects=True)
        # and erin (tenant admin) still sees both
        ids = {i["job_id"] for i in (await c.get(f"/v1/jobs?q={tag}", headers=_h("erin"))).json()["items"]}
        assert ids == {ws["a"]["job"], ws["b"]["job"]}, ids
    finally:
        await _reset_bindings(SU, baseline=False)
