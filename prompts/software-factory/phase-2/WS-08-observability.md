# WS-08: Observability

**Output directory:** `spec/08-observability/`
**Constraints most relevant:** D-17, D-21

## Purpose

Make every job traceable from trigger to merged PR across Temporal, the Runner, AgentRuns, the CLIs,
the gateway, and the humans in the loop, using self-hosted Langfuse OSS and the Grafana OSS stack.

## Required outputs

### 1. Platform deployment (`platform.md`)

- **Langfuse OSS self-hosted**: verify the current architecture and requirements (web and worker
  services, Postgres, ClickHouse, Redis/Valkey, S3-compatible blob storage), Helm chart or manifests,
  sizing for the assumed scale, SSO with Okta, retention, and backups. Its Postgres is separate from
  the factory's.
- **Grafana OSS stack**: Grafana, Loki, Tempo, Prometheus or Mimir (decide with ADR at the assumed
  scale), Grafana Alloy as the collector (verify Promtail's status). Deployment mode, object storage
  backends, retention, multi-tenancy (`X-Scope-OrgID` per factory tenant or not; decide), Okta SSO
  for Grafana.

### 2. Telemetry model (`telemetry-model.md`)

- OpenTelemetry trace propagation through every hop: trigger service → Temporal (verify the
  Temporal SDK's OTel interceptor support) → activities → Runner API → controller → sandbox entrypoint
  → CLI → gateway. Show the trace tree for one job.
- Langfuse trace structure per job: one trace per job or per workflow run (decide), spans per node
  and per CLI invocation, generations per model call (via gateway or CLI telemetry), scores, prompt
  version links, memory entry IDs as metadata.
- How Langfuse and Tempo traces relate (shared trace IDs, Langfuse OTel ingestion; verify).
- Loki: label set (low-cardinality only: environment, service, tenant, workflow type, phase), and
  structured metadata keys for `job_id`, `workflow_id`, `run_id`, `trace_id`.
- Metrics: Temporal SDK and server metrics, controller metrics, Runner metrics, sandbox resource
  metrics, gateway metrics, business metrics from WS-06. Name them.

### 3. Coding-session capture (`session-capture.md`)

How CLI transcripts and tool calls are captured from inside the sandbox: parsing
`stream-json` / Codex JSON events in the runner entrypoint, CLI-native OpenTelemetry export
(verify Claude Code's telemetry variables and Codex equivalents), shipping to Loki via a sidecar or
the entrypoint, full transcripts to object storage, selected events to Langfuse. **Redaction
pipeline** before any sink (secret patterns, entropy detection, PII rules) with tests.

### 4. Dashboards and correlation (`dashboards.md`)

- Grafana dashboards (list them with panels and queries): factory overview, per-tenant, sandbox
  fleet, Temporal health, gateway cost, release canary comparison.
- Job correlation: from a `job_id`, one click to the Temporal history, AgentRun status, Langfuse
  trace, Loki logs, and Tempo trace. Grafana data links and the control-plane UI's deep links.
- Alerts: SLO-based alerting (sandbox-ready latency, job failure rate, stuck workflows, approval
  backlog, cost anomalies) with rules.

### 5. Audit (`audit.md`)

The audit trail across Temporal history, the Postgres `audit` schema, and Okta logs: what each holds,
retention, and how an auditor answers "who approved this change and what did they see?"

## Must answer

- Given a merged PR, how does an engineer find everything the agent did to produce it?
- What is the Loki and ClickHouse ingest volume per day at the assumed scale, and the storage cost?
- How is a secret that leaked into a transcript found and purged from every sink?

## Acceptance criteria

- Every hop's propagation mechanism is named and verified.
- Label and metadata keys are fixed and consistent with `ids-and-naming.md`.
- Redaction runs before every sink and has a test plan.
