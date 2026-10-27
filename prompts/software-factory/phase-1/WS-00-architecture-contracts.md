# WS-00: Architecture and Contracts

**Output directory:** `spec/00-contracts/`
**Runs:** alone, before all other workstreams. Every Phase 2 workstream builds on your output, so
precision here determines the quality of everything downstream.

## Purpose

Define the system's shape and freeze the interfaces between its parts so eleven workstreams can
design in parallel without contradicting each other. You decide boundaries, ownership, identifiers,
and contracts. You do not design internals; leave those to Phase 2, but leave them no ambiguity
about what they must conform to.

## Required outputs

### 1. `architecture.md`: reference architecture

- Component inventory: every service, worker, controller, store, and external system, with one-line
  responsibility, owning workstream, language/runtime, and deployment unit.
- Three diagrams (Mermaid):
  - Logical architecture with the four state planes visibly separated: workflow state (Temporal),
    execution-environment state (AgentRun/etcd), durable artifacts (object storage), factory domain
    data (Postgres). Show the Runner API as the only path between orchestration and execution.
  - **Local composition** (Docker/Podman compose profile and `kind`/`k3d` controller profile).
  - **Production composition** on EKS: node pools (control plane, workers, sandbox pools per
    runtime class, NVMe-backed), namespaces, Temporal deployment, Postgres instances (factory,
    Temporal, Langfuse kept separate), S3 buckets, Langfuse, Grafana stack, egress proxy/mirror,
    gateway, Okta, Slack, GitHub.
- End-to-end happy-path sequence diagram for a bugfix job: trigger → JobRequest → router → plan →
  approval gate → implement AgentRun → QA AgentRun → review → publish → PR comment → repair AgentRun
  → memory candidate. Name the Temporal primitive at each step.

### 2. `state-ownership.md`: state ownership matrix

A table with one row per piece of state (job lifecycle, graph position, approval decisions,
AgentRun phase, workspace files, diffs, transcripts, CLI session captures, memory entries, briefings,
registries, release bundles, identity links, cost ledger, eval results, audit events, UI read
models, and anything else you find). Columns: state, single owning store, writers, readers,
consistency model, retention, how other stores reference it. Every row has exactly one owner.
Explain the read-model projection pattern for the UI (Temporal → Postgres projection) and how it is
kept consistent (for example, a projection activity at each transition, plus Temporal visibility
queries as the reconciliation source).

### 3. `ids-and-naming.md`: identifiers and correlation

Formats and generation rules for `tenant_id`, `job_id`, Temporal workflow IDs (including across
Continue-As-New), activity idempotency keys, `run_id` / AgentRun names, `release_bundle_id`
(content-addressed), artifact URIs, memory entry IDs, approval/gate IDs, and the OpenTelemetry trace
propagation rule. Include the exact Temporal Search Attributes the factory registers, and the label
and structured-metadata keys used in Loki and Langfuse.

### 4. `runner-api.openapi.yaml`: Runner API contract

Valid OpenAPI 3.1. At minimum:
- `POST /v1/runs` with `Idempotency-Key` header; request body is the run spec (references to
  agent and environment definition versions, repo/ref, inputs URI, artifact prefix, runtime class
  hint, timeout, tenant, trace context, callback target). Returns `run_id` and initial status.
- `GET /v1/runs/{run_id}`: status (phase, conditions, artifact URIs).
- `POST /v1/runs/{run_id}:cancel`.
- `POST /v1/runs/{run_id}/messages` for steering input to a running agent, if the design supports
  it (decide and justify).
- Completion callback contract: how the Runner notifies the workflow (Temporal Signal or Update via a
  small notifier service holding Temporal client credentials; payload schema), and the polling
  fallback semantics.
- Error model, retryable vs non-retryable errors, authentication between workers and the Runner.

### 5. `agentrun-crd.yaml`: AgentRun CRD

A complete `CustomResourceDefinition` with OpenAPI v3 schema for spec and status, printer columns,
status subresource, and validation. Status holds only phase, conditions (with defined condition
types and reasons), timestamps, and URIs. Include the phase state machine (Mermaid
`stateDiagram-v2`) with every transition and its trigger.

### 6. `events-and-signals.md`: workflow message catalog

Every Temporal Signal, Update, and Query the factory defines: name, payload schema (JSON Schema),
sender, validator rules (for Updates), idempotency behavior, and which graph interrupt it resumes.
Include gate decisions, steering messages, cancel/pause, PR comment events, Runner completion,
and release-version notices.

### 7. `job-request.schema.json`: normalized trigger

The single `JobRequest` all triggers normalize into (source, requester identity, tenant, repo
targets, intent text, attachments, priority, requested workflow type or "auto", trace context).

### 8. `definitions-contracts.md`: registry object contracts

Top-level schemas (JSON Schema) and versioning rules for: agent definition, environment
definition, graph definition, policy (risk rules, gate rules, approval channels, budgets), and
**Release Bundle**. Define immutability, content addressing, the publish flow from the Git config
repo into the Postgres registry, and how a bundle resolves every reference to a concrete version.
Internals of each are owned by Phase 2 workstreams; you fix the envelope and the reference format.

### 9. `artifact-layout.md`: object storage contract

Bucket strategy (per environment, per data class), key layout
(`s3://<bucket>/<tenant>/<job_id>/<run_id>/<artifact_type>/...`, or your better alternative),
manifest file format per run, artifact type registry (each with content type, max size, retention
class, redaction requirement, encryption context).

### 10. `postgres-ownership.md`: database ownership map

Which Postgres instances exist, which logical databases and schemas live in each, which service owns
writes to each schema, and the rule that services never write another service's schema. Name the
schemas Phase 2 will define (for example `registry`, `memory`, `identity`, `approvals`, `jobs_read`,
`audit`, `cost`, `evals`). Table-level DDL is owned by Phase 2 workstreams and WS-10.

### 11. `adr/`: architecture decisions

ADRs for at least: Temporal Updates vs Signals for gates; Runner completion via callback vs
long-poll activity; Temporal self-hosted vs Cloud (initial recommendation; WS-10 finalizes);
UI read-model projection approach; Release Bundle as the unit of versioning; and any
`Challenge: D-xx` you raise.

### 12. Coordination

Seed `spec/_coordination/open-questions.md` and `cross-ws-dependencies.md` with what you know.

## Must answer

- Exactly which process holds Temporal client credentials to signal workflows on Runner completion,
  and how that is secured.
- How a job is identified across Continue-As-New and child workflows.
- How the UI shows a consistent job view without querying four stores per page load.
- Where the boundary between "configuration" (no deploy) and "code" (worker deployment version) sits,
  stated as a rule Phase 2 can apply.

## Acceptance criteria

- All twelve outputs exist; OpenAPI and CRD YAML validate.
- Every piece of state has exactly one owner.
- Every Phase 2 workstream can find the contract it needs to conform to.
- External facts used in contracts are verified and tagged.
