# replicant-worker

ReplicantJob / MaintenanceBatch workflows, the interpreter graph engine, the core node
types and the v0 activities. Ported from `spec/01-orchestration/reference` (`replicant_orchestration` ->
`replicant_worker`, layout of `spec/11-local-dev/repo-layout.md`):

| Path | What |
|---|---|
| `replicant_worker/workflows/` | `replicant_job.py` (ReplicantJob), `maintenance.py`, `context.py` (NodeContext), `state.py` |
| `replicant_worker/graphs/` | FGL `dsl.py`, `graphspec.py`, `engines/` (base, interpreter) |
| `replicant_worker/nodes/` | core node types, WS-05 `qa_routing.py`, `license_scan.py` (worked example) |
| `replicant_worker/activities/` | `options.py` (the CATALOG), v0 implementations (`core`, `staging`, `runner`, `qa`, `gates`, `notify`), `catalog_impl.py` |
| `replicant_worker/local_bundle/` | the v0 definition/policy set (agents, go-service environment, router/repair/polling/gate policies, graphs) |
| `replicant_worker/testing/` | the reference stubs (`FakeWorld`) and `FakeActivities` |
| `replicant_worker/resources/spec/` | vendored, unmodified spec files the code and tests read |

v0 settings (plan.md §1.2-1.3): bugfix on InterpreterEngine (the only engine, ORCH-D1) with the shipped `bugfix.yaml`; deterministic router rules only;
gate rules local (plan gate off, publish gate on); 0 repair cycles; `memory.*` return a pinned empty snapshot; no
payload codec. The worker registers Worker Deployment version `replicant-worker:$REPLICANT_WORKER_BUILD_ID` and, with
`REPLICANT_WORKER_SET_CURRENT_ON_START=true`, sets it current. ReplicantJob is PINNED.

## Run

```bash
podman build -f workers/replicant-worker/Dockerfile -t localhost/replicant/replicant-worker:dev .   # from replicant/
replicant-worker run | healthcheck | nodecatalog export | local-bundle spec|export|publish | grant-jwks
```

## Test

```bash
uv run --package replicant-worker pytest workers/replicant-worker/tests                 # activity unit tests + smoke
uv run --package replicant-worker python -I workers/replicant-worker/tests/test_units.py
REPLICANT_EXTRA_NODE_MODULES=replicant_worker.nodes.license_scan \
  uv run --package replicant-worker python -I workers/replicant-worker/tests/test_scenarios.py
REPLICANT_TEST_FAST_RETRY=1 uv run ... tests/test_scenarios.py dependency_outage dependency_giveup identity_outage
REPLICANT_HISTORY_CAN_EVENTS=150 uv run ... tests/test_scenarios.py mid_run_can
uv run --package replicant-worker python workers/replicant-worker/tools/lint_activity_ids.py workers/replicant-worker/replicant_worker
```

`tests/test_worker_smoke.py` runs the real worker (real activities, InterpreterEngine, pinned versioned workers) through the
bugfix graph on a Temporal dev server against contract fakes of runner-local, the gateway shim, control-plane-api,
the publisher and the projector.
