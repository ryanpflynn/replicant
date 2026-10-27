# WS-01: Orchestration, Graphs, and Workflow Versioning

**Output directory:** `spec/01-orchestration/`
**Constraints most relevant:** D-01, D-02, D-03, D-04, D-15, D-19

## Purpose

Design how Temporal and LangGraph together execute every workflow type, how workflows extend
without control-plane changes, and how workflow code and configuration are versioned so that jobs
lasting up to 30 days always replay correctly while new versions roll out safely.

## Required outputs

### 1. Integration design (`temporal-langgraph.md`)

- Verify the current state of Temporal's LangGraph integration (package name, maturity, supported
  LangGraph features, how nodes map to activities, how interrupts map to Temporal messages, what
  state it persists and where). Tag every claim.
- Specify the **adapter layer**: the factory's own interface between graph definitions and Temporal,
  so the factory survives changes or removal of the integration. Include the fallback
  implementation: a generic Temporal workflow that interprets a compiled graph definition.
- Determinism rules for graph code: what may run in workflow code vs activities, how LLM calls,
  time, randomness, and config reads are kept out of workflow code.
- How graph state is represented inside Temporal (workflow-local state, size budget, what is offloaded
  to object storage by reference).

### 2. Graph patterns (`graphs/`)

Real LangGraph Python code (`StateGraph`, typed state, nodes, conditional edges, `interrupt`) plus a
Mermaid diagram and a sequence diagram per shape. Required shapes:

- **Bugfix**: router → context assembly → plan → (risk-gated) approval → implement → QA → review →
  verifier → publish.
- **Feature / API change**: adds skeptic/challenger, contract-compatibility check, consumer impact
  briefing, mandatory approval.
- **PR-comment repair**: triggered by a review event on an existing factory PR; triage comments,
  reply, repair AgentRun on the same branch, QA, push via publisher, memory candidate write;
  bounded loop with explicit exit conditions.
- **Hotfix** and **docs / low-risk** as variants showing gate differences.
- **Scheduled maintenance** as a fan-out (one parent, child workflows per repo) with concurrency
  limits.

For each: where Runner API calls sit, where Updates/Signals sit, timeouts, retry policies (real
`RetryPolicy` values), compensation on failure, and what the user sees at each step.

### 3. Extensibility (`extensibility.md`)

- The node registry: how nodes are declared, typed (inputs/outputs), and discovered by workers.
- Graph definitions as configuration: the format (for example YAML/JSON compiled to a LangGraph
  graph at workflow start), validation, and how per-codebase rules select or modify a graph.
- **Worked example:** add a "license and dependency policy scan" pre-step before implementation for
  one tenant's Go services. Show (a) the config-only path when the node already exists and (b) the
  path when the node is new code (new worker deployment version, no control-plane change).
- Rules for inserting, replacing, and conditionally skipping nodes; guarantees around core roles.

### 4. Versioning (`versioning.md`)

- Temporal Worker Versioning design: Worker Deployments, deployment versions, pinned vs auto-upgrade
  per workflow type, ramping, draining, and retirement. Verify current API names and semantics.
- How a workflow is pinned to a Release Bundle at start, and how the bundle's graph version,
  worker deployment version, and configuration versions are resolved and recorded.
- Continue-As-New strategy: which boundaries (repair cycle, phase completion, history threshold),
  what state carries over, how `job_id` continuity is kept.
- Patching policy: when `workflow.patched()` is acceptable vs requiring a new deployment version.
- Long-wait handling: a job waiting 30 days at a gate on version N while version N+3 is current.
  Specify exactly what happens on approval (continue on N, or Continue-As-New onto a newer bundle with
  the user's consent; define the rule and the UI affordance).
- Replay testing: how every deployment version is replay-tested against recorded histories in CI
  before ramping (`Replayer`, history export from production).
- The operations the UI exposes (WS-07 designs the screens; you define the operations and their
  Temporal-level effects): list versions, view pinned workflow counts, ramp, pause ramp, rollback,
  drain, retire, migrate eligible workflows.

### 5. Router (`router.md`)

Classification of `JobRequest` into a workflow type and subgraph: deterministic rules first,
LLM-assisted classification only for intent parsing, confidence thresholds, and a human
clarification path. Router output schema.

### 6. Human gates (`gates.md`)

Gate implementation inside the graph: interrupt → Temporal Update with validator (authorization
check by calling the identity service as an activity before accepting, or via validator constraints;
justify), first-wins semantics, durable timers for reminders/escalation/expiry, and the
rebase-and-reverify path on approval after base-branch drift. Coordinate channel delivery with
WS-07.

### 7. Temporal platform (`temporal-platform.md`)

Namespaces (per environment; tenancy via search attributes and task queues, or per-tenant namespaces:
decide), task queues, worker topology and scaling, rate limits and concurrency controls per tenant,
history and payload limits with numbers, payload codec (encryption with KMS keys, and a codec server
for the Temporal UI), advanced visibility backend, and an initial position on self-hosted vs Temporal
Cloud for WS-10.

## Must answer

- What happens if the LangGraph integration is abandoned upstream?
- How does a running job pick up a fix to a bug in node code it has not reached yet?
- How is a graph change tested before any production job uses it?
- What is the maximum history a 30-day job with 5 repair cycles produces, and how is it bounded?

## Acceptance criteria

- Every shape has runnable-looking LangGraph code, a graph diagram, and a sequence diagram with
  Temporal primitives named.
- The pre-step example shows both the config-only and new-code paths end to end.
- The versioning design covers ramp, rollback, drain, and long waits with no undefined case.
- All integration claims are verified or flagged with a fallback.
