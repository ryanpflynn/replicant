# replicant-temporal

Shared Temporal plumbing (WS-11 layout `libs/python/replicant-temporal`): client factory, search-attribute
helpers, Worker Deployment versioning helpers, the `_replicant-ctx` interceptor, worker healthcheck and runner.
Used by replicant-worker, publisher-worker, replicant-projector and control-plane-api (CONTROL).

```python
from replicant_temporal import TemporalSettings, connect
client = await connect()                                   # env: REPLICANT_TEMPORAL_ADDRESS / _NAMESPACE / _TLS*
from replicant_temporal.search_attributes import SA, job_start_attributes, dev_server_args
from replicant_temporal.versioning import deployment_config, set_current_version, build_id_from_env
from replicant_temporal.interceptors import ReplicantContextInterceptor, ReplicantContext, current_context, use_context
from replicant_temporal.health import worker_healthcheck
from replicant_temporal.runner import run_workers
from replicant_temporal.queues import Q_JOBS, Q_RUNNER, Q_LLM, Q_PUBLISHER, Q_PROJECTIONS
```

Test: `uv run pytest libs/python/replicant-temporal/tests` (starts a Temporal dev server through the SDK).
