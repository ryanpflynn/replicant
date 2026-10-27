"""replicant-worker entrypoint (XD-11-01, XD-11-13).

    replicant-worker run                           poll replicant-jobs, replicant-runner-calls, replicant-llm-calls
    replicant-worker healthcheck                   exit 0 if this version has pollers on its queues (and is current
                                                 when REPLICANT_WORKER_SET_CURRENT_ON_START=true)
    replicant-worker nodecatalog export            print this build's NodeCatalog
    replicant-worker local-bundle spec             print the v0 ReleaseBundle `spec` for REPLICANT_WORKER_BUILD_ID
    replicant-worker local-bundle export --out D   write registry-layout blobs + bundle-spec.json to D
    replicant-worker local-bundle publish          write the v0 definition blobs to the local registry bucket
    replicant-worker grant-jwks                    print the public JWK Set of REPLICANT_RUN_GRANT_KEY_FILE

Configuration (env, names from spec/11-local-dev compose.yaml):
    REPLICANT_ENV, REPLICANT_TEMPORAL_ADDRESS, REPLICANT_TEMPORAL_NAMESPACE, REPLICANT_TEMPORAL_TLS (+ cert files),
    REPLICANT_WORKER_BUILD_ID (b<YYYYMMDD>-<sha12>), REPLICANT_WORKER_IMAGE_DIGEST,
    REPLICANT_WORKER_SET_CURRENT_ON_START (local/dev only),
    REPLICANT_LLM_ACTIVITIES_PER_SECOND (default 20), REPLICANT_MAX_CACHED_WORKFLOWS (default 2000),
    REPLICANT_RUNNER_URL / _TOKEN_FILE, REPLICANT_GATEWAY_URL / REPLICANT_GATEWAY_SERVICE_TOKEN_FILE,
    REPLICANT_RUN_GRANT_KEY_FILE / _KID / _ISSUER, REPLICANT_S3_*, REPLICANT_CONTROL_PLANE_URL (+ _TOKEN_FILE),
    REPLICANT_GATEWAY_CASSETTE_SET, REPLICANT_GATEWAY_SCRIPT_SET (+ _SCRIPT_KEY), REPLICANT_LOCAL_REGISTRY_PUBLISH.

The payload codec is not installed in v0 (plan.md §1.3, deferred to P2).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pathlib
import sys
from typing import Any

from replicant_worker import build_config

DEPLOYMENT = build_config.DEPLOYMENT_NAME

# Imported once per process, not per workflow run. They hold no per-run mutable state that Replicant
# relies on (temporal-langgraph.md section 5, rule D-7).
PASSTHROUGH = ("typing_extensions", "pydantic", "pydantic_core", "replicant_worker.build_config", "replicant_temporal")


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise SystemExit(f"missing required environment variable {name}")
    return v


def setup_logging() -> None:
    from replicant_temporal.interceptors import ContextLogFilter

    class Json(logging.Formatter):
        def format(self, r: logging.LogRecord) -> str:
            d = {"ts": self.formatTime(r), "level": r.levelname.lower(), "logger": r.name, "msg": r.getMessage()}
            for k in ("job_id", "tenant_id", "run_id"):
                if getattr(r, k, None):
                    d[k] = getattr(r, k)
            try:  # trace_id / span_id of the current activity span (replicant-telemetry, M2)
                from replicant_telemetry import log_context
                d.update({k: v for k, v in log_context(r).items() if k not in d})
            except Exception:  # noqa: BLE001 - logging must never fail
                pass
            if r.exc_info:
                d["exc"] = self.formatException(r.exc_info)[-2000:]
            return json.dumps(d)

    from replicant_telemetry.redact import RedactingFormatter
    h = logging.StreamHandler(sys.stdout)
    h.addFilter(ContextLogFilter())
    h.setFormatter(RedactingFormatter(Json() if os.environ.get("REPLICANT_LOG_FORMAT", "json") == "json" else
                                      logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")))
    logging.basicConfig(level=os.environ.get("REPLICANT_LOG_LEVEL", "INFO"), handlers=[h], force=True)


_RUNTIME = None


def _runtime() -> Any:
    """SDK metrics on Prometheus with the global tag `deployment_version` (XD-FIX-R1-195)."""
    global _RUNTIME
    if _RUNTIME is None:
        from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig
        bind = os.environ.get("REPLICANT_METRICS_BIND", "0.0.0.0:9464")
        tags = {"deployment_version": f"{DEPLOYMENT}:{os.environ.get('REPLICANT_WORKER_BUILD_ID', 'unknown')}"}
        _RUNTIME = Runtime(telemetry=TelemetryConfig(metrics=PrometheusConfig(bind_address=bind), global_tags=tags)) \
            if bind != "off" else Runtime(telemetry=TelemetryConfig(global_tags=tags))
    return _RUNTIME


async def connect() -> Any:
    from replicant_temporal import TemporalSettings
    from replicant_temporal import connect as fconnect
    return await fconnect(TemporalSettings.from_env(identity=f"{DEPLOYMENT}@{os.uname().nodename}"), runtime=_runtime())


async def export_pinned_open_workflows(client: Any, build: str) -> None:
    """Gauge replicant_worker_pinned_open_workflows (XD-FIX-R1-195), from visibility every 60 s."""
    gauge = _runtime().metric_meter.create_gauge("replicant_worker_pinned_open_workflows",
                                                 "open workflows pinned to this worker build")
    q = f"TemporalWorkerDeploymentVersion = '{DEPLOYMENT}:{build}' AND ExecutionStatus = 'Running'"
    while True:
        try:
            gauge.set((await client.count_workflows(q)).count)
        except Exception:  # noqa: BLE001 - visibility lag or a transient error; try again next minute
            pass
        await asyncio.sleep(60)


def build_workers(client: Any, activities_by_queue: dict[str, list[Any]], build: str) -> list[Any]:
    from temporalio.worker import Worker
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

    from replicant_temporal.versioning import deployment_config
    from replicant_worker.activities.options import Q_JOBS, Q_LLM, Q_RUNNER
    from replicant_worker.workflows.replicant_job import ReplicantJob
    from replicant_worker.workflows.maintenance import MaintenanceBatch, MaintenanceShard

    dep = deployment_config(DEPLOYMENT, build)
    runner = SandboxedWorkflowRunner(restrictions=SandboxRestrictions.default.with_passthrough_modules(*PASSTHROUGH))
    return [
        Worker(client, task_queue=Q_JOBS, workflows=[ReplicantJob, MaintenanceBatch, MaintenanceShard],
               activities=activities_by_queue.get(Q_JOBS, []), workflow_runner=runner, deployment_config=dep,
               max_cached_workflows=int(env("REPLICANT_MAX_CACHED_WORKFLOWS", "2000")),
               max_concurrent_workflow_tasks=100, max_concurrent_activities=200),
        Worker(client, task_queue=Q_RUNNER, activities=activities_by_queue.get(Q_RUNNER, []), deployment_config=dep,
               max_concurrent_activities=200),
        Worker(client, task_queue=Q_LLM, activities=activities_by_queue.get(Q_LLM, []), deployment_config=dep,
               max_concurrent_activities=32,
               max_task_queue_activities_per_second=float(env("REPLICANT_LLM_ACTIVITIES_PER_SECOND", "20"))),
    ]


async def run(activities_by_queue: dict[str, list[Any]]) -> None:
    from replicant_temporal.runner import run_workers, set_current_allowed
    from replicant_temporal.versioning import build_id_from_env
    if (build_config.FAST_RETRY or build_config.DETERMINISTIC_RUNS) and env("REPLICANT_ENV", "local") in ("prod", "staging"):
        raise SystemExit("REPLICANT_TEST_FAST_RETRY and REPLICANT_DETERMINISTIC_RUNS are local/CI only")
    build = build_id_from_env()
    log = logging.getLogger("replicant_worker")
    from replicant_worker.activities.config import settings
    if settings().local and settings().registry_publish:
        try:
            from replicant_worker.activities.bundle import publish_local_set
            log.info("published %d v0 definition blobs to the local registry", await asyncio.to_thread(publish_local_set))
        except Exception as e:  # noqa: BLE001 - S3 not ready yet: bundles fall back to the built-in set
            log.warning("local registry publish skipped: %s", str(e)[:200])
    client = await connect()
    workers = build_workers(client, activities_by_queue, build)
    asyncio.get_running_loop().create_task(export_pinned_open_workflows(client, build))
    log.info("replicant-worker starting", extra={"build_id": build, "engine": build_config.ENGINE})
    await run_workers(client, workers, deployment_name=DEPLOYMENT, build_id=build, set_current=set_current_allowed())


def nodecatalog() -> dict[str, Any]:
    from replicant_worker import nodes, registry  # noqa: F401  (registers node types)
    from replicant_worker.graphs.dsl import FGL_VERSION
    from replicant_worker.workflows.state import CARRY_SCHEMA
    return registry.build_node_catalog(
        deployment_name=DEPLOYMENT, build_id=env("REPLICANT_WORKER_BUILD_ID", "b20261024-000000000000"),
        image_digest=env("REPLICANT_WORKER_IMAGE_DIGEST", "sha256:" + "0" * 64), carry_schemas=[CARRY_SCHEMA],
        fgl_version=FGL_VERSION, engine=build_config.ENGINE)


async def healthcheck() -> int:
    from replicant_temporal.health import worker_healthcheck
    from replicant_temporal.queues import DEPLOYMENT_QUEUES
    from replicant_temporal.versioning import build_id_from_env
    return await worker_healthcheck(DEPLOYMENT, build_id_from_env(), DEPLOYMENT_QUEUES[DEPLOYMENT])


def local_bundle_cmd(a: argparse.Namespace) -> int:
    from replicant_worker import local_bundle
    build = env("REPLICANT_WORKER_BUILD_ID", "b20261024-000000000000")
    spec = local_bundle.bundle_spec(build, env("REPLICANT_WORKER_IMAGE_DIGEST", "sha256:" + "0" * 64))
    if a.action == "spec":
        print(json.dumps(spec, indent=2, sort_keys=True))
        return 0
    if a.action == "export":
        out = pathlib.Path(a.out)
        rb, objs = local_bundle.bundle_registry_objects(build, env("REPLICANT_WORKER_IMAGE_DIGEST", "sha256:" + "0" * 64))
        for key, body in objs.items():
            (out / key).parent.mkdir(parents=True, exist_ok=True)
            (out / key).write_bytes(body)
        (out / "bundle-spec.json").write_text(json.dumps(spec, indent=2, sort_keys=True))
        print(json.dumps({"release_bundle_id": rb, "objects": len(objs), "out": str(out)}))
        return 0
    from replicant_worker.activities.bundle import publish_local_set
    print(json.dumps({"published": publish_local_set()}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="replicant-worker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    nc = sub.add_parser("nodecatalog")
    nc.add_argument("action", choices=["export"])
    sub.add_parser("healthcheck")
    lb = sub.add_parser("local-bundle")
    lb.add_argument("action", choices=["spec", "export", "publish"])
    lb.add_argument("--out", default="local-bundle")
    sub.add_parser("grant-jwks")
    a = ap.parse_args(argv)
    if a.cmd == "nodecatalog":
        print(json.dumps(nodecatalog(), indent=2, sort_keys=True))
        return 0
    if a.cmd == "healthcheck":
        return asyncio.run(healthcheck())
    if a.cmd == "local-bundle":
        return local_bundle_cmd(a)
    if a.cmd == "grant-jwks":
        from replicant_worker.activities.grants import signer
        print(json.dumps({"keys": [signer().public_jwk()]}, indent=2))
        return 0
    setup_logging()
    from replicant_worker.activities.catalog_impl import ACTIVITIES_BY_QUEUE
    asyncio.run(run(ACTIVITIES_BY_QUEUE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
