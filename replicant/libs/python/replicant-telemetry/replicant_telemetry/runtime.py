"""Process-wide tracer providers with an OTLP/HTTP exporter (telemetry-model.md §2.3).

Env: OTEL_SDK_DISABLED (true = off), OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (default http://alloy:4318/v1/traces),
OTEL_SERVICE_NAME or the `service` argument, REPLICANT_ENV.
"""
from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator
from typing import Any, Optional

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

from replicant_telemetry import core

_lock = threading.Lock()
_state: dict[str, Any] = {}


def enabled() -> bool:
    return os.environ.get("OTEL_SDK_DISABLED", "true").lower() != "true"


def _env() -> str:
    return os.environ.get("REPLICANT_ENV", "local")


def _providers(service: str | None = None) -> tuple[TracerProvider, TracerProvider] | None:
    if not enabled():
        return None
    with _lock:
        if "main" not in _state:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            import sys
            name = (os.environ.get("OTEL_SERVICE_NAME") or service
                    or os.path.basename(sys.argv[0] or "") or "replicant").removesuffix(".py")
            endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://alloy:4318/v1/traces")
            res = Resource.create({"service.name": name, "replicant.env": _env(),
                                   "deployment.environment.name": f"replicant-{_env()}"})
            export = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=5), schedule_delay_millis=1000)
            main = core.build_tracer_provider(_env())
            main._resource = res  # noqa: SLF001 - build_tracer_provider creates the provider without a resource
            main.add_span_processor(export)
            root = core.build_root_provider(_env(), [export])
            root._resource = res  # noqa: SLF001
            _state.update(main=main, root=root, service=name)
        return _state["main"], _state["root"]


def tracer(service: str | None = None):  # noqa: ANN201
    p = _providers(service)
    return p[0].get_tracer("replicant") if p else None


def emit_job_root(job_id: str, tenant_id: str, *, workflow_type: str | None = None,
                  release_bundle_id: str | None = None, trigger_traceparent: str | None = None,
                  service: str | None = None) -> Optional[str]:
    """Root span `job` (T-1 trace, T-5a span). Returns the trace ID, or None when tracing is off."""
    p = _providers(service)
    if not p:
        return None
    ctx = core.ReplicantContext(tenant_id=tenant_id, job_id=job_id, workflow_type=workflow_type,
                              release_bundle_id=release_bundle_id)
    sc = core.start_job_root_span(p[1], ctx, trigger_traceparent)
    return f"{sc.trace_id:032x}"


@contextlib.contextmanager
def job_span(name: str, job_id: str, *, tenant_id: str | None = None, kind: SpanKind = SpanKind.INTERNAL,
             attributes: dict[str, Any] | None = None, service: str | None = None) -> Iterator[Any]:
    """A span in the job's trace, parented under the root span `job` unless a job span is already current."""
    t = tracer(service)
    if t is None or not job_id.startswith("job_"):
        yield None
        return
    from opentelemetry import trace

    cur = trace.get_current_span().get_span_context()
    parent = None if (cur.is_valid and f"{cur.trace_id:032x}" == core.job_trace_id(job_id)) else core.job_context(job_id)
    fctx = core.ReplicantContext(tenant_id=tenant_id or "", job_id=job_id)
    tok = core.set_replicant_context(fctx)
    try:
        with t.start_as_current_span(name, context=parent, kind=kind,
                                     attributes={"replicant.job_id": job_id, **(attributes or {})}) as span:
            try:
                yield span
            except BaseException as e:
                span.set_status(Status(StatusCode.ERROR, type(e).__name__))
                raise
    finally:
        core._current.reset(tok)  # noqa: SLF001


def log_context(record: Any = None) -> dict[str, str]:
    """C-IDS §9 correlation keys for a log line: job_id/run_id/tenant_id from the record (extras), the active
    ReplicantContext or a job span, plus trace_id/span_id of the current span. Services merge this into their JSON
    log objects; Alloy derives trace_id from job_id when a line has no span (T-1)."""
    from opentelemetry import trace

    out: dict[str, str] = {}
    for k in ("job_id", "run_id", "tenant_id"):
        v = getattr(record, k, None) if record is not None else None
        if v:
            out[k] = str(v)
    ctx = core.current_replicant_context()
    if ctx is not None:
        if ctx.job_id and "job_id" not in out:
            out["job_id"] = ctx.job_id
        if ctx.tenant_id and "tenant_id" not in out:
            out["tenant_id"] = ctx.tenant_id
    sc = trace.get_current_span().get_span_context()
    if sc.is_valid:
        out["trace_id"], out["span_id"] = f"{sc.trace_id:032x}", f"{sc.span_id:016x}"
    elif out.get("job_id", "").startswith("job_"):
        out["trace_id"] = core.job_trace_id(out["job_id"])
    return out


def shutdown() -> None:
    with _lock:
        for k in ("main", "root"):
            p = _state.pop(k, None)
            if p:
                p.shutdown()
