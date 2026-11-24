"""In-process calls between the modules of the `core` process (SIMPLIFY Phase 2, DEVIATIONS SIMPLIFY-D10).

github-integration, slack-integration and control-plane-api run in one process. Their calls to each other keep the
HTTP contract they had as separate services (the same routes, validation and authorization) but are dispatched to the
callee's ASGI app in process through `httpx.ASGITransport`, never over the network. The caller's service principal
travels in the ASGI scope under `IN_PROCESS_CALLER`: uvicorn builds the scope from the HTTP request and never sets
that key, so no network client can claim it, and no caller token is needed.

* `caller(app, principal_id)` wraps a callee app so every request carries the caller principal.
* `client(apps, url)` gives an AsyncClient for `url`: in process when its host names a module app of this process
  (`github-integration`, `slack-integration`: the old service names, so configured URLs keep working), else over
  the network.
"""
from __future__ import annotations

from typing import Any

import httpx

IN_PROCESS_CALLER = "replicant.in_process_caller"   # same literal in github_integration.app, slack_integration.app


class InProcessCaller:
    """ASGI wrapper: marks every HTTP request to `app` as coming from service principal `principal_id`."""

    def __init__(self, app: Any, principal_id: str) -> None:
        self.app = app
        self.principal_id = principal_id

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope = {**scope, IN_PROCESS_CALLER: self.principal_id}
        await self.app(scope, receive, send)


def caller(app: Any, principal_id: str) -> InProcessCaller:
    return InProcessCaller(app, principal_id)


def transport(app: Any, principal_id: str) -> httpx.AsyncBaseTransport:
    # raise_app_exceptions=False: an unhandled error in the callee is a 500, like it was over the network
    return httpx.ASGITransport(app=caller(app, principal_id), raise_app_exceptions=False)


def client(apps: dict[str, Any] | None, url: str, **kw: Any) -> httpx.AsyncClient:
    """`apps` maps a host name to an already wrapped (`caller()`) module app (`app.state.in_process`)."""
    app = (apps or {}).get(httpx.URL(url).host)
    if app is not None:
        kw["transport"] = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(**kw)
