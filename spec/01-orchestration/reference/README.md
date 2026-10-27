# WS-01 Reference Implementation

Executable reference for the orchestration design. It is specification, not production code. Activity
bodies are stubs (`replicant_orchestration/testing/stubs.py`); workflow code, engines, node types,
the graph model, the FGL evaluator, the codec routing, the lint, and the code generator are written to be
lifted into the `replicant-worker` repository.

## Layout

| Path | What |
|---|---|
| `replicant_orchestration/ids.py` | C-IDS derivations (test vectors reproduced) |
| `replicant_orchestration/dsl.py` | FGL v1 guard language |
| `replicant_orchestration/graphspec.py` | GraphDefinition model, modifiers, invariants I-1..I-7 |
| `replicant_orchestration/registry.py` | `NodeType`, `@node_type`, NodeCatalog export |
| `replicant_orchestration/context.py` | `NodeContext`: activities with logical IDs, Runner-call sequence, gates, interrupts |
| `replicant_orchestration/engines/` | `GraphEngine`, `PregelEngine` (LangGraph), `InterpreterEngine` (fallback) |
| `replicant_orchestration/nodes/` | core node types; `qa_routing.py` (WS-05, vendored); `license_scan.py` (worked example) |
| `replicant_orchestration/workflow.py` | `ReplicantJob`: handlers, validators, router prologue, waits, CAN, migration handoff, projections |
| `replicant_orchestration/maintenance.py` | `MaintenanceBatch` |
| `replicant_orchestration/state.py` | `JobState` and the Continue-As-New carry |
| `replicant_orchestration/activity_options.py` | activity catalog, queues, timeouts, retry policies |
| `replicant_orchestration/codec.py` | `TenantContextCodec` (serialization-context tenant routing) |
| `replicant_orchestration/worker.py` | `replicant-worker run | nodecatalog export | healthcheck` |
| `tests/test_units.py` | pure tests |
| `tests/test_scenarios.py` | 44 end-to-end scenarios on a local Temporal dev server (25 from the Phase 3 fix round) |
| `tests/compare_engines.py` | engine conformance |
| `tools/lint_activity_ids.py` | CI lint for explicit activity IDs |
| `tools/graph_codegen.py` | YAML → LangGraph module + Mermaid, with topology equivalence check |
| `tools/history_model.py` | history budget model calibrated by the `history_budget` scenario |

## Running (validated on macOS, Python 3.12, 2026-10-24)

```bash
uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python "temporalio==1.34.0" "langgraph==1.2.17" "opentelemetry-sdk==1.38.0" jsonschema pyyaml cryptography
.venv/bin/python -I tests/test_units.py
.venv/bin/python tools/lint_activity_ids.py replicant_orchestration
.venv/bin/python tools/graph_codegen.py ../graphs/definitions ../graphs/generated
# end-to-end (downloads the Temporal CLI dev server on first use); build N+1 includes the license node
REPLICANT_EXTRA_NODE_MODULES=replicant_orchestration.nodes.license_scan REPLICANT_GRAPH_ENGINE=langgraph-pregel \
  .venv/bin/python -I tests/test_scenarios.py --dump pregel.json
REPLICANT_EXTRA_NODE_MODULES=replicant_orchestration.nodes.license_scan REPLICANT_GRAPH_ENGINE=interpreter \
  .venv/bin/python -I tests/test_scenarios.py --dump interp.json
.venv/bin/python tests/compare_engines.py pregel.json interp.json
.venv/bin/python tools/history_model.py pregel.json
# dependency-outage group (R2-010, R2-011): activity retries shrunk to < 1 s (test-only knob; refused in staging/prod)
REPLICANT_TEST_FAST_RETRY=1 .venv/bin/python -I tests/test_scenarios.py dependency_outage dependency_giveup identity_outage
# Continue-As-New inside a long run wait
REPLICANT_HISTORY_CAN_EVENTS=150 .venv/bin/python -I tests/test_scenarios.py mid_run_can
```

The stub Runner validates every `RunSpec` the workflow builds against the C-RUNNER 3.0.0 schema, and the stub
projector validates every `ProjectionEvent` against C-MSG 2.0.0. A scenario fails on any violation.

Versions used: `temporalio` 1.34.0, `temporalio-langgraph` 0.1.0 (read, not used), `langgraph` 1.2.17,
Temporal CLI 1.9.1 / server 1.32.0 (dev server), Python 3.12.14.
