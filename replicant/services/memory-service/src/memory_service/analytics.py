"""Memory analytics reports (admin): per-entry outcomes, feedback recurrence and a summary."""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memory_service.auth import HttpError
from memory_service.db import Database, rows
from memory_service.store import _jsonable


async def analytics(db: Database, report: str, q: dict[str, Any]) -> dict[str, Any]:
    async with db.tx(admin=True) as c:
        if report == "entries":
            # per active entry: outcomes of jobs on the same repo with and without the entry injected
            res = rows(await c.execute(text("""
              WITH exp AS (SELECT DISTINCT split_part(r, '@', 1) AS entry_id, i.job_id
                             FROM memory.injections i, unnest(i.injected_refs) r)
              SELECT e.entry_id, e.repo, e.memory_type, e.injection_count, v.statement,
                     count(o.job_id) FILTER (WHERE x.job_id IS NOT NULL) AS jobs_with,
                     count(o.job_id) FILTER (WHERE x.job_id IS NULL) AS jobs_without,
                     avg(o.changes_requested_reviews) FILTER (WHERE x.job_id IS NOT NULL)::float AS changes_requested_with,
                     avg(o.changes_requested_reviews) FILTER (WHERE x.job_id IS NULL)::float AS changes_requested_without,
                     avg(o.review_rounds) FILTER (WHERE x.job_id IS NOT NULL)::float AS review_rounds_with,
                     avg(o.review_rounds) FILTER (WHERE x.job_id IS NULL)::float AS review_rounds_without,
                     avg(o.first_pass_qa::int) FILTER (WHERE x.job_id IS NOT NULL)::float AS first_pass_qa_with,
                     avg(o.first_pass_qa::int) FILTER (WHERE x.job_id IS NULL)::float AS first_pass_qa_without
                FROM memory.entries e JOIN memory.entry_versions v ON v.entry_id = e.entry_id AND v.version = e.current_version
                LEFT JOIN memory.job_outcomes o ON o.tenant_id = e.tenant_id AND o.repo = e.repo
                LEFT JOIN exp x ON x.entry_id = e.entry_id AND x.job_id = o.job_id
               WHERE e.state = 'active' GROUP BY e.entry_id, e.repo, e.memory_type, e.injection_count, v.statement
               ORDER BY e.injection_count DESC LIMIT 200""")))
            return {"report": report, "rows": _jsonable(res)}
        if report == "feedback":
            res = rows(await c.execute(text("""
              SELECT date_trunc('week', decided_at) AS week, split_part(source_key, ':', 1) AS source, count(*) AS items,
                     count(*) FILTER (WHERE decision = 'evidence_merged') AS repeated,
                     count(*) FILTER (WHERE decision IN ('candidate_created', 'conflict_opened')) AS new_candidates,
                     count(*) FILTER (WHERE decision LIKE 'blocked_%') AS blocked
                FROM memory.feedback_items GROUP BY 1, 2 ORDER BY 1 DESC, 2""")))
            for r in res:
                r["repeated_rate"] = round(r["repeated"] / r["items"], 4) if r["items"] else None
            return {"report": report, "rows": _jsonable(res)}
        if report == "summary":
            counts = rows(await c.execute(text("SELECT state, count(*) AS n FROM memory.entries GROUP BY state")))
            inj = rows(await c.execute(text("""SELECT count(*) AS runs, count(DISTINCT job_id) AS jobs,
                avg(cardinality(injected_refs))::float AS avg_entries FROM memory.injections""")))
            outc = rows(await c.execute(text("""SELECT count(*) AS jobs, avg(review_rounds)::float AS avg_review_rounds,
                avg(changes_requested_reviews)::float AS avg_changes_requested, avg(first_pass_qa::int)::float AS first_pass_qa_rate
                FROM memory.job_outcomes""")))
            return {"report": report, "states": {r["state"]: r["n"] for r in counts}, "injections": _jsonable(inj[0]),
                    "outcomes": _jsonable(outc[0])}
    raise HttpError(404, "unknown_report", report)
