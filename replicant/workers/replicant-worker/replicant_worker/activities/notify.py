"""notify.* activities -> control-plane-api `POST /internal/v1/notifications` (notifications.md N-1), which resolves
recipients and fans out to channels (the GitHub/Gitea status comment goes through github-integration). Each call
carries a dedupe key (the activity's idempotency key), so retries never double-post.

M1: if control-plane-api has no notification route or refuses the worker's caller token (401/403/404), the
notification is dropped with a warning rather than holding the job: notify.* are fire-and-forget (C-MSG)."""
from __future__ import annotations

from typing import Any

import httpx
from temporalio import activity

from replicant_worker.activities.base import implements, http, idempotency_key, retryable_http
from replicant_worker.activities.config import read_secret, settings


async def post_notification(body: dict[str, Any]) -> dict[str, Any]:
    s = settings()
    tok = read_secret(s.control_plane_token_file)
    headers = {"Idempotency-Key": f'"{body["dedupe_key"]}"'}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        r = await http().post(f"{s.control_plane_url}/internal/v1/notifications", json=body, headers=headers)
    except httpx.ConnectError as e:
        if s.local:
            activity.logger.warning("control-plane-api unreachable; notification dropped (local)", extra={"error": str(e)[:100]})
            return {"delivered": False, "reason": "unreachable"}
        raise
    if r.status_code in (200, 201, 202, 204, 409):
        return {"delivered": True}
    if r.status_code in (400, 401, 403, 404, 405, 422):
        activity.logger.warning("notification refused", extra={"status": r.status_code, "event_key": body["event_key"]})
        return {"delivered": False, "reason": f"http_{r.status_code}"}
    raise retryable_http("POST /internal/v1/notifications", r)


def _body(event_key: str, a: dict[str, Any], **extra: Any) -> dict[str, Any]:
    b: dict[str, Any] = {"dedupe_key": idempotency_key()[:256], "event_key": event_key,
                         "tenant_id": a.get("tenant_id") or _tenant(a)}
    for k in ("job_id", "gate_id"):
        if a.get(k):
            b[k] = a[k]
    b.update(extra)
    return b


def _tenant(a: dict[str, Any]) -> str:
    from replicant_temporal.interceptors import current_context
    c = current_context()
    return (c.tenant_id if c and c.tenant_id else None) or "unknown"


def _vars(a: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: a[k] for k in keys if a.get(k) is not None}


@implements("notify.gate_opened")
async def notify_gate_opened(a: dict[str, Any]) -> dict[str, Any]:
    return await post_notification(_body("gate.opened", a, recipients={"roles": ["eligible_approvers", "requester"]},
                                         vars=_vars(a, "kind", "channels", "packet_uri", "banner", "presented_digest",
                                                   "expires_at", "opened_at")))


@implements("notify.gate_reminder")
async def notify_gate_reminder(a: dict[str, Any]) -> dict[str, Any]:
    return await post_notification(_body("gate.reminder", a, recipients={"roles": ["eligible_approvers"]},
                                         vars=_vars(a, "kind", "n", "final")))


@implements("notify.gate_escalated")
async def notify_gate_escalated(a: dict[str, Any]) -> dict[str, Any]:
    return await post_notification(_body("gate.escalated", a, recipients={"roles": ["eligible_approvers", "tenant_admins"]},
                                         vars=_vars(a, "kind", "level")))


@implements("notify.gate_resolved")
async def notify_gate_resolved(a: dict[str, Any]) -> dict[str, Any]:
    d = a.get("decision") or {}
    return await post_notification(_body("gate.resolved", a, recipients={"roles": ["requester"]},
                                         vars={"decision": d.get("decision"), "channel": d.get("channel"),
                                               **{k: d[k] for k in ("principal_id", "decided_at") if d.get(k)},
                                               **({"state": a["state"]} if a.get("state") else {})}))


@implements("notify.job_event")
async def notify_job_event(a: dict[str, Any]) -> dict[str, Any]:
    kind = str(a.get("kind") or "event").replace("-", "_")
    rec = {"principals": [a["to"]]} if a.get("to") else {"roles": ["requester"]}
    return await post_notification(_body(f"job.{kind}", a, recipients=rec,
                                         vars=_vars(a, "gate_kind", "note", "pr", "days", "outcome", "reason", "title")))


@implements("notify.operator")
async def notify_operator(a: dict[str, Any]) -> dict[str, Any]:
    b = {k: a[k] for k in ("dedupe_key", "event_key", "tenant_id", "job_id", "recipients", "vars", "operator") if k in a}
    return await post_notification(b)
