"""replicant-projector tests: pure mapping (no DB) and the real writer against Postgres with the spec DDL
(podman container wrk-pg-<pid>, removed afterwards). Run: uv run pytest workers/replicant-projector/tests"""
from __future__ import annotations

import asyncio
import copy
import os
import pathlib
import random
import shutil
import subprocess

import pytest

from replicant_projector import mapping
from replicant_projector.activities import validate

import events as E

HERE = pathlib.Path(__file__).resolve().parent
SPEC = pathlib.Path(os.environ.get("REPLICANT_SPEC_DIR", HERE.parents[3] / "spec"))


def test_events_are_contract_valid() -> None:
    for e in E.sequence():
        validate(e)


def test_mapping() -> None:
    seq = E.sequence()
    j = mapping.job_row(seq[-1], repo_ids=["42"])
    assert j["status"] == "succeeded" and j["terminal_outcome"] == "pr_merged" and j["seq"] == 8
    assert j["risk_level"] == "low" and j["repos"] == ["git.replicant.localhost:3300/acme-payments/ledger-api"]
    gates, elig, decs = mapping.gate_rows(seq[4])
    assert gates[0]["state"] == "approved" and gates[0]["packet_revision"] == 1
    assert decs[0]["text_key"] is None and decs[0]["text_sha256"] is None  # pair not complete yet (CHECK)
    _, _, decs = mapping.gate_rows(seq[5])
    assert decs[0]["text_key"] and decs[0]["text_sha256"] == "b" * 64
    pr = mapping.pr_row(seq[-1], ["42"])
    assert pr and pr["state"] == "merged" and pr["repo_id"] == "42" and pr["merged_at"]
    batched = copy.deepcopy(seq[3])
    batched["events"] = [{"seq": 3, "event": seq[2]["event"]}, {"seq": 4, "event": seq[3]["event"]}]
    validate(batched)
    assert [r["seq"] for r in mapping.timeline_rows(batched)] == [3, 4]
    au = mapping.audit_events(seq[4])
    assert au[0]["event_type"] == "gate.decided" and au[0]["actor_principal_id"] == E.BOB and au[0]["channel"] == "api"


def test_invalid_event_is_rejected() -> None:
    from temporalio.exceptions import ApplicationError
    bad = copy.deepcopy(E.sequence()[0])
    del bad["snapshot"]["requester"]
    with pytest.raises(ApplicationError) as ei:
        validate(bad)
    assert ei.value.type == "SchemaViolation"


@pytest.fixture(scope="module")
def pg():
    if not shutil.which("podman") or not (SPEC / "10-data-platform").exists():
        pytest.skip("podman or the spec directory is not available")
    name = f"wrk-pg-proj-{os.getpid()}"
    port = subprocess.run([str(HERE / "pg_fixture.sh"), "up", name, str(SPEC)], check=True, capture_output=True,
                          text=True).stdout.strip().splitlines()[-1]
    os.environ["REPLICANT_DB_DSN"] = f"postgresql+psycopg://svc_replicant_projector@127.0.0.1:{port}/replicant"
    pw = HERE / ".pw"
    pw.write_text("proj")
    os.environ["REPLICANT_DB_PASSWORD_FILE"] = str(pw)
    yield f"postgresql://postgres:pw@127.0.0.1:{port}/replicant"
    subprocess.run([str(HERE / "pg_fixture.sh"), "down", name], check=False)
    pw.unlink(missing_ok=True)


def _q(dsn: str, sql: str, *args):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute(sql, args).fetchall()


def test_apply_against_postgres(pg: str) -> None:
    from replicant_projector import db

    async def main() -> None:
        seq = E.sequence()
        # out of order and duplicated deliveries converge (C-STATE §2.1: idempotent, order-insensitive)
        order = seq[:] + seq[2:5]
        random.Random(7).shuffle(order)
        for e in order:
            await db.apply(e)
        await db.apply(seq[-1])
    asyncio.run(main())
    jobs = _q(pg, "SELECT seq, status, phase, terminal_outcome, risk_level, worker_build_id FROM jobs_read.jobs WHERE job_id=%s", E.JOB)
    assert jobs == [(8, "succeeded", "done", "pr_merged", "low", "b20261024-000000000000")], jobs
    assert [r[0] for r in _q(pg, "SELECT seq FROM jobs_read.job_timeline WHERE job_id=%s ORDER BY seq", E.JOB)] == list(range(1, 9))
    assert _q(pg, "SELECT phase, result_key IS NOT NULL FROM jobs_read.job_runs WHERE run_id=%s", E.RUN) == [("Succeeded", True)]
    assert _q(pg, "SELECT state, presented_digest, requester_may_decide FROM approvals.gates WHERE gate_id=%s", E.GATE) == \
        [("approved", E.DIG, True)]
    assert _q(pg, "SELECT outcome, principal_id, text_sha256 FROM approvals.gate_decisions WHERE decision_id=%s", E.DEC) == \
        [("accepted", E.BOB, "b" * 64)]
    assert _q(pg, "SELECT kind, value FROM approvals.gate_eligibility WHERE gate_id=%s", E.GATE) == [("okta_group", "payments-approvers")]
    prs = _q(pg, "SELECT pr_number, state, job_open FROM jobs_read.job_prs WHERE job_id=%s", E.JOB)
    assert prs == [(7, "merged", False)], prs
    assert _q(pg, "SELECT count(*) FROM audit.events WHERE job_id=%s AND event_type IN ('gate.opened','gate.decided','job.terminal')", E.JOB)[0][0] == 3
    # RLS: another tenant's context sees nothing (the projector login is not cross-tenant)
    import psycopg
    dsn, kw = db.conninfo()
    with psycopg.connect(dsn, **kw) as c:
        c.execute("SELECT set_config('replicant.tenant_id', 'commerce', false)")
        assert c.execute("SELECT count(*) FROM jobs_read.jobs").fetchone()[0] == 0
