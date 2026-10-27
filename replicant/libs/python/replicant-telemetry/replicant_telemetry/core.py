"""replicant-telemetry: the shared OpenTelemetry setup every Python replicant service imports (WS-08).

Responsibilities (telemetry-model.md section 2.3):
  * ReplicantContext: a contextvar holding the job-scoped identity of the current unit of work.
  * ReplicantContextSpanProcessor: stamps C-IDS T-4 attributes on every span at start (Langfuse needs trace-level
    attributes on every span, C-IDS section 10).
  * ReplicantRootSampler: always samples job work; samples other root traces at a ratio.
  * start_job_root_span(): emits the root span `job` with the deterministic trace ID (T-1) and span ID (T-5a,
    CCR-08-01) and a link to the trigger span (T-2).
  * JsonLogFormatter: one JSON object per line with the fixed metadata keys of C-IDS section 9.

Self-test: uv run --with opentelemetry-sdk python replicant_telemetry.py
"""
from __future__ import annotations

import contextvars
import dataclasses
import json
import logging
import time
from typing import Optional, Sequence

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import Link, NonRecordingSpan, SpanContext, SpanKind, TraceFlags
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import Attributes

from replicant_telemetry.redact import redact_text
from replicant_telemetry.trace_ids import job_root_span_id, job_trace_id


@dataclasses.dataclass(frozen=True)
class ReplicantContext:
    tenant_id: str
    job_id: str
    workflow_type: Optional[str] = None
    workflow_id: Optional[str] = None
    workflow_run_id: Optional[str] = None
    release_bundle_id: Optional[str] = None
    requester_id: Optional[str] = None
    memory_snapshot_id: Optional[str] = None
    run_id: Optional[str] = None
    node_key: Optional[str] = None
    cycle: Optional[int] = None
    ordinal: Optional[int] = None
    role: Optional[str] = None
    phase: Optional[str] = None  # C-IDS 1.10.0 section 5 (CCR-08-01): replicant.phase on all job spans
    # Langfuse experiment attributes for eval jobs (WS-06 XD-06-05): keys id, name, dataset.id, item.id,
    # item.root_observation_id, item.version -> span attributes langfuse.experiment.<key>
    langfuse_experiment: Optional[tuple] = None  # tuple of (key, value) pairs, hashable for the frozen dataclass

    def span_attributes(self) -> dict:
        out = {}
        for f in dataclasses.fields(self):
            v = getattr(self, f.name)
            if v is None or f.name == "langfuse_experiment":
                continue
            out[f"replicant.{f.name}"] = v
        for k, v in self.langfuse_experiment or ():
            out[f"langfuse.experiment.{k}"] = v
        return out

    def with_phase(self, phase: str) -> "ReplicantContext":
        return dataclasses.replace(self, phase=phase)

    # Temporal header `_replicant-ctx` (XD-FIX-R1-016): identifiers only, never names or free text. Written by
    # replicant_telemetry.temporal.ReplicantContextInterceptor on every outbound command; the client is created with
    # header_codec_behavior=NO_CODEC, so the header must stay free of content. Experiment attributes are not
    # carried: workers take them from the run plan's effective.experiment (XD-FIX-R1-076).
    HEADER_KEYS = ("tenant_id", "job_id", "workflow_type", "release_bundle_id", "requester_id", "memory_snapshot_id")

    def to_header(self) -> bytes:
        return json.dumps({k: getattr(self, k) for k in self.HEADER_KEYS if getattr(self, k) is not None},
                          separators=(",", ":"), sort_keys=True).encode()

    @classmethod
    def from_header(cls, raw: bytes) -> "ReplicantContext":
        d = json.loads(raw)
        return cls(**{k: d[k] for k in cls.HEADER_KEYS if k in d})


_current: contextvars.ContextVar[Optional[ReplicantContext]] = contextvars.ContextVar("replicant_ctx", default=None)


def set_replicant_context(ctx: Optional[ReplicantContext]) -> contextvars.Token:
    """Called by middleware (FastAPI request, Temporal activity interceptor reading header `_replicant-ctx`)."""
    return _current.set(ctx)


def current_replicant_context() -> Optional[ReplicantContext]:
    return _current.get()


class ReplicantContextSpanProcessor(SpanProcessor):
    """Stamps replicant.* attributes (C-IDS T-4) on every span started while a ReplicantContext is active."""

    def __init__(self, env: str):
        self._env = env

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        ctx = _current.get()
        span.set_attribute("replicant.env", self._env)
        span.set_attribute("deployment.environment.name", f"replicant-{self._env}")
        if ctx is not None:
            span.set_attributes(ctx.span_attributes())

    def on_end(self, span: ReadableSpan) -> None:  # noqa: D401 - interface
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


