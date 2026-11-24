"""The `core` process (SIMPLIFY Phase 2, DEVIATIONS SIMPLIFY-D10..D13): `control-plane-api serve`.

One process, one asyncio loop:

* listener REPLICANT_HTTP_PORT (8401): control-plane-api, plus the ingress routes of github-integration
  (`/webhooks/gitea`, `/github/webhook`, `/v1/admin/scm-installations`) and, when Slack is enabled, of
  slack-integration (`/slack/events|interactions|commands`). Those routes authenticate requests themselves (webhook
  HMAC, Slack signatures, dev seed token), as they did in their own services.
* background: control-plane-api's loops, github-integration's consumer and inventory scanner, slack-integration's
  auth check and decision retrier, and the replicant-projector Temporal worker (task queue replicant-projections).

Each module keeps its own database login and pool (REPLICANT_<MODULE>_DB_DSN / _PASSWORD_FILE), so the
least-privilege split between them is the database's, as before: only the projector's login writes jobs_read and
approvals. Calls between modules go through in_process.py (their HTTP contracts, dispatched in process).

Modules: github-integration runs when REPLICANT_GITHUB_INTEGRATION_DB_DSN is set, the projector when
REPLICANT_PROJECTOR_DB_DSN is set; REPLICANT_<GITHUB_INTEGRATION|PROJECTOR>_ENABLED=true|false overrides. There is no
run-grants listener any more (SIMPLIFY-D50): the Runner signs run tokens and asks core for a job's run scope and
revocations on the internal routes of routes_runner.py. Slack is opt-in:
REPLICANT_SLACK_ENABLED=true (with REPLICANT_SLACK_INTEGRATION_DB_DSN); while it is off, Slack deliveries are recorded
as suppressed (`slack_disabled`).
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import signal
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI

from control_plane_api import in_process
from control_plane_api.app import check_route_permissions, create_app
from control_plane_api.config import Settings, env

log = logging.getLogger("control_plane_api.core")
CP_PRINCIPAL = "svc_control-plane-api"


def _on(name: str, configured: bool) -> bool:
    v = env(f"REPLICANT_{name}_ENABLED")
    return configured if v is None else v == "true"


@dataclass(frozen=True)
class CoreModules:
    github: bool = False
    slack: bool = False
    projector: bool = False

    @classmethod
    def from_env(cls) -> "CoreModules":
        return cls(github=_on("GITHUB_INTEGRATION", bool(env("REPLICANT_GITHUB_INTEGRATION_DB_DSN"))),
                   slack=env("REPLICANT_SLACK_ENABLED", "false") == "true",
                   projector=_on("PROJECTOR", bool(env("REPLICANT_PROJECTOR_DB_DSN"))))


def _module_db(prefix: str) -> dict[str, str | None]:
    dsn = env(f"REPLICANT_{prefix}_DB_DSN")
    if not dsn:
        raise SystemExit(f"REPLICANT_{prefix}_DB_DSN is required when the module runs in core")
    return {"db_dsn": dsn, "db_password_file": env(f"REPLICANT_{prefix}_DB_PASSWORD_FILE")}


def _github(app: FastAPI, settings: Settings) -> FastAPI:
    from github_integration.app import create_app as create_github
    from github_integration.clients import ControlPlane
    from github_integration.config import Settings as GithubSettings
    gs = dataclasses.replace(GithubSettings.from_env(), **_module_db("GITHUB_INTEGRATION"),
                             control_plane_token_file=None, notify_caller_token_file=None)
    app.state.auth.allow_in_process("svc_github-integration")
    cp = ControlPlane(gs, transport=in_process.transport(app, "svc_github-integration"))
    return create_github(gs, cp=cp, run_consumer=settings.background_tasks and gs.role in ("all", "consumer"))


def _slack(app: FastAPI) -> FastAPI:
    from slack_integration.app import create_app as create_slack
    from slack_integration.config import Settings as SlackSettings
    ss = dataclasses.replace(SlackSettings.from_env(), **_module_db("SLACK_INTEGRATION"),
                             control_plane_token_file=None, notify_caller_token_file=None)
    app.state.auth.allow_in_process("svc_slack-integration")
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)   # it logs request bodies at DEBUG
    return create_slack(ss, cp_transport=in_process.transport(app, "svc_slack-integration"))


def create_core(settings: Settings | None = None, modules: CoreModules | None = None) -> FastAPI:
    """-> the API app with the modules attached."""
    settings = settings or Settings.from_env()
    m = modules or CoreModules.from_env()
    app = create_app(settings)
    subapps: dict[str, FastAPI] = {}
    if m.github:
        subapps["github-integration"] = _github(app, settings)
    if m.slack:
        subapps["slack-integration"] = _slack(app)
    for name, sub in subapps.items():
        app.include_router(sub.state.ingress_router)
        app.state.in_process[name] = in_process.caller(sub, CP_PRINCIPAL)   # host name of the old service URL
    app.state.notify_loops.in_process = app.state.in_process
    app.state.notify_loops.slack_enabled = m.slack
    app.state.core_modules = m
    check_route_permissions(app)

    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a: FastAPI):  # noqa: ANN202
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(inner(a))
            for sub in subapps.values():           # their own loops (consumer, scanner, Slack auth and retrier)
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            if m.projector and settings.background_tasks:
                from replicant_projector.cli import serve_in_process
                task = asyncio.create_task(serve_in_process())
                stack.push_async_callback(_cancel, task)
            yield

    app.router.lifespan_context = lifespan
    log.info("core modules: github-integration=%s slack-integration=%s projector=%s", m.github, m.slack, m.projector)
    return app


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


class _Server(uvicorn.Server):
    """Without uvicorn's own signal capture: with two servers in one loop each would replace the other's handler, so
    `serve()` installs one handler that stops both."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def serve(listeners: list[tuple[Any, int]]) -> int:
    """Serve each (app, port) until SIGTERM/SIGINT or until one listener stops (then all stop)."""
    servers = [_Server(uvicorn.Config(a, host="0.0.0.0", port=p, log_config=None, access_log=False))
               for a, p in listeners]

    def stop() -> None:
        for s in servers:
            s.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop)
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    stop()
    await asyncio.gather(*tasks, return_exceptions=True)
    return 0 if all(s.started for s in servers) else 3     # 3: uvicorn's startup-failure exit code
