# ADR-08-007: Temporal tracing with OpenTelemetryPlugin plus explicit transition spans

- **Status:** Accepted (WS-08, 2026-10-24). WS-01 implements in the worker.
- **Constraints:** D-01, D-17; C-IDS Rule T-3 (`OpenTelemetryPlugin` preferred, `TracingInterceptor` legacy)

## Context

`temporalio.contrib.opentelemetry` in Python SDK 1.34.0 offers [verified:
https://github.com/temporalio/sdk-python/blob/1.34.0/temporalio/contrib/opentelemetry/README.md, 2026-10-24]:

- `OpenTelemetryPlugin(add_temporal_spans=False)`: real workflow span durations, standard OTel API inside
  workflows, experimental, and "Workflow spans only appear after workflow completion".
- `TracingInterceptor`: stable, immediate, zero-duration workflow spans.
- Both propagate context in the Temporal header `_tracer-data`, the same default header key as the Go SDK's
  `contrib/opentelemetry` tracing interceptor (`defaultHeaderKey = "_tracer-data"`) [verified: sdk-python
  `_interceptor.py`, `_otel_interceptor.py` at 1.34.0; sdk-go `interceptor/tracing_interceptor.go` at v1.50.0,
  2026-10-24]. run-notifier (Go) can therefore inject the context the Python workflow extracts.
- `ReplaySafeMeterProvider` drops metric recordings made by workflow code during replay.

A `ReplicantJob` run can wait 30 days at a gate or in `pr_review`. With the plugin, spans created in workflow code
during that run would appear only when the run completes or continues-as-new.

## Decision

- Register `OpenTelemetryPlugin()` on every Temporal client in control-plane-api, replicant-worker,
  publisher-worker, replicant-projector and eval-worker (C-IDS T-3). Context reaches activities, child workflows,
  Continue-As-New, Signals and Updates through headers.
- **Workflow code creates no long-lived spans.** Live progress does not depend on workflow spans: every node
  boundary is an activity (`RunActivity` spans are created in the activity worker and exported immediately), and
  every transition emits `projection.apply`, whose activity span carries `replicant.phase`, `replicant.node_key` and
  the timeline event type. Those spans are the job's live skeleton in Tempo.
- Workflow-side spans are allowed only around short synchronous sections; they appear when the run ends, which
  for a 30-day wait is acceptable because the activity spans already show the timeline.
- A `ReplicantContext` Temporal interceptor (in the shared `replicant-telemetry` package) carries
  `{job_id, tenant_id, workflow_type, release_bundle_id, requester_id, memory_snapshot_id}` in header
  `_replicant-ctx` so activity-side span processors can stamp C-IDS T-4 attributes without reading workflow inputs
  (telemetry-model.md section 2.3).

## Consequences

- If the plugin's experimental API changes, the fallback is `TracingInterceptor` (same header key, same trace),
  with no change to the rest of the design because nothing depends on workflow span durations.
- Header payload encoding: Temporal headers are payloads; if WS-01's codec encrypts headers, run-notifier (no
  codec key) cannot inject a readable `_tracer-data`. `[unverified: the Python SDK's default header codec
  behavior; WS-01 to confirm headers are not passed through the payload codec]`. Fallback: the workflow
  ignores missing context on `run.status_changed` and parents its handling under the job root span (Rule T-5).

## Revisit when

- The plugin becomes stable and supports incremental workflow span export.
