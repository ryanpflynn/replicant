"""Activity registration helpers and HTTP plumbing shared by the v0 activity implementations."""
from __future__ import annotations

import logging
from typing import Any, Callable

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = logging.getLogger("replicant_worker.activities")
REGISTRY: dict[str, Callable[..., Any]] = {}


def implements(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register an activity implementation under its catalog name (activity_options.CATALOG)."""
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in REGISTRY:
            raise ValueError(f"duplicate activity {name}")
        wrapped = activity.defn(name=name)(fn)
        REGISTRY[name] = wrapped
        return wrapped
    return deco


def info_ids() -> tuple[str, str]:
    """(workflow_id, activity_id) of the current activity; ("", "") outside an activity."""
    try:
        i = activity.info()
        return i.workflow_id or "", i.activity_id or ""
    except RuntimeError:
        return "", ""


def idempotency_key() -> str:
    wf, aid = info_ids()
    return f"ik1:{wf}:{aid}"


def parse_lid(activity_id: str) -> tuple[str, int, int, str] | None:
    """`<node>.c<cycle>.n<ordinal>.<effect>` -> parts, None for default (numeric) activity IDs."""
    parts = activity_id.split(".")
    if len(parts) != 4 or not parts[1].startswith("c") or not parts[2].startswith("n"):
        return None
    try:
        return parts[0], int(parts[1][1:]), int(parts[2][1:]), parts[3]
    except ValueError:
        return None


_http: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))
    return _http


def set_http(c: httpx.AsyncClient | None) -> None:  # tests
    global _http
    _http = c


def problem(resp: httpx.Response) -> dict[str, Any]:
    try:
        d = resp.json()
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def retryable_http(what: str, resp: httpx.Response) -> ApplicationError:
    p = problem(resp)
    ra = resp.headers.get("Retry-After")
    from datetime import timedelta
    kw: dict[str, Any] = {}
    if ra and ra.isdigit():
        kw["next_retry_delay"] = timedelta(seconds=min(int(ra), 300))
    return ApplicationError(f"{what}: HTTP {resp.status_code} {p.get('code') or ''}".strip(), p,
                            type=str(p.get("code") or f"http_{resp.status_code}"), **kw)


def not_in_v0(name: str) -> Callable[..., Any]:
    async def _fn(arg: Any) -> Any:
        raise ApplicationError(f"{name} is not part of the v0 bugfix path", type="NotImplementedInV0", non_retryable=True)
    _fn.__name__ = name.replace(".", "_")
    return activity.defn(name=name)(_fn)
