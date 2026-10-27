"""replicant-worker entrypoint (XD-11-01, XD-11-13).

    replicant-worker run                  poll replicant-jobs, replicant-runner-calls, replicant-llm-calls
    replicant-worker nodecatalog export   print this build's NodeCatalog (worker CI publishes it, C-DEF 2.1.0
                                        source `worker_ci`)
    replicant-worker healthcheck          exit 0 if Temporal is reachable and this version has pollers

Configuration (env, names from spec/11-local-dev compose.yaml):
    REPLICANT_ENV, REPLICANT_TEMPORAL_ADDRESS, REPLICANT_TEMPORAL_NAMESPACE, REPLICANT_TEMPORAL_TLS (true|false),
    REPLICANT_TEMPORAL_TLS_CERT_FILE / _KEY_FILE / _CA_FILE, REPLICANT_WORKER_BUILD_ID (b<YYYYMMDD>-<sha12>),
    REPLICANT_WORKER_IMAGE_DIGEST, REPLICANT_GRAPH_ENGINE (langgraph-pregel|interpreter),
    REPLICANT_WORKER_SET_CURRENT_ON_START (local and dev only; production promotes through release
    operations), REPLICANT_LLM_ACTIVITIES_PER_SECOND (default 20), REPLICANT_MAX_CACHED_WORKFLOWS (default 2000).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from temporalio.client import Client, TLSConfig
from temporalio.common import WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from . import build_config, registry
from .activity_options import Q_JOBS, Q_LLM, Q_RUNNER
from .dsl import FGL_VERSION
from .state import CARRY_SCHEMA

# LangGraph and its dependencies are imported once per process, not per workflow run. They hold no
# per-run mutable state that Replicant relies on (temporal-langgraph.md section 5, rule D-7).
PASSTHROUGH = ("langgraph", "langchain_core", "langsmith", "typing_extensions", "pydantic", "pydantic_core",
               "ormsgpack", "xxhash", "replicant_orchestration.build_config")


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise SystemExit(f"missing required environment variable {name}")
    return v


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def connect(data_converter: Any = None) -> Client:
    tls: TLSConfig | bool = False
    if env("REPLICANT_TEMPORAL_TLS", "false") == "true":
        tls = TLSConfig(client_cert=_read(env("REPLICANT_TEMPORAL_TLS_CERT_FILE")),
                        client_private_key=_read(env("REPLICANT_TEMPORAL_TLS_KEY_FILE")),
                        server_root_ca_cert=_read(env("REPLICANT_TEMPORAL_TLS_CA_FILE")))
    # XD-08-01 (R1-016): OpenTelemetryPlugin on every client (and so on every worker built from it), the
    # ReplicantContextInterceptor (header `_replicant-ctx`, WS-08 package replicant_telemetry), and headers kept
    # out of the payload codec (HeaderCodecBehavior.NO_CODEC, the SDK default, stated explicitly) so that
    # run-notifier's `_tracer-data` and `_replicant-ctx` stay readable without tenant keys.
    from temporalio.common import HeaderCodecBehavior
    from temporalio.contrib.opentelemetry import OpenTelemetryPlugin
    from replicant_telemetry.temporal import ReplicantContextInterceptor  # WS-08 (telemetry-model.md, hop 4)
    kw: dict[str, Any] = {"namespace": env("REPLICANT_TEMPORAL_NAMESPACE", f"replicant-{env('REPLICANT_ENV', 'local')}"), "tls": tls,
                          "plugins": [OpenTelemetryPlugin()], "interceptors": [ReplicantContextInterceptor()],
                          "header_codec_behavior": HeaderCodecBehavior.NO_CODEC}
    if data_converter is not None:
        kw["data_converter"] = data_converter
    kw["runtime"] = _runtime()
    return await Client.connect(env("REPLICANT_TEMPORAL_ADDRESS", "localhost:7233"), **kw)


_RUNTIME = None


def _runtime() -> Any:
    """SDK metrics on Prometheus with the global tag `deployment_version` (XD-FIX-R1-195), so every SDK series
    (task latency, slots, failures) can be grouped by worker build."""
    global _RUNTIME
    if _RUNTIME is None:
        from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig
        _RUNTIME = Runtime(telemetry=TelemetryConfig(
            metrics=PrometheusConfig(bind_address=env("REPLICANT_METRICS_BIND", "0.0.0.0:9464")),
            global_tags={"deployment_version": f"{build_config.DEPLOYMENT_NAME}:{env('REPLICANT_WORKER_BUILD_ID', 'unknown')}"}))
    return _RUNTIME


async def export_pinned_open_workflows(client: Client) -> None:
    """Gauge replicant_worker_pinned_open_workflows{deployment_version} (XD-FIX-R1-195): open workflows pinned to
    this pod's own build, from Temporal visibility every 60 s. Needs CountWorkflowExecutions for the
    replicant-worker identity (XD-W2-01). Feeds WS-08's drain alerts and the release UI."""
    build = env("REPLICANT_WORKER_BUILD_ID")
    # deployment_version comes from the runtime's global tag; adding it again would duplicate the label
    # (verified: the Prometheus exporter emits the key twice)
    gauge = _runtime().metric_meter.create_gauge(
        "replicant_worker_pinned_open_workflows", "open workflows pinned to this worker build")
    q = (f"TemporalWorkerDeploymentVersion = '{build_config.DEPLOYMENT_NAME}:{build}' "
         "AND ExecutionStatus = 'Running'")
    while True:
        try:
            gauge.set((await client.count_workflows(q)).count)
        except Exception:  # noqa: BLE001 - visibility lag or a transient error; try again next minute
            pass
        await asyncio.sleep(60)


