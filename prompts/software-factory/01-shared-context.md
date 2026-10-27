# Shared Context: Enterprise Software Factory

Every agent working on this specification reads this file in full before doing anything else.
It is authoritative. Where a workstream brief and this file disagree, this file wins. Once Phase 1
freezes `spec/00-contracts/`, those contracts are authoritative for interfaces and this file remains
authoritative for constraints.

---

## 1. Mission

Design a production software factory for a large engineering organization: a system that accepts
work (bug reports, feature requests, API changes, docs fixes, hotfixes, scheduled maintenance, PR
review feedback), plans it, executes it with native coding-agent CLIs inside strongly isolated
sandboxes, validates it locally (build, run, headless browser checks), routes it through human
approval, publishes pull requests, and learns from reviewer feedback over time.

The output is a **complete technical specification and reference architecture** that a team of
engineers (and coding agents) can implement directly. Prefer concrete designs, schemas, sequence
flows, configuration, and code over prose. Every recommendation must name the library, flag, API,
field, or primitive involved.

Optimize the design, in priority order, for:

1. **Correctness and safety.** Untrusted code and untrusted text run here. Isolation and
   authorization are not negotiable.
2. **Long-term viability.** The system will evolve for years: workflow shapes, prompts, models, CLIs,
   and memory will all change under live traffic. Versioning, evaluation, and reversibility are
   first-class.
3. **User experience.** Engineers trigger, watch, steer, approve, and take over work. Approvers need
   context, not noise. Platform operators need one place to see and control releases.
4. **Operability.** One local command reproduces the production code paths. Every job is traceable
   end to end.

Effort and timeline are not constraints. Choose the better design over the quicker one.

---

## 2. Scale and SLO targets (confirmed)

Size all infrastructure from these. If a design choice is sensitive to a number, say so and give the
threshold at which the choice changes.

| Dimension | Assumed value |
|---|---|
| Engineers with access | 2,000 |
| Tenants (teams/orgs with separate policy) | 40 |
| Repositories under management | 1,500 |
| Jobs per day (steady) | 300, peaks of 1,000 |
| Peak concurrent AgentRuns | 150 |
| Median job wall time excluding human waits | 25 min |
| Longest human wait to support | 30 days |
| Time from trigger to first visible progress | under 60 s |
| Time from trigger to sandbox ready (p95) | under 90 s |
| Control plane availability | 99.9% |
| Artifact retention ceiling | 2 years (diffs), 90 days (transcripts) by default |

---

## 3. Decided constraints

Each constraint has an ID. Reference IDs in your output. If you believe a constraint is technically
wrong or will fail at the scale above, **do not silently work around it**: write an ADR in your
section titled `Challenge: D-xx`, explain the failure with evidence, propose the replacement, and
continue designing against the constraint as written unless the replacement is a strict superset.

### Orchestration

- **D-01 Temporal + LangGraph.** Temporal is the durable execution spine: workflow lifetime, crash
  recovery, long waits, retries, timers, audit history. LangGraph defines workflow shapes and node
  logic, integrated through Temporal's LangGraph integration. Each LangGraph node that does real work
  (I/O, LLM calls, Runner calls) executes as a Temporal activity. The router classifies the trigger
  and selects a subgraph.
  - **Temporal history is the single source of truth for workflow state.** No LangGraph checkpointer
    holds authoritative state in production. If the integration requires a checkpointer, explain
    exactly what it stores and why it cannot diverge from Temporal history.
  - Human gates are LangGraph interrupts backed by Temporal **Updates** (preferred, because they
    return a result and can be validated) or Signals. Justify the choice per gate type.
  - The maturity of Temporal's LangGraph integration must be verified against current
    documentation and source. If it is experimental, design an adapter layer so the factory is not
    coupled to its internals, and describe the fallback (hand-written Temporal workflow that executes
    a compiled LangGraph graph definition).
