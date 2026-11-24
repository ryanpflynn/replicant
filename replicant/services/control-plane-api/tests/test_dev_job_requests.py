"""SIMPLIFY-D54: POST /v1/admin/dev/job-requests (replicant_dev_auth, LOCAL ONLY) records an intake row for a test
job so the Runner contract driver's runs pass run-scope admission. Dev seed token only; absent in the prod image;
the dev package refuses to install outside REPLICANT_ENV=local. No database: the transaction is faked."""
import contextlib
import json
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from control_plane_api.app import create_app
from control_plane_api.config import Settings
from control_plane_api.routes_runner import acting_for, target_repo_ids

JOB = "job_" + "a" * 26
USR = "usr_" + "b" * 26
BODY = {"job_id": JOB, "tenant_id": "payments", "repo_ids": ["1", "7"], "requester_principal_id": USR}


class FakeConn:
    def __init__(self, db: "FakeDB") -> None:
        self.db = db

    async def execute(self, stmt, params=None):  # noqa: ANN001, ANN201
        sql, p = str(stmt), dict(params or {})
        self.db.calls.append((sql, p, self.db.tx_kw))
        rows: list = []
        if sql.startswith("SELECT tenant_id, dedupe_key FROM intake.job_requests"):
            j = self.db.rows.get(p["j"])
            rows = [SimpleNamespace(tenant_id=j["tenant_id"], dedupe_key=j["dedupe_key"])] if j else []
        elif sql.startswith("INSERT INTO intake.job_requests"):
            self.db.rows[p["j"]] = {"tenant_id": p["t"], "dedupe_key": p["k"], "requester": p["who"],
                                    "request": json.loads(p["doc"]), "release_bundle_id": p["rb"]}
        elif sql.startswith("UPDATE intake.job_requests"):
            self.db.rows[p["j"]].update(requester=p["who"], request=json.loads(p["doc"]))
        return SimpleNamespace(first=lambda: rows[0] if rows else None)


class FakeDB:
    def __init__(self) -> None:
        self.rows: dict = {}
        self.calls: list = []
        self.tx_kw: dict = {}

    @contextlib.asynccontextmanager
    async def tx(self, **kw):  # noqa: ANN003, ANN201
        self.tx_kw = kw
        yield FakeConn(self)


def make(monkeypatch, tmp_path, **settings):  # noqa: ANN001, ANN003, ANN201
    seed = tmp_path / "seed"
    seed.write_text("tok-seed\n")
    callers = tmp_path / "callers"
    callers.write_text('"tok-runner": svc_runner-local\n')
    app = create_app(Settings(background_tasks=False, dev_seed_token_file=str(seed),
                              internal_caller_tokens_file=str(callers), **settings))
    db = FakeDB()
    monkeypatch.setattr(app.state.db, "tx", db.tx)

    async def refresh(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None
    monkeypatch.setattr(app.state.auth.rbac, "refresh", refresh)
    return TestClient(app), db


SEED = {"Authorization": "Bearer tok-seed"}


def test_registers_a_started_row_run_scope_can_read(monkeypatch, tmp_path):  # noqa: ANN001
    c, db = make(monkeypatch, tmp_path)
    r = c.post("/v1/admin/dev/job-requests", json=BODY, headers=SEED)
    assert r.status_code == 201, r.text
    assert r.json() == {"job_id": JOB, "tenant_id": "payments", "repo_ids": ["1", "7"], "acting_for": USR,
                        "created": True}
    row = db.rows[JOB]
    assert row["tenant_id"] == "payments" and row["requester"] == USR and row["dedupe_key"] == f"dev:test-job:{JOB}"
    assert target_repo_ids(row["request"]) == ["1", "7"] and acting_for(row["request"]) == USR
    ins = next(x for x in db.calls if x[0].startswith("INSERT"))
    # born `started` (the intake retrier never starts a workflow for it), written as the intake role in the tenant
    assert "'started'" in ins[0] and ins[2] == {"role": "svc_cpa_intake", "tenant": "payments"}
    # idempotent for the same tenant: the targets and requester are replaced
    other = "usr_" + "c" * 26
    r = c.post("/v1/admin/dev/job-requests", json={**BODY, "repo_ids": ["2"], "requester_principal_id": other},
               headers=SEED)
    assert r.status_code == 200 and r.json()["created"] is False
    assert target_repo_ids(db.rows[JOB]["request"]) == ["2"] and acting_for(db.rows[JOB]["request"]) == other


def test_conflicts_and_validation(monkeypatch, tmp_path):  # noqa: ANN001
    c, db = make(monkeypatch, tmp_path)
    assert c.post("/v1/admin/dev/job-requests", json=BODY, headers=SEED).status_code == 201
    r = c.post("/v1/admin/dev/job-requests", json={**BODY, "tenant_id": "local-dev"}, headers=SEED)
    assert r.status_code == 409 and r.json()["code"] == "job_exists"
    db.rows["job_" + "d" * 26] = {"tenant_id": "payments", "dedupe_key": "ui:form:x"}   # a job a channel submitted
    r = c.post("/v1/admin/dev/job-requests", json={**BODY, "job_id": "job_" + "d" * 26}, headers=SEED)
    assert r.status_code == 409
    for bad in ({**BODY, "job_id": "job_x"}, {**BODY, "repo_ids": []}, {**BODY, "repo_ids": ["1"] * 6},
                {**BODY, "requester_principal_id": "mallory"}, {**BODY, "tenant_id": "Payments"},
                {**BODY, "extra": 1}, {**BODY, "release_bundle_id": "rb_x"}):
        r = c.post("/v1/admin/dev/job-requests", json=bad, headers=SEED)
        assert r.status_code == 422, bad


def test_refused_without_the_seed_token(monkeypatch, tmp_path):  # noqa: ANN001
    c, db = make(monkeypatch, tmp_path)
    assert c.post("/v1/admin/dev/job-requests", json=BODY).status_code == 401
    assert c.post("/v1/admin/dev/job-requests", json=BODY, headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.post("/v1/admin/dev/job-requests", json=BODY, headers={"Authorization": "Bearer tok-runner"})
    assert r.status_code == 403
    r = c.post("/v1/admin/dev/job-requests", json=BODY, headers={"X-Replicant-Dev-User": "grace"})
    assert r.status_code == 403
    assert db.rows == {}


def test_absent_without_the_dev_package(monkeypatch, tmp_path):  # noqa: ANN001
    monkeypatch.setitem(sys.modules, "replicant_dev_auth", None)   # the prod image does not ship it
    c, db = make(monkeypatch, tmp_path)
    assert c.post("/v1/admin/dev/job-requests", json=BODY, headers=SEED).status_code in (401, 404, 405)
    assert not any(getattr(r, "path", "") == "/v1/admin/dev/job-requests" for r in c.app.routes)


def test_dev_package_refuses_outside_local(tmp_path):  # noqa: ANN001
    import replicant_dev_auth
    s = SimpleNamespace(is_local=False, dev_principals_file=None, dev_seed_token_file=None)
    with pytest.raises(SystemExit):
        replicant_dev_auth.install(SimpleNamespace(), SimpleNamespace(), s)