ALWAYS_SAMPLE_ROUTES = ("/v1/jobs", "/internal/v1/gates/", "/webhooks/", "/slack/")


class ReplicantRootSampler(Sampler):
    """Root decision only (wrapped in ParentBased): job work and trigger endpoints are always sampled; health and
    read traffic is sampled at `ratio`. Children follow their parent, so a job trace is complete."""

    def __init__(self, ratio: float = 0.05):
        self._ratio = TraceIdRatioBased(ratio)

    def should_sample(self, parent_context: Optional[Context], trace_id: int, name: str,
                      kind: Optional[SpanKind] = None, attributes: Attributes = None,
                      links: Optional[Sequence[Link]] = None, trace_state=None) -> SamplingResult:
        attrs = attributes or {}
        if _current.get() is not None or "replicant.job_id" in attrs:
            return SamplingResult(Decision.RECORD_AND_SAMPLE, attrs)
        route = str(attrs.get("http.route", ""))
        if any(route.startswith(p) for p in ALWAYS_SAMPLE_ROUTES):
            return SamplingResult(Decision.RECORD_AND_SAMPLE, attrs)
        return self._ratio.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

    def get_description(self) -> str:
        return "ReplicantRootSampler"


def build_tracer_provider(env: str, ratio: float = 0.05) -> TracerProvider:
    provider = TracerProvider(sampler=ParentBased(root=ReplicantRootSampler(ratio)))
    provider.add_span_processor(ReplicantContextSpanProcessor(env))
    return provider


def job_context(job_id: str, sampled: bool = True) -> Context:
    """A parent context positioned at the job's root span (T-1 trace ID, T-5a span ID). Used by any component that
    acts for a job without an inbound traceparent (github-integration on pr.event, sweepers, audit export)."""
    sc = SpanContext(trace_id=int(job_trace_id(job_id), 16), span_id=int(job_root_span_id(job_id), 16),
                     is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED if sampled else 0))
    return trace.set_span_in_context(NonRecordingSpan(sc))


_root_ids: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar("replicant_root_ids", default=None)


class JobRootIdGenerator(RandomIdGenerator):
    """ID generator of the dedicated root-span provider: returns the deterministic T-1/T-5a IDs when
    start_job_root_span() has set them in a contextvar (safe under concurrency), random IDs otherwise."""

    def generate_trace_id(self) -> int:
        ids = _root_ids.get()
        return ids[0] if ids else super().generate_trace_id()

    def generate_span_id(self) -> int:
        ids = _root_ids.get()
        return ids[1] if ids else super().generate_span_id()

    def is_trace_id_random(self) -> bool:
        return False


def build_root_provider(env: str, processors: Sequence[SpanProcessor]) -> TracerProvider:
    """Provider used only for the `job` root span; shares exporters with the main provider."""
    provider = TracerProvider(id_generator=JobRootIdGenerator())
    provider.add_span_processor(ReplicantContextSpanProcessor(env))
    for p in processors:
        provider.add_span_processor(p)
    return provider


def start_job_root_span(root_provider: TracerProvider, ctx: ReplicantContext, trigger_traceparent: Optional[str]):
    """control-plane-api, inside the POST /v1/jobs transaction (C-IDS T-2). Emits and ends span `job` with trace ID
    T-1 and span ID T-5a, linked to the trigger span. Callers start the workflow with job_context(job_id) current."""
    links = []
    if trigger_traceparent:
        trig = trace.get_current_span(TraceContextTextMapPropagator().extract({"traceparent": trigger_traceparent}))
        if trig.get_span_context().is_valid:
            links.append(Link(trig.get_span_context(), {"replicant.link": "trigger"}))
    ids_token = _root_ids.set((int(job_trace_id(ctx.job_id), 16), int(job_root_span_id(ctx.job_id), 16)))
    ctx_token = set_replicant_context(ctx)
    try:
        span = root_provider.get_tracer("replicant.control_plane").start_span(
            "job", kind=SpanKind.INTERNAL, links=links, context=Context())
        span.end()
        return span.get_span_context()
    finally:
        _current.reset(ctx_token)
        _root_ids.reset(ids_token)


METADATA_KEYS = ("job_id", "workflow_id", "workflow_run_id", "run_id", "agentrun", "trace_id", "span_id",
                 "node_key", "cycle", "activity_id", "gate_id", "decision_id", "release_bundle_id", "phase",
                 "principal_id", "memory_snapshot_id")