- **D-02 Workflow versioning is first-class and managed from the UI.**
  - Use Temporal Worker Versioning (Worker Deployments, deployment versions, pinned vs auto-upgrade
    behavior, ramping) so in-flight workflows replay on the code that started them.
  - Every workflow is **pinned at start** to a Release Bundle (D-03). Long human waits (D-19) must
    never break replay.
  - Use Continue-As-New at repair-cycle and phase boundaries to bound history size (Temporal limits:
    verify current event-count and size limits).
  - Operators manage versions in the control-plane UI: see which bundle versions exist, which
    workflows are pinned to each, ramp new versions by percentage or tenant, roll back, drain and
    retire old versions.
  - "Extensible without redeploying the core" means: **configuration-level changes** (inserting an
    existing node into a sequence, parameters, subgraph selection rules, per-codebase constraints)
    need no deploy; **new node code** requires a worker deployment version but never a control-plane
    change. Say this plainly in the spec.
- **D-03 Release Bundle.** An immutable, content-addressed, versioned tuple that fully determines
  factory behavior for a job: graph definition version, worker deployment version, agent definition
  versions, tool/environment definition versions, prompt versions (Langfuse prompt management labels
  resolved to concrete versions), CLI versions (pinned image digests), model identifiers, policy
  versions, and the memory snapshot policy. Bundles are promoted only after passing the evaluation
  harness (WS-06). Every job records the bundle it ran on.

### Execution isolation

