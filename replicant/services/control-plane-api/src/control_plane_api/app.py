"""control-plane-api ASGI app factory (architecture.md §2)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.routing import Route

from control_plane_api import routes_admin, routes_gates, routes_jobs, routes_notify, routes_registry, routes_web
from control_plane_api import routes_graph
from control_plane_api.auth import AuthContext
from control_plane_api.config import Settings
from control_plane_api.db import Database
from control_plane_api.intake import Intake
from control_plane_api.notify import NotifyService
from control_plane_api.notify_loops import NotifyLoops
from control_plane_api.notify_policy import NotifySettings
from control_plane_api.outbox import Outbox
from control_plane_api.problems import ProblemError, problem_handler, request_id, validation_handler
from control_plane_api.s3 import S3
from control_plane_api.temporal_client import TemporalGateway

log = logging.getLogger(__name__)
PUBLIC_PATHS = {"/healthz", "/readyz"}


def _permission_of(route: Route) -> str | None:
    dependant = getattr(route, "dependant", None)
    stack = list(dependant.dependencies) if dependant else []
    while stack:
        d = stack.pop()
        perm = getattr(d.call, "__replicant_permission__", None)
        if perm:
            return perm
        stack.extend(d.dependencies)
    return None


def iter_routes(routes: list[Any]) -> list[Any]:
    """Flattens FastAPI >= 0.16x included routers (`_IncludedRouter.original_router`)."""
    out: list[Any] = []
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            out.extend(iter_routes(list(inner.routes)))
        else:
            out.append(r)
    return out


def check_route_permissions(app: FastAPI) -> None:
    """Startup check (architecture.md §2.2): every route declares a permission, except the public ones."""
    missing = []
    for r in iter_routes(list(app.routes)):
        path = getattr(r, "path", "")
        if path in PUBLIC_PATHS or not hasattr(r, "dependant") or path.startswith(("/docs", "/openapi", "/redoc")):
            continue
        if _permission_of(r) is None:
            missing.append(path)
    if missing:
        raise SystemExit(f"routes without a permission: {missing}")


def create_app(settings: Settings | None = None, *, db: Database | None = None,
               temporal: TemporalGateway | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.startup_guard()
    db = db or Database(settings)
    temporal = temporal or TemporalGateway(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        tasks: list[asyncio.Task[Any]] = []
        if settings.background_tasks:
            tasks.append(asyncio.create_task(app.state.intake.retrier_loop()))
            tasks.append(asyncio.create_task(app.state.outbox.retrier_loop()))
            import os

            from control_plane_api.registry import WORKER_QUEUES_DEFAULT, worker_registrar_loop
            queues = tuple(q for q in os.environ.get("REPLICANT_WORKER_READY_QUEUES", ",".join(WORKER_QUEUES_DEFAULT))
                           .split(",") if q)
            tasks.append(asyncio.create_task(worker_registrar_loop(db, temporal, queues)))
            if os.environ.get("REPLICANT_NOTIFY_LOOPS", "true") == "true":
                tasks.append(asyncio.create_task(app.state.notify_loops.dispatcher_loop()))
                tasks.append(asyncio.create_task(app.state.notify_loops.scheduler_loop()))
        yield
        for t in tasks:
            t.cancel()
        await db.dispose()

    app = FastAPI(title="control-plane-api", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.temporal = temporal
    app.state.s3 = S3(settings)
    app.state.auth = AuthContext(settings)
    app.state.intake = Intake(db, temporal, app.state.auth, settings.replicant_env, settings.web_origin)
    app.state.outbox = Outbox(db, temporal)
    app.state.notify = NotifyService(db, app.state.auth, NotifySettings.from_env(settings.web_origin))
    app.state.notify_loops = NotifyLoops(app.state.notify)
    app.add_exception_handler(ProblemError, problem_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_handler)  # type: ignore[arg-type]

    @app.middleware("http")
    async def request_id_mw(request: Request, call_next):  # noqa: ANN001, ANN202
        rid = request_id(request)
        resp = await call_next(request)
        resp.headers["X-Request-Id"] = rid
        return resp

    app.include_router(routes_jobs.router)
    app.include_router(routes_gates.router)
    app.include_router(routes_web.router)
    app.include_router(routes_notify.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_registry.router)
    app.include_router(routes_graph.router)  # GRAPH-01: job graph + run activity reads

    try:
        import replicant_dev_auth
    except ImportError:
        replicant_dev_auth = None  # prod image: no dev routes, no seed token (identity.md §9.3 L-1)
    if replicant_dev_auth is not None:
        replicant_dev_auth.install(app, app.state.auth, settings)
    elif settings.dev_seed_token_file:
        log.warning("REPLICANT_DEV_SEED_TOKEN_FILE is set but replicant_dev_auth is not installed (prod image); ignored")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        # readiness requires the database; Temporal reachability is reported (degraded mode allows reads, §10)
        db_ok = await db.ping()
        t_ok = await temporal.reachable() if db_ok else False
        body = {"status": "ok" if db_ok else "unavailable", "db": db_ok, "temporal": t_ok}
        return JSONResponse(body, status_code=200 if db_ok else 503)

    check_route_permissions(app)
    return app
