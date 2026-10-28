"""Human-action outbox (C-MSG §3; architecture.md §2.4): persist `intake.pending_actions`, then
`execute_update(id=<action_ref>, rpc_timeout=10 s)`. On a result: `done`; on a permanent rejection: `rejected`;
on timeout/unavailable: stay `pending|retry` and answer 202 {state: recording}. The retrier (every 2 s, exponential
backoff to 2 min, advisory-lock singleton) re-sends with the same Update ID."""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from control_plane_api.db import Database
from control_plane_api.locks import locked_loop
from control_plane_api.problems import ProblemError
from control_plane_api.temporal_client import TemporalGateway, UpdateOutcome

log = logging.getLogger(__name__)

OUTBOX_LOCK = (0x46414354, 2)
MAX_BACKOFF_S = 120


@dataclass
class ActionResult:
    state: str                         # done | rejected | recording
    result: dict[str, Any] | None = None
    error_type: str | None = None
    message: str | None = None
    details: list[Any] | None = None
    action_ref: str | None = None


class Outbox:
    def __init__(self, db: Database, temporal: TemporalGateway) -> None:
        self.db = db
        self.temporal = temporal

    async def submit(self, *, action_ref: str, kind: str, workflow_id: str, tenant_id: str | None, actor: str,
                     payload: dict[str, Any]) -> ActionResult:
        async with self.db.tx(role="svc_cpa_intake", tenant=tenant_id) as conn:
            await conn.execute(text(
                "INSERT INTO intake.pending_actions (action_ref, kind, workflow_id, tenant_id, actor, payload) VALUES "
                "(:a, :k, :w, :t, :actor, CAST(:p AS jsonb)) ON CONFLICT (action_ref) DO NOTHING"),
                {"a": action_ref, "k": kind, "w": workflow_id, "t": tenant_id, "actor": actor, "p": json.dumps(payload)})
            row = (await conn.execute(text("SELECT state, result, payload, kind, workflow_id, actor FROM "
                                           "intake.pending_actions WHERE action_ref = :a"), {"a": action_ref})).first()
        if (row.kind, row.workflow_id, row.actor) != (kind, workflow_id, actor):
            # An idempotency key reused for a different action must not return (or replay) another action's outcome.
            raise ProblemError(409, "idempotency_key_reused")
        if row.state == "done":
            return ActionResult("done", result=row.result)
        if row.state in ("rejected", "dead", "abandoned"):
            r = row.result or {}
            return ActionResult("rejected", error_type=r.get("error_type"), message=r.get("message"),
                                details=r.get("details"))
        # re-submissions keep the stored payload (decided_at unchanged, XD-FIX-R2-014)
        return await self.attempt(action_ref, kind, workflow_id, tenant_id, row.payload)

    async def attempt(self, action_ref: str, kind: str, workflow_id: str, tenant_id: str | None,
                      payload: dict[str, Any]) -> ActionResult:
        out: UpdateOutcome = await self.temporal.execute_update(workflow_id, kind, payload, action_ref)
        if out.kind == "rejected" and out.error_type == "DuplicateDecision" and out.details:
            out = UpdateOutcome("result", result=out.details[0] if isinstance(out.details[0], dict) else None)
        async with self.db.tx(role="svc_cpa_intake", tenant=tenant_id) as conn:
            if out.kind == "result":
                await conn.execute(text("UPDATE intake.pending_actions SET state = 'done', result = CAST(:r AS jsonb), "
                                        "attempts = attempts + 1, completed_at = now(), last_error = NULL "
                                        "WHERE action_ref = :a"), {"r": json.dumps(out.result), "a": action_ref})
                await conn.execute(text("SELECT pg_notify('replicant_action_results', :p)"),
                                   {"p": f"{tenant_id or ''}:{action_ref}:done"})
                return ActionResult("done", result=out.result)
            if out.kind == "rejected":
                err = {"error_type": out.error_type, "message": (out.message or "")[:500], "details": out.details}
                await conn.execute(text("UPDATE intake.pending_actions SET state = 'rejected', result = CAST(:r AS jsonb), "
                                        "attempts = attempts + 1, completed_at = now(), last_error = :e "
                                        "WHERE action_ref = :a"),
                                   {"r": json.dumps(err, default=str), "e": out.error_type, "a": action_ref})
                return ActionResult("rejected", error_type=out.error_type, message=out.message, details=out.details)
            await conn.execute(text(
                "UPDATE intake.pending_actions SET state = 'retry', attempts = attempts + 1, last_error = :e, "
                "next_attempt_at = now() + make_interval(secs => least(:max, 2 * power(2, least(attempts, 7)))) "
                "WHERE action_ref = :a"), {"e": out.error_type, "a": action_ref, "max": MAX_BACKOFF_S})
        return ActionResult("recording", error_type=out.error_type)

    async def retry_due(self, limit: int = 50) -> int:
        # svc_cpa_intake is the outbox's sole writer and reads every tenant's rows (rls_pending_actions_sole_writer)
        async with self.db.tx(role="svc_cpa_intake") as conn:
            rows = (await conn.execute(text(
                "SELECT action_ref, kind, workflow_id, tenant_id, payload FROM intake.pending_actions WHERE state IN "
                "('pending', 'retry') AND next_attempt_at <= now() AND created_at < now() - interval '2 seconds' "
                "ORDER BY next_attempt_at LIMIT :n"), {"n": limit})).all()
        for r in rows:
            await self.attempt(r.action_ref, r.kind, r.workflow_id, r.tenant_id, r.payload)
        return len(rows)

    async def retrier_loop(self, interval_s: float = 2.0) -> None:
        await locked_loop(self.db, OUTBOX_LOCK, interval_s, self.retry_due, "outbox retrier")


def utcnow_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