def deployment() -> WorkerDeploymentConfig:
    return WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(build_config.DEPLOYMENT_NAME, env("REPLICANT_WORKER_BUILD_ID")),
        use_worker_versioning=True)  # mandatory: ReplicantJob is PINNED (temporal-platform.md section 4)


async def run(activities_by_queue: dict[str, list[Any]], data_converter: Any = None) -> None:
    from .maintenance import MaintenanceBatch, MaintenanceShard
    from .workflow import ReplicantJob
    if (build_config.FAST_RETRY or build_config.DETERMINISTIC_RUNS) and env("REPLICANT_ENV", "local") in ("prod", "staging"):
        raise SystemExit("REPLICANT_TEST_FAST_RETRY and REPLICANT_DETERMINISTIC_RUNS are local/CI only")
    client = await connect(data_converter)
    dep = deployment()
    runner = SandboxedWorkflowRunner(restrictions=SandboxRestrictions.default.with_passthrough_modules(*PASSTHROUGH))
    workers = [
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
    if env("REPLICANT_WORKER_SET_CURRENT_ON_START", "false") == "true":
        if env("REPLICANT_ENV", "local") in ("prod", "staging"):
            raise SystemExit("REPLICANT_WORKER_SET_CURRENT_ON_START is forbidden in staging and prod")
        asyncio.get_running_loop().create_task(_set_current(client))
    asyncio.get_running_loop().create_task(export_pinned_open_workflows(client))
    await asyncio.gather(*(w.run() for w in workers))


async def _set_current(client: Client) -> None:
    from temporalio.api.workflowservice.v1 import SetWorkerDeploymentCurrentVersionRequest
    for _ in range(30):  # the version must have pollers on every task queue first
        await asyncio.sleep(2)
        try:
            await client.workflow_service.set_worker_deployment_current_version(SetWorkerDeploymentCurrentVersionRequest(
                namespace=client.namespace, deployment_name=build_config.DEPLOYMENT_NAME,
                build_id=env("REPLICANT_WORKER_BUILD_ID"), identity="replicant-worker-local"))
            return
        except Exception:  # noqa: BLE001 - pollers not yet registered
            continue


def nodecatalog() -> dict[str, Any]:
    from . import nodes  # noqa: F401  (registers node types)
    return registry.build_node_catalog(
        deployment_name=build_config.DEPLOYMENT_NAME, build_id=env("REPLICANT_WORKER_BUILD_ID"),
        image_digest=env("REPLICANT_WORKER_IMAGE_DIGEST", "sha256:" + "0" * 64), carry_schemas=[CARRY_SCHEMA],
        fgl_version=FGL_VERSION, engine=build_config.ENGINE)


async def healthcheck() -> int:
    from temporalio.api.deployment.v1 import WorkerDeploymentVersion as PVer
    from temporalio.api.workflowservice.v1 import DescribeWorkerDeploymentVersionRequest
    client = await connect()
    d = await client.workflow_service.describe_worker_deployment_version(DescribeWorkerDeploymentVersionRequest(
        namespace=client.namespace,
        deployment_version=PVer(deployment_name=build_config.DEPLOYMENT_NAME, build_id=env("REPLICANT_WORKER_BUILD_ID"))))
    tqs = {t.name for t in d.version_task_queues}
    missing = {Q_JOBS, Q_RUNNER, Q_LLM} - tqs
    print(json.dumps({"build_id": env("REPLICANT_WORKER_BUILD_ID"), "task_queues": sorted(tqs), "missing": sorted(missing)}))
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="replicant-worker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    nc = sub.add_parser("nodecatalog")
    nc.add_argument("action", choices=["export"])
    sub.add_parser("healthcheck")
    a = ap.parse_args(argv)
    if a.cmd == "nodecatalog":
        print(json.dumps(nodecatalog(), indent=2, sort_keys=True))
        return 0
    if a.cmd == "healthcheck":
        return asyncio.run(healthcheck())
    from .activities_impl import ACTIVITIES_BY_QUEUE  # production activity implementations (not in this reference)
    asyncio.run(run(ACTIVITIES_BY_QUEUE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
