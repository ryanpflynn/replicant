# ADR-01-008: Graph shapes are registry configuration, composed per job by an activity and compiled in the workflow

- **Status:** Accepted (WS-01, 2026-10-24)
- **Constraints:** D-02 ("configuration-level changes … need no deploy"), D-03, C-DEF CC-1..CC-4

## Context

D-02 separates configuration (insert an existing node, parameters, subgraph selection, per-codebase
constraints: no deploy) from code (new node code: a worker deployment version, never a control-plane
change). The graph shape must therefore be data, and the node behaviors code.

## Options

1. **Graphs as Python modules in the worker.** Every shape change is a deploy. Violates D-02.
2. **Graphs as data interpreted dynamically, with arbitrary expressions (Python `eval`, Jinja).** Flexible,
   but non-deterministic and unsafe in workflow code.
3. **GraphDefinition YAML (registry, versioned, digest-pinned in the bundle) + codebase-binding graph
   modifiers + a closed guard language (FGL v1); composition and validation in an activity; compilation to
   a LangGraph `StateGraph` in the workflow (chosen).**

## Decision

- **Authoring:** `graphs/*.yaml` in the replicant-config repo (kind schema
  [graph-definition.v1.json](../schemas/graph-definition.v1.json)). Nodes reference NodeCatalog `nodeType`s.
  Guards use FGL v1 (null-safe, total, no I/O, ≤ 64 AST nodes).
- **Publish-time checks** (registry, C-DEF §4.2): schema; compile against the NodeCatalog of every worker
  build the graph declares compatibility with; invariants I-1..I-7 ([extensibility.md §4](../extensibility.md#4-invariants-and-core-role-guarantees)).
- **Bundle build:** pins the graph per workflow type plus the `codebase_bindings` policy, whose
  `graphModifiers` (WS-01 sub-schema) apply per repository.
- **Job start:** activity `graph.compose(release_bundle_id, workflow_type, repo)` loads the pinned
  documents (digest-verified), applies the repository's modifiers, re-runs the invariants on the composed
  graph, and returns the composed spec plus `composed_digest`. The result is recorded in history, so replay
  never re-reads the registry. The workflow compiles it to a `StateGraph` (pure, deterministic).
- **After Continue-As-New:** the activity runs again and must return the same `composed_digest` (all inputs
  are pinned). A mismatch fails the job with outcome `graph_digest_mismatch` (renamed by R1-009), which detects registry corruption.

## Consequences

- Inserting `license_scan` before `implement` for one tenant's Go repos is a binding change, then a new
  tenant-derived bundle, eval, and assignment. No deploy (worked example:
  [extensibility.md §6](../extensibility.md#6-worked-example-license-and-dependency-policy-scan)).
- FGL semantic changes and new node types are code (CC-2).
- Composition cost is one activity per run (about 10 to 30 KiB result), within the payload budget.

## Revisit when

- Graphs need constructs FGL cannot express (loops with data-dependent fan-out, map-reduce over lists).
  Such constructs would be new node types (code), not language extensions.