- **D-04 Narrow boundary.** Temporal activities call the **Runner API** ("start run with this
  spec") and receive a run ID. They never call the Kubernetes API. The workflow waits for completion
  via a Runner-originated Signal/Update with a timer timeout plus a polling fallback activity. Runner
  calls are idempotent, keyed on an idempotency key derived from workflow ID + activity ID + attempt
  semantics, so retries never create duplicate runs.
- **D-05 AgentRun CRD + controller.** `factory.example.com/v1alpha1 AgentRun` is the unit of
  isolated work. Spec: repo + ref, agent definition reference, tool/environment definition
  reference, runtime class, resource profile, network policy reference, artifact output prefix,
  timeout, idempotency key, tenant. Status: phase, conditions, object-storage URIs only (no
  artifact bodies). The controller owns the full lifecycle (Job/Pod creation, config application,
  artifact collection, completion event, finalizer cleanup).
  - Workflow state lives in Temporal. Cluster resource state lives in the CRD/etcd. Artifacts live
    in object storage. Factory domain data lives in Postgres (D-09). Nothing crosses these lines.
- **D-06 Isolation runtime is pluggable; the default is chosen by evidence.** The runtime is
  selected per AgentRun via `RuntimeClass`, set by policy (tenant + workload class). Candidates:
  - gVisor (`runsc`, systrap platform; KVM platform where available)
  - Kata Containers with Firecracker
  - Kata Containers with Cloud Hypervisor
  WS-02 runs a structured comparison and recommends a default, possibly different per workload
  class (coding vs QA/build). Comparison criteria include: isolation strength, EKS operability
  (custom AMIs, metal or nested-virtualization instance requirements, node bootstrap), cold start,
  I/O-heavy build performance (`npm ci`, `go build`, `cargo build`, image builds), container-image
  builds inside the sandbox, sidecar services for QA, emptyDir behavior and `sizeLimit` enforcement
  under the runtime, memory overhead, and density. **Prior lean (to be tested, not assumed):** Kata
  on bare-metal or nested-virt-capable node pools is likely stronger for QA/build workloads because a
  real guest kernel makes rootless image builds and sidecar services far more reliable than under
  gVisor.
  - Firecracker snapshotting / warm pools may be proposed as a later optimization for boot latency.
    Snapshots are never a persistence or checkpoint mechanism.
- **D-07 Workspace storage: local ephemeral disk.**
  - Default: `emptyDir` with `sizeLimit`, on node pools with local NVMe instance store (for example
    `m6id`, `c6id`, `m7gd`, `i4i`, or metal equivalents), with the kubelet's ephemeral storage on the
    NVMe volume. Pods declare `ephemeral-storage` requests and limits so scheduling accounts for
    disk.
  - "Fail loudly on overrun": kubelet `sizeLimit` enforcement is periodic eviction, not a hard quota,
    and `LocalStorageCapacityIsolationFSQuotaMonitoring` only improves accounting (it does not
    enforce). Design for both: **an enforcing hard limit per workspace** (guest block device under
    Kata; enforcing XFS project quota `bhard` set by a node agent, or a per-pod LVM/loop volume, under
    gVisor/runc; volume size for generic ephemeral volumes), **plus** an in-sandbox disk watchdog that
    emits a structured `WorkspaceDiskExceeded` failure before eviction. (Amended by accepted
    challenge ADR-014, 2026-10-24.)
  - Overflow option for workspaces larger than local disk allows: **generic ephemeral volumes**
    (`volumes[].ephemeral.volumeClaimTemplate`) backed by the EBS CSI driver with a StorageClass
    using `reclaimPolicy: Delete` and `volumeBindingMode: WaitForFirstConsumer`.
  - **Not** CSI ephemeral inline volumes: the AWS EBS CSI driver does not support them, and inline
    volumes do not use StorageClasses. **Not** EBS snapshots as checkpoints.
  - Workspaces are scratch. Anything worth keeping is uploaded to object storage before the pod
    exits. Coding-CLI session state that must survive (D-14) is captured explicitly.
- **D-08 Durable artifacts in object storage.** S3 in production; locally an S3-compatible server (SeaweedFS by default, since the MinIO repository was archived in April 2026 and no longer publishes community images; amended by accepted CCR-11-02). System of record
  for diff bundles, QA logs, build outputs, screenshots, coding-session transcripts, CLI session
  state captures, review artifacts, oversized briefing packets, and memory export snapshots.
  Retention and lifecycle per artifact type.
- **D-09 Postgres is the factory's relational system of record.** Factory domain data: job
  read-model projections for the UI, registries (agent, tool/environment, graph, release bundles,
  policies), the memory store (with `pgvector` for retrieval over memory entries), identity links
  (Okta ↔ Slack ↔ GitHub), approvals projection, audit events, cost ledger, eval results.
  - Temporal persistence and Langfuse each get their **own** Postgres instance or cluster. The
    factory database is never shared with them.
  - Postgres never holds workflow state (Temporal does) or artifact bodies (object storage does).
  - Production hosting (Amazon RDS / Aurora PostgreSQL vs CloudNativePG on EKS) is an open decision
    owned by WS-10.

### Agents, tools, and context

- **D-10 Native coding CLIs only.** Implementer (and optionally planner/reviewer) nodes invoke
  native CLIs in headless mode: `claude -p` / Claude Agent SDK, or `codex exec`. No reimplemented
  coding harness. Each invocation runs inside an AgentRun. Agent configuration files cannot be
  committed to target repositories; all instructions, settings, MCP configuration, and constraints
  are supplied externally by the factory (user-scope config inside the sandbox, CLI flags, env).
  CLI versions are pinned by image digest and change only through a Release Bundle.
- **D-11 AI/ML gateway routing.** Coding CLIs are configured to call an internal AI/ML gateway.
  Gateway configuration is part of the agent definition and is injected into the AgentRun. Local mode
  uses a dev/stub gateway. Gateway credentials are short-lived and scoped to one run.
- **D-12 Declarative tool/environment provisioning.** Definitions (base image, packages, setup
  scripts, env vars, caches, services, resource profile: CPU/memory requests and limits, workspace
  disk) live outside target repos in a Git-backed config repo, published into the Postgres registry
  as immutable versions. Local and production use the same format. Resource profiles derive from
  measured baselines per codebase type: requests at median peak, limits at p95 plus roughly 30%
  headroom, plus measured runtime overhead (gVisor or Kata VM overhead). Under Kata, memory requests
  follow the measured **host-side** footprint and are set equal to the limit when the p50 footprint
  ratio is ≥ 0.8, because Kata does not return guest-touched memory to the host by default (amended
  by accepted challenge WS-03 ADR-008, supported by WS-02 benchmark B-MEM-01). Disk is the most
  underestimated resource (node_modules, image layers, compiler caches can reach 20–40 GB).
- **D-13 Ecosystem awareness without full-repo vector indexing or a manual schema registry.** Use
  automatically derived sources: service catalog derived from existing metadata (for example a
  Backstage catalog, CODEOWNERS, deploy manifests), extracted API contracts (OpenAPI, protobuf,
  GraphQL, AsyncAPI, event schemas) from repos and CI, dependency and call graphs (static analysis,
  build graphs, service-mesh or tracing telemetry), and MCP/internal APIs exposing them. Indexed
  **lexical/structural code search** (for example Zoekt or Sourcegraph) exposed as an MCP tool is
  permitted and recommended; embedding indexes over whole repositories are not. Compatibility checks
  run in CI against extracted contracts and call sites. Local mode includes stubs.

### Memory (highest-importance subsystem)

- **D-14 The memory system is a first-class, factory-owned subsystem.** It is a primary determinant
  of whether the factory improves or degrades over time. It covers:
  - **Long-term memory**: rules/conventions learned from PR feedback, episodic records of past jobs
    and repairs, procedural playbooks per codebase/task type.
  - **Job working memory**: state carried across nodes, CLI invocations, repair cycles, and
    Continue-As-New boundaries, including how coding-CLI sessions are resumed or replaced with
    handoff summaries given that sandbox disks are ephemeral.
  - **Context budgeting**: how much of each context window goes to briefings, memory, constraints,
    and task content, and how that is enforced.
  Memory must be scoped, provenance-tracked, lifecycle-managed (candidate → active → deprecated),
  defended against poisoning, curated by admins through the UI (D-25), measurably evaluated, pinned per
  job for reproducibility, and deletable. Retrieval runs over memory entries only, never over whole
  repositories (consistent with D-13).
- **D-15 PR feedback loop.** When a factory PR receives review comments, the factory reads them,
  replies, applies fixes via a new AgentRun on the same PR branch, and writes candidate memory.
  Bounded repair loops apply per job. Only authorized humans (org members linked to Okta identities)
  can trigger repairs; comments from bots, including the factory itself, never do.

### Validation

- **D-16 QA/QE stage runs locally inside the sandbox.** Build (including container images where
  applicable, using a daemonless builder such as BuildKit rootless or buildah), start the
  application and its declared dependencies, confirm health, and for web apps run headless
  Playwright (DOM assertions, screenshots, traces). Artifacts go to object storage. Real GUI/desktop
  environments are out of scope. Remote dev/staging deploys are out of scope for v1. Per-codebase
  configuration via the tool/environment definition. How the app's dependencies (databases, queues,
  dependent services) are satisfied is a required design decision (sidecars, in-sandbox services,
  contract stubs), and Docker-socket-based approaches such as Testcontainers need an explicit
  answer.

