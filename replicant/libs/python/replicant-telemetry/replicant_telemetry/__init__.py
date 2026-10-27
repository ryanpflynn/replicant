"""replicant-telemetry (WS-08, spec/08-observability/code): the shared OpenTelemetry setup of Python replicant services.

`core` is the spec reference (ReplicantContext, span processor, sampler, job root span, JSON log formatter);
`runtime` wires it to an OTLP/HTTP exporter (Alloy, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`), and `temporal` adds a
Temporal interceptor so every activity runs in a span of its job's trace (T-1) under the root span `job` (T-5a).
Tracing is off when `OTEL_SDK_DISABLED=true` (the v0 default without the observability profile).
"""
from replicant_telemetry.core import (  # noqa: F401
    ReplicantContext,
    JsonLogFormatter,
    current_replicant_context,
    job_context,
    set_replicant_context,
    start_job_root_span,
)
from replicant_telemetry.redact import RedactingFormatter  # noqa: F401
from replicant_telemetry.runtime import emit_job_root, enabled, job_span, log_context, shutdown, tracer  # noqa: F401
from replicant_telemetry.trace_ids import job_root_span_id, job_trace_id, run_span_id  # noqa: F401
