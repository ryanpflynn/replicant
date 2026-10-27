# replicant-telemetry

WS-08 shared telemetry for Python services, from `spec/08-observability/code` (`core.py` = `replicant_telemetry.py`,
`redact.py`, `trace_ids.py`, rule files under `replicant_telemetry/redaction/`). Adds:
- `runtime`: OTLP/HTTP exporter to Alloy (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`), `emit_job_root()` (span `job`,
  T-1/T-5a), `job_span()`; off when `OTEL_SDK_DISABLED=true`.
- `temporal.TracingInterceptor`: spans for workflow start, Signals/Updates and every activity, in the job trace.
  `replicant_temporal.connect()` installs it when this package is importable.

Tests: `uv run --package replicant-telemetry --group dev pytest libs/python/replicant-telemetry`