### Platform

- **D-17 Observability is open source and self-hosted.**
  - **Langfuse OSS** (self-hosted; verify current architecture, which for v3 includes web + worker
    services, Postgres, ClickHouse, Redis/Valkey, and S3-compatible blob storage) for LLM/agent
    traces, prompt management, token/cost accounting, scores, and datasets.
  - **Grafana OSS stack**: Grafana, Loki (logs), Tempo (traces), Prometheus or Mimir (metrics),
    Grafana Alloy as the collector (verify Promtail's deprecation status; do not design on Promtail).
  - **OpenTelemetry is the correlation backbone**: trace context propagates from trigger → Temporal
    workflow/activities → Runner API → controller → sandbox → CLI → gateway, and every log line and
    Langfuse trace carries the same `job_id` and trace ID.
  - Loki label discipline: high-cardinality IDs (job, workflow, run) go in structured metadata, not
    labels.
  - No SaaS observability vendors.
- **D-18 Identity.** Okta OIDC for users, SCIM for group and user provisioning. Authorization uses
  Okta groups **and** a check of the requesting user's own permissions on the target repository (no
  confused-deputy: the factory never does for a user what that user could not do). Slack and GitHub
  identities are linked to Okta identities; an unlinked identity cannot approve or trigger. Local
  stub IdP supported.
