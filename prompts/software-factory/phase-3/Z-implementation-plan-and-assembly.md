# Z: Trade-offs, Phased Implementation Plan, Traceability, and Assembly

**Output directory:** `spec/99-implementation-plan/`, plus `spec/README.md`
**Runs:** after R1/R2 findings are triaged and fixed.

## Required outputs

### 1. Trade-offs and rejected alternatives (`tradeoffs.md`)

Consolidate the ADRs into one reference. It must explicitly cover each of the following, with the
reasoning grounded in the final design:

- Temporal activities that create or watch Kubernetes objects directly.
- Pure LangGraph (no Temporal) and pure Temporal (no LangGraph).
- A LangGraph checkpointer as authoritative state.
- Fixed linear pipelines (Machinist-style) instead of selectable, extensible graphs.
- Manual schema/version registries.
- Full-repository embedding indexes.
- Designs requiring agent configuration files committed to target repos.
- Reimplementing a coding harness instead of native CLIs.
- Non-local-first architectures.
- Memory stored only in CLI-native memory files.
- Remote-environment QA from day one.
- GUI/desktop environments in sandboxes.
- A single hardcoded isolation runtime.
- CSI ephemeral inline EBS volumes, and EBS snapshots as checkpoints.
- Unversioned workflow code under long-running workflows.
- LLM-decided gate removal.
- SaaS observability.
- Sharing one Postgres among the factory, Temporal, and Langfuse.

### 2. Phased implementation plan (`plan.md`)

The plan will be executed by multiple coding agents and humans working in parallel. Structure it for
that.

- **Phase v0: walking skeleton (local only).** Define it explicitly and keep it small: one workflow
  shape (bugfix), router with deterministic rules, one CLI (`claude -p`), local Docker/Podman Runner
  against the frozen Runner API, MinIO artifacts, factory Postgres with registries (SQLAlchemy
  models, Alembic migrations applied automatically, API-based seeding, per D-26), one human gate
  through the UI only, publish to the local Git server, Langfuse and Loki with `job_id` correlation,
  stub IdP, gateway stub, Release Bundle pinning with a single worker deployment version, and the
  dev GitHub App so an issue opened on a personal org triggers the job end to end. No memory
  promotion, no Slack/GitHub approvals, no EKS. Adjust this list only with stated reasons.
- **Subsequent phases** to full scope, each with: goal, included components, exit criteria
  (measurable), dependencies, and risks. Suggested ordering principle: production execution path
  (controller, `kind` profile, runtime bake-off) and versioning/evals before broad feature work;
  memory write path behind curation before automatic promotion; EKS after the `kind` profile passes
  the same e2e suite.
- **Work breakdown**: for each phase, epics and tasks sized for one agent or engineer, each with the
  spec sections it implements, its interface contracts, its acceptance tests, and its upstream
  dependencies, so tasks can be assigned in parallel without collisions. Mark tasks that touch a
  frozen contract.
- **Critical path** and parallelizable streams (Mermaid Gantt or dependency graph; dependencies only,
  no calendar dates required).
- **Spikes to run first**: the runtime benchmark (WS-02), the Temporal LangGraph integration spike
  (WS-01), CLI session capture and resume (WS-04), and any load-bearing unverified claim.

### 3. Traceability matrix (`traceability.md`)

Map every requirement to the sections that satisfy it: each D-xx constraint, each original
deliverable listed below, each "Must answer" question across all briefs, and each UX requirement in
shared context section 4. Flag anything without a satisfying section.

Original deliverables to trace: architecture overview; graph patterns; isolation model; storage
design; tool/environment/agent-config provisioning; context injection; feedback memory; QA/QE stage;
observability; identity; AI/ML gateway integration; approval channels; state model and durability;
security and tenancy; local development and test plan; trade-offs and rejected alternatives;
phased implementation plan. Added deliverables: workflow versioning and UI management; Release
Bundles and evaluation; Postgres and data platform; memory as a full subsystem; user experience
design.

### 4. Assembly (`spec/README.md`)

Reading order, a one-page architecture summary with the main diagram, links to every section and
ADR, the list of accepted challenges to decided constraints, the consolidated open questions with
recommendations, and the remaining unverified claims with their fallbacks.

## Acceptance criteria

- v0 is small, runnable, and exercises the core boundaries (Runner API, Temporal + LangGraph, state
  separation, Release Bundle pinning, observability correlation).
- Every task in the work breakdown references spec sections and acceptance tests.
- The traceability matrix has no unmapped requirement.