class JsonLogFormatter(logging.Formatter):
    """stdout JSON lines for node-logs.alloy. Labels: tenant_id, workflow_type, role, level, log_type.
    Metadata: C-IDS section 9 keys only. The message and every string extra are redacted at the source with the
    replicant ruleset (redact.py) before formatting (D-21, R1-198); Alloy's secretfilter is the second pass."""

    converter = time.gmtime  # timestamps are UTC ("Z")

    def format(self, record: logging.LogRecord) -> str:
        ctx = _current.get()
        span = trace.get_current_span().get_span_context()
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
               "level": record.levelname.lower(), "logger": record.name, "msg": redact_text(record.getMessage())}
        if ctx:
            out.update({"tenant_id": ctx.tenant_id, "job_id": ctx.job_id, "workflow_type": ctx.workflow_type,
                        "workflow_id": ctx.workflow_id, "workflow_run_id": ctx.workflow_run_id,
                        "run_id": ctx.run_id, "node_key": ctx.node_key, "cycle": ctx.cycle,
                        "release_bundle_id": ctx.release_bundle_id, "role": ctx.role, "phase": ctx.phase,
                        "memory_snapshot_id": ctx.memory_snapshot_id})
        if span.is_valid:
            out["trace_id"], out["span_id"] = f"{span.trace_id:032x}", f"{span.span_id:016x}"
        for k in METADATA_KEYS + ("log_type",):
            v = getattr(record, k, None)
            if v is not None:
                out[k] = redact_text(v) if isinstance(v, str) else v
        return json.dumps({k: v for k, v in out.items() if v is not None}, separators=(",", ":"))


if __name__ == "__main__":
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    export = SimpleSpanProcessor(exporter)
    provider = build_tracer_provider("prod", ratio=0.0)
    provider.add_span_processor(export)
    root_provider = build_root_provider("prod", [export])
    ctx = ReplicantContext(tenant_id="payments", job_id="job_01m5p7x7r0bt69j0h0fbxepw90", workflow_type="bugfix",
                         release_bundle_id="rb_ds5p3wmcj4dag2e9a8ncp4py6k", requester_id="usr_01knk8dfr0fvjc7v0b9vhvkzbp",
                         phase="planning",
                         langfuse_experiment=(("id", "exp_42"), ("dataset.id", "ds_7")))
    root = start_job_root_span(root_provider, ctx, "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    assert f"{root.trace_id:032x}" == "fc0575170c8597ce9805552723c08d6e"
    assert f"{root.span_id:016x}" == job_root_span_id(ctx.job_id)
    # A later component with only the job_id parents under the root span.
    tok = set_replicant_context(ctx)
    child = provider.get_tracer("replicant.github").start_span("pr.event", context=job_context(ctx.job_id))
    child.end()
    _current.reset(tok)
    # Non-job traffic at ratio 0 is not sampled.
    noise = provider.get_tracer("x").start_span("GET /healthz", attributes={"http.route": "/healthz"})
    noise.end()
    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert names == ["job", "pr.event"], names
    assert spans[1].parent.span_id == root.span_id
    assert spans[1].attributes["replicant.tenant_id"] == "payments"
    assert spans[1].attributes["replicant.phase"] == "planning"
    assert spans[1].attributes["langfuse.experiment.dataset.id"] == "ds_7"
    assert spans[0].links[0].context.trace_id == int("4bf92f3577b34da6a3ce929d0e0e4736", 16)
    logging.basicConfig()
    h = logging.StreamHandler()
    h.setFormatter(JsonLogFormatter())
    log = logging.getLogger("replicant.demo")
    log.addHandler(h)
    log.propagate = False
    tok = set_replicant_context(ctx)
    import io
    buf = io.StringIO()
    h.setStream(buf)
    log.warning("calling gateway with Authorization: Bearer eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJydW4ifQ.c2lnbmF0dXJlLXg",
                extra={"activity_id": "implementer.c0.n1.run"})
    line = json.loads(buf.getvalue())
    assert "eyJzdWIi" not in line["msg"] and line["phase"] == "planning", line
    print(buf.getvalue().strip())
    _current.reset(tok)
    hdr = ReplicantContext(tenant_id="payments", job_id="job_01m5p7x7r0bt69j0h0fbxepw90", phase="implementing",
                         langfuse_experiment=(("name", "x"),)).to_header()
    assert b"phase" not in hdr and b"experiment" not in hdr, hdr
    assert ReplicantContext.from_header(hdr).job_id == "job_01m5p7x7r0bt69j0h0fbxepw90"
    print("replicant_telemetry self-test passed")