- **D-19 Approvals.** Human gates support the control-plane UI, Slack interactive messages, and
  GitHub PR reviews/labels. All resolve to the same Temporal Update/Signal. First valid decision
  wins; duplicates are idempotent no-ops; all channels update to show who decided and where. Waits
  are durable but not unmanaged: reminders, escalation, expiry, and auto-cancel are policy-driven.
  When the base branch has moved by the time of approval, the workflow rebases and re-verifies
  before publishing. Risk classification that decides gate presence is **deterministic** (paths,
  services, diff size, CODEOWNERS, data classification); an LLM may raise risk, never lower it.
- **D-20 Local-first.** The full system runs locally with Docker or Podman before any cloud spend:
  one command starts Temporal, workers, Runner, local sandbox runner, Postgres, MinIO, Langfuse,
  the Grafana stack, the gateway stub, the IdP stub, a GitHub mock or local Git server, and
  ecosystem stubs. A developer can trigger a job, watch Temporal history, see the native CLI run in
  the isolated workspace, approve, comment on the PR, and inspect artifacts. A second local profile
  runs the **real AgentRun controller** on `kind` or `k3d` (with Kata where the host supports KVM) so
  the production execution path is exercised before EKS.
- **D-21 Security baseline.**
  - The sandbox never holds Git push credentials. The Publisher, outside the sandbox, pushes from
    the diff bundle.
  - Every credential in a sandbox is short-lived, scoped to one run, and assumed exfiltratable.
  - Egress is default-deny. Package installs go through an internal mirror/proxy (for example
    Artifactory or Nexus) or FQDN-aware policy (for example Cilium); plain IP-based NetworkPolicy
    cannot express registry allowlists.
  - Issue text, PR comments, repo contents, and tool output are untrusted input to an agent with a
    shell. Design prompt-injection containment around capability limits, not detection alone.
  - Transcripts and logs are redacted for secrets and PII before reaching Loki or Langfuse.
- **D-22 Production target.** AWS: EKS, S3, RDS/Aurora or CloudNativePG, KMS, IAM (IRSA or EKS Pod
  Identity). Temporal self-hosted on EKS vs Temporal Cloud is an open decision owned by WS-01 and
  WS-10; the preference for open source favors self-hosting.
- **D-23 Default implementation languages** (challengeable via ADR): Python for Temporal workers,
  LangGraph graphs (`temporalio`, `langgraph`), the control-plane API (FastAPI), and the
  Slack/GitHub integration services; Go for the AgentRun controller (kubebuilder /
  controller-runtime) and the Runner API; TypeScript for the control-plane frontend only. The
  reason for this split: every service that talks to the factory Postgres is Python, so there is
  one data-access layer and one migration tool (D-26). The Go services do not use the factory
  database; Runner idempotency lives on the AgentRun resource.
- **D-24 Companion GitHub App with a development variant.** The factory ships a GitHub App
  definition (as an app manifest in the factory repo) that is installable on any org or account.
  There are at least two registrations of it: a **dev app** that a developer installs on a personal
  GitHub org to test real triggers (issue opened → job → PR → review comment → repair) against a
  locally running factory, and production app(s) for the enterprise. Same code, same manifest,
  different registration, credentials, and webhook URL. Webhooks reach a laptop through a forwarding
  mechanism (for example smee.io, `gh webhook forward`, or a Cloudflare/ngrok tunnel; verify and
  choose). The local Git server (Gitea/Forgejo) remains for fully offline tests; the dev app is the
  path for testing against real GitHub.
