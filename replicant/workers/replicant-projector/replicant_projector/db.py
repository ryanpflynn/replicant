"""Postgres writer (login svc_replicant_projector). One transaction per ProjectionEvent (C-STATE §2.1 step 2):
tenant context (`replicant.tenant_id`, RLS), timeline rows ON CONFLICT DO NOTHING, seq-guarded upserts of jobs, job_runs,
approvals.gates, the inbox eligibility index, decisions (from GateView.decision_log), PRs, memory usage, steering,
audit.emit() for gate and terminal events, and NOTIFY replicant_job_updates '<tenant>:<job>:<seq>'."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from replicant_projector import mapping

log = logging.getLogger("replicant_projector.db")
_pool: AsyncConnectionPool | None = None


def conninfo() -> tuple[str, dict[str, Any]]:
    dsn = os.environ.get("REPLICANT_DB_DSN", "postgresql://svc_replicant_projector@localhost:5432/replicant")
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")
    kw: dict[str, Any] = {"application_name": "replicant-projector"}
    pf = os.environ.get("REPLICANT_DB_PASSWORD_FILE")
    if pf and os.path.exists(pf):
        with open(pf, encoding="utf-8") as f:
            kw["password"] = f.read().strip()
    return dsn, kw


async def pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        dsn, kw = conninfo()
        _pool = AsyncConnectionPool(dsn, kwargs=kw, min_size=1, max_size=int(os.environ.get("REPLICANT_DB_POOL_MAX", "8")),
                                    open=False)
        await _pool.open(wait=False)
    return _pool


def J(v: Any) -> Any:
    return Jsonb(v) if v is not None else None


JOB_COLS = ["job_id", "tenant_id", "seq", "as_of", "parent_job_id", "workflow_kind", "workflow_type", "status", "phase",
            "sub_status", "sub_status_detail", "pr_repo_id", "pr_number", "current_node", "current_node_started_at", "title",
            "requester", "on_behalf_of", "priority", "risk_level", "evaluation_mode", "repos", "repo_ids",
            "release_bundle_id", "worker_build_id", "memory_snapshot_id", "cycle", "repair_used", "repair_max", "paused",
            "open_gate_id", "waiting_since", "last_progress_at", "terminal_outcome", "terminal_at", "session_purge_due_at",
            "created_at", "snapshot"]
JSON_COLS = {"sub_status_detail", "snapshot", "refs", "winning_decision", "release_block"}
# kept from the first insert, or kept when the newer snapshot does not carry them
KEEP_ON_UPDATE = {"created_at"}
COALESCE_ON_UPDATE = {"repo_ids", "session_purge_due_at", "pr_repo_id"}


def _vals(row: dict[str, Any], cols: list[str]) -> list[Any]:
    return [J(row[c]) if c in JSON_COLS else row[c] for c in cols]


def _upsert_sql(table: str, cols: list[str], pk: str, *, guard: bool = True, keep: set[str] = frozenset(),
                coalesce: set[str] = frozenset()) -> str:
    sets = []
    for c in cols:
        if c == pk or c in keep:
            continue
        if c in coalesce:
            if c == "repo_ids":
                sets.append(f"{c} = CASE WHEN cardinality(excluded.{c}) > 0 THEN excluded.{c} ELSE {table}.{c} END")
            else:
                sets.append(f"{c} = COALESCE(excluded.{c}, {table}.{c})")
        else:
            sets.append(f"{c} = excluded.{c}")
    if table == "jobs_read.jobs":
        sets.append("updated_at = now()")
    where = f" WHERE {table}.seq < excluded.seq" if guard else ""
    return (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT ({pk}) DO UPDATE SET {', '.join(sets)}{where}")


RUN_COLS = ["run_id", "job_id", "tenant_id", "seq", "node_key", "cycle", "ordinal", "role", "phase", "reason", "isolation",
            "started_at", "finished_at", "artifact_manifest_key", "result_key"]
GATE_COLS = ["gate_id", "job_id", "tenant_id", "seq", "kind", "state", "risk_level", "title", "repo", "requester",
             "release_bundle_id", "opened_at", "expires_at", "next_reminder_at", "escalation_level", "packet_revision",
             "presented_digest", "packet_key", "channels", "min_repo_permission", "requester_may_decide", "qa_reruns_used",
             "winning_decision", "decided_at", "release_block"]
DEC_COLS = ["decision_id", "gate_id", "tenant_id", "outcome", "decision", "principal_id", "channel", "delegate_of",
            "denial_reason", "text_key", "text_sha256", "decided_at", "presented_digest"]
PR_COLS = ["repo_id", "pr_number", "job_id", "tenant_id", "repo", "head_ref", "head_sha", "state", "url", "job_open",
           "opened_at", "merged_at", "closed_at"]


async def _repo_ids(cur: psycopg.AsyncCursor, job_id: str) -> list[str]:
    """Immutable provider repo ids from the intake record (the snapshot carries only <host>/<owner>/<name>)."""
    try:
        await cur.execute("SAVEPOINT repo_ids")
        await cur.execute("SELECT request -> 'targets' FROM intake.job_requests WHERE job_id = %s", (job_id,))
        row = await cur.fetchone()
        await cur.execute("RELEASE SAVEPOINT repo_ids")
    except psycopg.Error as e:
        await cur.execute("ROLLBACK TO SAVEPOINT repo_ids")
        log.debug("intake read failed: %s", e)
        return []
    if not row or not row[0]:
        return []
    return [str(t["repo"]["repo_id"]) for t in row[0] if (t.get("repo") or {}).get("repo_id")][:15]


async def _savepoint(cur: psycopg.AsyncCursor, name: str, sql: str, params: Any) -> bool:
    await cur.execute(f"SAVEPOINT {name}")
    try:
        await cur.execute(sql, params)
        await cur.execute(f"RELEASE SAVEPOINT {name}")
        return True
    except psycopg.Error as e:
        await cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
        log.warning("%s skipped: %s", name, str(e).splitlines()[0][:200])
        return False


async def apply(ev: dict[str, Any]) -> dict[str, Any]:
    p = await pool()
    async with p.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                return await apply_in(cur, ev)


async def apply_in(cur: psycopg.AsyncCursor, ev: dict[str, Any]) -> dict[str, Any]:
    tenant, job = ev["tenant_id"], ev["job_id"]
    await cur.execute("SELECT set_config('replicant.tenant_id', %s, true)", (tenant,))
    stats = {"timeline": 0, "job": False}
    for r in mapping.timeline_rows(ev):
        cols = ["job_id", "seq", "tenant_id", "type", "at", "summary", "actor", "refs"]
        # job_timeline is RANGE-partitioned on job_id; a missing partition must not lose the snapshot
        if await _savepoint(cur, "timeline", f"INSERT INTO jobs_read.job_timeline ({', '.join(cols)}) VALUES "
                            f"({', '.join(['%s'] * len(cols))}) ON CONFLICT DO NOTHING", _vals(r, cols)):
            stats["timeline"] += 1
    for a in mapping.audit_events(ev):  # per timeline item, whatever the snapshot's seq (idempotency_key dedupes)
        await _savepoint(cur, "audit_emit", "SELECT audit.emit(%s)", (Jsonb(a),))
    await cur.execute("SELECT seq, cardinality(repo_ids) FROM jobs_read.jobs WHERE job_id = %s", (job,))
    prev = await cur.fetchone()
    repo_ids = await _repo_ids(cur, job) if not prev or not prev[1] else None
    row = mapping.job_row(ev, repo_ids=repo_ids)
    if prev is not None and prev[0] >= row["seq"]:
        return {**stats, "stale": True}  # seq guard: an older or duplicate snapshot changes nothing
    await cur.execute(_upsert_sql("jobs_read.jobs", JOB_COLS, "job_id", keep=KEEP_ON_UPDATE, coalesce=COALESCE_ON_UPDATE),
                      _vals(row, JOB_COLS))
    stats["job"] = True
    for r in mapping.run_rows(ev):
        await cur.execute(_upsert_sql("jobs_read.job_runs", RUN_COLS, "run_id"), _vals(r, RUN_COLS))
    gates, elig, decs = mapping.gate_rows(ev)
    for g in gates:
        await cur.execute(_upsert_sql("approvals.gates", GATE_COLS, "gate_id"), _vals(g, GATE_COLS))
    for e in elig:
        await cur.execute("INSERT INTO approvals.gate_eligibility (gate_id, tenant_id, kind, value, since) VALUES "
                          "(%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                          (e["gate_id"], e["tenant_id"], e["kind"], e["value"], e["since"]))
    for d in decs:
        sets = ("outcome = excluded.outcome, text_key = COALESCE(excluded.text_key, approvals.gate_decisions.text_key), "
                "text_sha256 = COALESCE(excluded.text_sha256, approvals.gate_decisions.text_sha256)")
        await cur.execute(f"INSERT INTO approvals.gate_decisions ({', '.join(DEC_COLS)}) VALUES "
                          f"({', '.join(['%s'] * len(DEC_COLS))}) ON CONFLICT (decision_id) DO UPDATE SET {sets}",
                          _vals(d, DEC_COLS))
    pr = mapping.pr_row(ev, row["repo_ids"] or None)
    if pr:
        await cur.execute(
            f"INSERT INTO jobs_read.job_prs ({', '.join(PR_COLS)}) VALUES ({', '.join(['%s'] * len(PR_COLS))}) "
            "ON CONFLICT (repo_id, pr_number) DO UPDATE SET head_sha = COALESCE(excluded.head_sha, jobs_read.job_prs.head_sha), "
            "state = excluded.state, url = excluded.url, job_open = excluded.job_open, "
            "merged_at = COALESCE(jobs_read.job_prs.merged_at, excluded.merged_at), "
            "closed_at = COALESCE(jobs_read.job_prs.closed_at, excluded.closed_at)", _vals(pr, PR_COLS))
    if row["memory_snapshot_id"]:
        await cur.execute("INSERT INTO jobs_read.job_memory_usage (job_id, tenant_id, memory_snapshot_id, first_seen_seq) "
                          "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                          (job, tenant, row["memory_snapshot_id"], row["seq"]))
    for m in mapping.steering_rows(ev):
        await cur.execute("INSERT INTO jobs_read.job_steering (message_id, job_id, tenant_id, actor, outcome, run_id, at) "
                          "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (message_id) DO UPDATE SET "
                          "outcome = excluded.outcome, run_id = COALESCE(excluded.run_id, jobs_read.job_steering.run_id)",
                          (m["message_id"], m["job_id"], m["tenant_id"], m["actor"], m["outcome"], m["run_id"], m["at"]))
    await cur.execute("SELECT pg_notify('replicant_job_updates', %s)", (f"{tenant}:{job}:{row['seq']}",))
    return stats


async def ready() -> bool:
    p = await pool()
    async with p.connection() as conn:
        await conn.execute("SELECT 1 FROM jobs_read.jobs LIMIT 0")
    return True


def dumps(v: Any) -> str:
    return json.dumps(v, default=str)
