"""SIMPLIFY-D50: the Runner's core routes (routes_runner.py) that replace the run-grants app. runner-local asks for a
job's run scope (tenant, targets, acting_for, revoked) and for the job revocation list; only the Runner's caller
identity may. No database: the transaction is faked from the SQL it runs."""
import contextlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from control_plane_api.app import create_app, iter_routes
from control_plane_api.config import Settings

JOB = "job_" + "a" * 26
REQ = {"requester": {"principal_id": "usr_" + "b" * 26, "on_behalf_of": None},
       "targets": [{"repo": {"repo_id": "R_1"}}, {"repo": {"repo_id": "R_2"}}]}


class FakeConn:
    def __init__(self, db: "FakeDB") -> None:
        self.db = db

    async def execute(self, stmt, params=None):  # noqa: ANN001, ANN201
        sql = str(stmt)
        self.db.calls.append((sql, dict(params or {}), self.db.tx_kw))
        rows: list = []
        if "FROM intake.job_requests" in sql:
            j = self.db.jobs.get(params["j"])
            if j and j["tenant_id"] == params["t"]:
                rows = [SimpleNamespace(tenant_id=j["tenant_id"], request=json.dumps(j["request"]))]
        elif "SELECT status FROM jobs_read.jobs" in sql:
            j = self.db.jobs.get(params["j"])
            rows = [SimpleNamespace(status=j["status"])] if j else []
        elif "FROM jobs_read.jobs WHERE status IN" in sql:
            import datetime as dt
            rows = [SimpleNamespace(job_id=k, at=dt.datetime(2026, 11, 23, tzinfo=dt.UTC)) for k, j in sorted(self.db.jobs.items())
                    if j["status"] in ("cancelled", "failed", "taken_over")]
        return SimpleNamespace(first=lambda: rows[0] if rows else None, scalar=lambda: rows[0].status if rows else None,
                               all=lambda: rows)


class FakeDB:
    def __init__(self) -> None:
        self.jobs = {JOB: {"tenant_id": "payments", "request": REQ, "status": "running"}}
        self.calls: list = []
        self.tx_kw: dict = {}

    @contextlib.asynccontextmanager
    async def tx(self, **kw):  # noqa: ANN003, ANN201
        self.tx_kw = kw
        yield FakeConn(self)

    async def has_relation(self, name: str) -> bool:
        return name == "jobs_read.jobs"


@pytest.fixture
def client(monkeypatch, tmp_path):  # noqa: ANN001, ANN201
    callers = tmp_path / "callers"
    callers.write_text('"tok-runner": svc_runner-local\n"tok-worker": svc_replicant-worker\n')
    app = create_app(Settings(background_tasks=False, internal_caller_tokens_file=str(callers)))
    db = FakeDB()
    monkeypatch.setattr(app.state.db, "tx", db.tx)
    monkeypatch.setattr(app.state.db, "has_relation", db.has_relation)

    async def refresh(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None
    monkeypatch.setattr(app.state.auth.rbac, "refresh", refresh)
    return TestClient(app), db, app


RUNNER = {"Authorization": "Bearer tok-runner"}


def test_run_scope_is_the_intake_record(client):  # noqa: ANN001
    c, db, _ = client
    r = c.get(f"/internal/v1/jobs/{JOB}/run-scope", params={"tenant_id": "payments"}, headers=RUNNER)
    assert r.status_code == 200, r.text
    assert r.json() == {"job_id": JOB, "tenant_id": "payments", "repo_ids": ["R_1", "R_2"],
                        "acting_for": REQ["requester"]["principal_id"], "status": "running", "revoked": False}
    assert db.calls[0][2] == {"tenant": "payments"}   # read under the tenant's row-level scope


def test_another_tenants_job_is_not_found(client):  # noqa: ANN001
    c, _, _ = client
    r = c.get(f"/internal/v1/jobs/{JOB}/run-scope", params={"tenant_id": "other"}, headers=RUNNER)
    assert r.status_code == 404


def test_revoked_job(client):  # noqa: ANN001
    c, db, _ = client
    db.jobs[JOB]["status"] = "cancelled"
    assert c.get(f"/internal/v1/jobs/{JOB}/run-scope", params={"tenant_id": "payments"}, headers=RUNNER).json()["revoked"]
    r = c.get("/internal/v1/revocations", headers=RUNNER)
    assert r.status_code == 200 and [j["id"] for j in r.json()["job_ids"]] == [JOB] and r.json()["run_ids"] == []
    again = c.get("/internal/v1/revocations", headers={**RUNNER, "If-None-Match": r.headers["etag"]})
    assert again.status_code == 304


def test_only_the_runner_may_ask(client):  # noqa: ANN001
    c, _, _ = client
    for h in ({}, {"Authorization": "Bearer tok-worker"}):
        assert c.get(f"/internal/v1/jobs/{JOB}/run-scope", params={"tenant_id": "payments"}, headers=h).status_code in (401, 403)
        assert c.get("/internal/v1/revocations", headers=h).status_code in (401, 403)


def test_no_run_grant_routes_left(client):  # noqa: ANN001
    _, _, app = client
    paths = {getattr(r, "path", "") for r in iter_routes(list(app.routes))}
    assert not paths & {"/.well-known/jwks.json", "/grants/revoked"}