- **D-25 Memory management is admin-only.** Every memory management capability in the control
  plane is restricted to members of the factory admin Okta group (name configurable; default
  `factory-admins`): the candidate queue, approving/rejecting/editing entries, promotion across
  scopes, conflict resolution, disabling and bulk disabling, quarantine and rollback, deletion,
  export, and memory analytics. Tenant admins and other roles get none of these unless they are also
  in the admin group.
  - Enforcement is in the control-plane API and its policy layer, not only by hiding UI. Every
    memory-management endpoint checks admin group membership server-side, and denials are audited.
  - Non-admins see no memory management navigation or actions. They keep **read-only** visibility of
    the memory entries that influenced a job they are already authorized to see (job detail and
    approval view), because approvers need that context.
  - Automated system writes (the Feedback Ingestor creating candidates) are unaffected. Any lifecycle
    transition that WS-04 would otherwise allow to happen automatically must either be performed by
    the system under an admin-configured policy or wait for an admin; nothing lets a non-admin
    change memory state.
  - Group membership changes via SCIM take effect on the next request (no long-lived cached grant).
- **D-26 Database management without connecting to the database.** No routine task (schema
  changes, seeding, data fixes, admin operations, investigation) requires a person to open a
  database connection.
  - **Data access and schema source of truth:** SQLAlchemy 2.0 models (typed `Mapped[]` style) in a
    single shared Python package used by every service that touches the factory database, with
    `pgvector-python` for vector columns. The DDL written in the spec is the reviewed target; the
    models and migrations must produce it exactly.
  - **Migrations:** Alembic, with autogenerate as a starting point and every migration reviewed
    in a PR. CI renders each migration to SQL (`alembic upgrade --sql`), lints it for unsafe
    operations (for example with `squawk`: locking `ALTER`s, non-concurrent index builds, volatile
    defaults), runs upgrade and downgrade against a disposable Postgres, and checks the models
    and the migrated schema have no drift. Zero-downtime changes follow expand → migrate data →
    contract across separate releases.
  - **Applying migrations is automatic:** locally, a one-shot `migrate` service in the compose
    stack runs before any service starts; in production, a Kubernetes Job runs as a deployment
    pre-sync/pre-upgrade hook (Argo CD `PreSync` or a Helm `pre-upgrade` hook; decide), holding a
    Postgres advisory lock so concurrent runs cannot collide. Services check on startup that the
    schema is **compatible** with the revision range they support (not strictly equal, so
    expand/contract migrations and long-pinned worker versions keep working) and refuse to start
    otherwise. (Amended by accepted CCR-10-01.)
  - **Seeding and data fixes:** reference data ships as migrations; dev/test seed data loads
    through the control-plane API (`factory dev seed`), not SQL scripts. One-off data fixes are
    written as reviewed, idempotent data migrations or admin API operations, never ad hoc SQL.
  - **Operations and visibility:** routine admin work happens through the control-plane UI, its
    API, and the `factory` CLI. A read-only Grafana Postgres data source (dedicated read-only role)
    serves dashboards. Migration status (current revision, pending, history) is visible in the
    control-plane admin area and exposed as a Prometheus metric.
  - **Break-glass only:** direct access exists for incidents, via short-lived IAM database
    authentication, approved and time-boxed, with full session audit. It is never part of a
    documented procedure other than the break-glass runbook.
  - Temporal's and Langfuse's databases are migrated by their own tooling (for example
    `temporal-sql-tool` and Langfuse's startup migrations; verify), also run automatically.

### Out of scope for v1

Remote dev/staging deploys, GUI/desktop environments in sandboxes, Firecracker snapshot warm pools,
full-repo embedding indexes, manual schema registries, committed agent files in target repos.

---

## 4. Functional scope carried forward

### Workflow types (router selects a subgraph per type)

Bugfix, feature / API change, docs / low-risk, hotfix, scheduled maintenance (dependency upgrades,
lint/codemod sweeps), PR-comment repair (first-class), QA/QE (first-class stage reused by others).

### Graph roles (nodes)

Intake/Router, Planner/Spec, Implementer, Reviewer, Skeptic/Challenger (optional), Verifier, QA/QE,
Human Gate, Publisher, Feedback Ingestor, Memory Writer, Context Assembler. Additional steps can be
inserted without changing core roles.

### Triggers

GitHub (issue opened or labeled, PR review comment, `/factory` command), Jira, Slack (shortcut or
slash command), the control-plane UI, a CLI, and schedules. All normalize into one `JobRequest`.

All GitHub integration goes through a **GitHub App** (D-24), never personal access tokens.

### User experience requirements

- Live progress per job (phase, current node, sandbox status, cost so far) within 60 s of trigger.
- Cancel, pause at next gate, and **steer**: send additional instructions to a running job.
- **Take over**: hand the user the branch, workspace snapshot archive, transcript, and plan so they
  can finish locally.
- Approval inbox with everything an approver needs: plan, diff, QA results and screenshots, risk
  rationale, memory entries used, cost.
- Release/version management, definition registries, eval results, and cost views for platform
  operators. Memory management tools for admin group members only (D-25).

### Visual design bar for the control plane

The control-plane UI must look modern, crafted, and distinctive: the kind of tool engineers
choose to keep open, in the lineage of the best developer tools (dense, fast, typographically
confident, keyboard-first). It must **not** carry the visual signatures of generic AI-generated
interfaces. Banned unless a specific screen justifies it in writing:

- Purple/blue/pink gradients, gradient text, glowing borders, aurora or mesh backgrounds.
- Glassmorphism and frosted cards as a default surface.
- Sparkle/magic-wand icons, "AI-powered" badges, emoji in headings or navigation.
- Grids of identical rounded cards with an icon, a title, and a one-line description.
- Vanity stat tiles ("1,234 jobs ✨ +12%") that answer no question.
- Everything centered, oversized hero sections, marketing-page layouts inside an app.
- Default component-library styling shipped unmodified (stock shadcn/ui look, default Inter at
  default weights, default shadows and radii everywhere).
- Decorative motion: bouncing, floating, shimmering, staggered fade-ins on every list.
- Lorem ipsum or vague placeholder data in mocks.

Required instead: a deliberate design system with its own tokens (type scale, spacing, a restrained
neutral palette with one considered accent, semantic status colors that are distinguishable in
both themes and for color-blind users), information density appropriate to operators, real
hierarchy through type and spacing rather than boxes, monospace for IDs, hashes, diffs, and logs,
light and dark themes designed together, motion used only to explain state change, a command
palette and keyboard shortcuts, and realistic data in every mock. WS-07 owns this and proves it with
a rendered prototype.

---

## 5. Corrections from the prior review (do not regress)

| Earlier assumption | Problem | Now |
|---|---|---|
| EBS via CSI ephemeral inline volumes with a StorageClass | EBS CSI does not support inline ephemeral volumes; inline volumes ignore StorageClasses | emptyDir on NVMe by default; generic ephemeral volumes for overflow (D-07) |
| gVisor decided | Docker builds and sidecars are fragile under gVisor; EKS needs custom AMIs; I/O overhead underestimated | Pluggable runtime, evidence-based default (D-06) |
| Graphs extensible without redeploy | New code under long-lived workflows breaks determinism | Worker Versioning + pinned Release Bundles; config vs code distinction (D-02, D-03) |
| Approvals wait indefinitely | Stale work, base-branch drift | Reminders, escalation, expiry, rebase-and-reverify (D-19) |
| LLM risk classification drives gates | Model can talk itself out of a gate | Deterministic rules; LLM can only raise (D-19) |
| Okta groups are sufficient authz | Confused deputy | Group + repo-permission check, identity linking (D-18) |
| Memory as a feature of the feedback loop | Poisoning, bloat, no evidence of benefit | Memory as a first-class subsystem with lifecycle and evals (D-14) |
| No evaluation harness | No way to know whether changes help | Eval harness gates Release Bundle promotion (D-03, WS-06) |
| Local runner bypasses controller | Production path untested before EKS | `kind`/`k3d` controller profile (D-20) |
| NetworkPolicy for egress | Cannot express FQDN allowlists | Mirror/proxy or FQDN-aware CNI (D-21) |
| No database named | Registries, memory, identity links, UI read models need a home | Postgres (D-09) |
| Phased plan "minimal slice" included everything | Contradiction | Explicit v0 slice defined in phase 3 |

---

## 6. Conventions for all spec output

### Files and structure

- Write only inside `spec/`. Your brief names your directory. Do not edit another workstream's files;
  raise issues in `spec/_coordination/` instead.
- Every section starts with: purpose, decisions summary (with D-IDs and ADR links), and a component
  diagram (Mermaid).
- Every section ends with: **Open questions** (who decides, what the options are, your
  recommendation), **Risks**, and **Interfaces consumed / provided** (by contract ID).

### Precision

- Name exact libraries and versions-at-time-of-writing, CLI flags, environment variables, Temporal
  primitives, CRD fields, API paths, table columns, metric names, label keys.
- Schemas and configuration are written as real, syntactically valid YAML, JSON Schema, SQL DDL,
  OpenAPI, protobuf, Python, Go, or TypeScript. No pseudo-YAML. Mark elided parts with comments, not
  ellipses inside structures.
- Sequence flows are Mermaid `sequenceDiagram`s that name the exact Temporal primitive at each step
  (activity, Update, Signal, timer, Continue-As-New, child workflow).
- State every failure mode you can think of for each flow and what happens: retry, compensation,
  operator alert, user-visible status.

### Verification of external facts

Technology facts change. For every non-trivial claim about third-party behavior (driver support,
API flags, version-specific features, limits, deprecations), check current official documentation,
release notes, or source with the web tools available to you, then tag it inline:

- `[verified: <url>, <date checked>]`
- `[unverified: <reason>]` when you could not confirm. Unverified claims must not be load-bearing
  without an explicit fallback.

The prior plan contained a load-bearing claim (EBS inline ephemeral volumes) that was false. Assume
others like it exist and look for them.

### Decisions

- Use ADRs for decisions with real alternatives: `spec/<section>/adr/NNN-title.md` with Context,
  Options, Decision, Consequences, Revisit-when.
- Challenges to decided constraints use the `Challenge: D-xx` ADR form (section 3).
- Do not invent features that contradict the decided constraints.

### Coordination files (shared, append-only)

| File | Use |
|---|---|
| `spec/_coordination/contract-change-requests.md` | Requests to change a frozen Phase 1 contract: ID, requester, change, rationale |
| `spec/_coordination/open-questions.md` | Product decisions that need a human, with recommendation |
| `spec/_coordination/cross-ws-dependencies.md` | "I assume WS-xx provides Y" statements |
| `spec/_coordination/verification-log.md` | External facts checked, with source and result |

Append entries with your workstream ID and a timestamp. Never delete another agent's entry.

---

## 7. Glossary

| Term | Meaning |
|---|---|
| Job | One unit of requested work, from trigger to terminal state. Has a `job_id`. Spans one or more Temporal workflow runs (Continue-As-New) |
| Release Bundle | Immutable versioned tuple determining factory behavior (D-03) |
| AgentRun | One isolated sandbox execution (CRD, D-05) |
| Runner API | Service boundary between workflows and the execution environment (D-04) |
| Agent definition | Versioned config for a CLI invocation: CLI, model, prompts, tools, MCP servers, gateway config |
| Environment definition | Versioned tool/provisioning config and resource profile (D-12) |
| Briefing | Generated context packet about the target and related services (D-13) |
| Memory entry | A unit of long-term memory with scope, provenance, and lifecycle state (D-14) |
| Gate | A human decision point backed by a Temporal Update/Signal (D-19) |
| Tenant | A team or org boundary with its own policy, quotas, and memory scope |
