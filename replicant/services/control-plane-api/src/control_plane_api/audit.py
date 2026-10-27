"""audit.emit() inside the business transaction (WS-08 audit.md §3, C-PG rule 3).

M1: when WS-08's `audit.emit(jsonb)` is not migrated (PLATFORM may not ship 08's DDL in v0) the event is logged
with `log_type=audit` and an `aud_<ulid>` is minted locally so callers that must record an audit id
(registry.bundle_promotion_events) still can. A failing emit runs inside a SAVEPOINT and is logged, so an M1 catalog
mismatch never blocks the business change (recorded in DEVIATIONS)."""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from control_plane_api.ids import ulid

log = logging.getLogger("replicant.audit")


AUDIT_CHANNELS = {"ui", "slack", "github", "cli", "api", "system", "okta"}


async def emit(conn: AsyncConnection, db: Any, event: dict[str, Any]) -> str:
    """Platform-scope events use tenant `replicant-system`; channels outside the audit enum map to `system`."""
    ev = {"occurred_at": dt.datetime.now(dt.UTC).isoformat(), "source_service": "control-plane-api",
          "tenant_id": "replicant-system", "channel": "api", **event}
    if ev.get("tenant_id") is None:
        ev["tenant_id"] = "replicant-system"
    if ev.get("channel") not in AUDIT_CHANNELS:
        ev["channel"] = "system"
    ev = {k: v for k, v in ev.items() if v is not None}
    if await db.has_function("audit.emit"):
        try:
            async with conn.begin_nested():
                r = await conn.execute(text("SELECT audit.emit(CAST(:e AS jsonb))"), {"e": json.dumps(ev)})
                aid = r.scalar_one()
                log.info(json.dumps({"log_type": "audit", "audit_event_id": aid, **_safe(ev)}))
                return aid
        except Exception as e:  # noqa: BLE001
            log.warning("audit.emit failed (%s: %s); event logged only", type(e).__name__, str(e).splitlines()[0][:200])
    aid = ulid("aud")
    log.info(json.dumps({"log_type": "audit", "audit_event_id": aid, "persisted": False, **_safe(ev)}))
    return aid


def _safe(ev: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in ev.items() if k in ("event_type", "outcome", "reason", "tenant_id", "actor_principal_id",
                                                  "job_id", "gate_id", "decision_id", "release_bundle_id",
                                                  "target_kind", "target_id")}
