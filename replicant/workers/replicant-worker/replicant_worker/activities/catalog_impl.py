"""ACTIVITIES_BY_QUEUE for the replicant-worker deployment (replicant-jobs, replicant-runner-calls, replicant-llm-calls).

Every catalogued activity on these queues is registered: the v0 implementation, or a stub that fails fast with the
non-retryable `NotImplementedInV0` (off the v0 bugfix path), so no call ever waits on a queue with no handler.
Activities on replicant-publisher (publisher-worker) and replicant-projections (replicant-projector) live in those workers.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from replicant_worker.activities import core, feedback, gates, memory, notify, qa, runner, staging  # noqa: F401  (register)
from replicant_worker.activities.base import REGISTRY, not_in_v0
from replicant_worker.activities.options import CATALOG, Q_JOBS, Q_LLM, Q_RUNNER

QUEUES = (Q_JOBS, Q_RUNNER, Q_LLM)


def activities_by_queue() -> dict[str, list[Callable[..., Any]]]:
    out: dict[str, list[Callable[..., Any]]] = defaultdict(list)
    for name, d in CATALOG.items():
        q = d.klass.task_queue
        if q not in QUEUES:
            continue
        out[q].append(REGISTRY.get(name) or not_in_v0(name))
    return dict(out)


def implemented() -> list[str]:
    return sorted(n for n in CATALOG if n in REGISTRY and CATALOG[n].klass.task_queue in QUEUES)


ACTIVITIES_BY_QUEUE = activities_by_queue()
