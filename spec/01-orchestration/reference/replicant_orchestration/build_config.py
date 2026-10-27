"""Per-build constants. Imported as a sandbox pass-through module, so it is read once per worker
process, not per workflow run. The values are fixed for a worker deployment version (they are
part of the image and advertised in its NodeCatalog), which keeps workflow code deterministic.
"""
import os

# langgraph-pregel (default) or interpreter (fallback). Changing it is a new worker build (CC-2).
ENGINE = os.environ.get("REPLICANT_GRAPH_ENGINE", "langgraph-pregel")
DEPLOYMENT_NAME = "replicant-worker"

# Node modules compiled into this build in addition to nodes.core. A real build lists them statically
# in nodes/__init__.py; the variable lets the reference tests build "build N" (without) and "build N+1"
# (with replicant_orchestration.nodes.license_scan) from one source tree.
EXTRA_NODE_MODULES = [m for m in os.environ.get("REPLICANT_EXTRA_NODE_MODULES", "").split(",") if m]

# History ceiling for Continue-As-New at safe points, in addition to the server's suggestion
# (limit.historyCount.suggestContinueAsNew, default 4,096). Tests lower it to force mid-run CAN.
HISTORY_CAN_EVENTS = int(os.environ.get("REPLICANT_HISTORY_CAN_EVENTS", "8000"))

# Test-only: shrink activity retry budgets so dependency-outage scenarios exhaust retries in seconds
# (tests/test_scenarios.py, the `fast_retry` group). worker.py refuses to start in staging or prod with it set.
FAST_RETRY = os.environ.get("REPLICANT_TEST_FAST_RETRY") == "1"

# XD-W2-11-01: local/CI only. Runs get agents.build_run_plan(deterministic=true) (fixed seeds, cassette replay).
# worker.py refuses it in staging and prod.
DETERMINISTIC_RUNS = os.environ.get("REPLICANT_DETERMINISTIC_RUNS") == "true"
