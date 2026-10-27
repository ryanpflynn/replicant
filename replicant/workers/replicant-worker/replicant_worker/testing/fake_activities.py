"""`FakeActivities`: in-process fakes for every activity in the WS-01 catalog, grouped by task queue
(XD-FIX-R1-267, WS-11). A thin facade over `replicant_worker.testing.stubs.FakeWorld`, so WS-11 and other
workstreams get the same scripted behaviour the WS-01 scenarios use. No environment variable selects task queues:
each activity's queue comes from `replicant_worker.activities.options.CATALOG`.

    fakes = FakeActivities.from_catalog(CATALOG)            # every catalogued activity, default behaviour
    Worker(client, task_queue="replicant-jobs", activities=fakes.for_queue("replicant-jobs"))
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from replicant_worker.activities.options import CATALOG, ActivityDef
from replicant_worker.testing.stubs import FakeWorld


class FakeActivities:
    def __init__(self, world: FakeWorld, by_queue: Mapping[str, list[Callable[..., Any]]]) -> None:
        self.world = world  # scripted outcomes, failures, outages; records every call (world.calls)
        self._by_queue = {q: list(fns) for q, fns in by_queue.items()}

    @classmethod
    def from_catalog(cls, catalog: Mapping[str, ActivityDef] = CATALOG, *, world: FakeWorld | None = None,
                     client: Any = None) -> "FakeActivities":
        """Fakes for exactly the activities in `catalog`. `client` (a temporalio Client) lets the fake Runner send
        run.status_changed hints and lets the fake release activity apply pinned overrides; without it hints
        are off and the fake Runner is polled only."""
        w = world or FakeWorld()
        if client is None:
            w.send_hints = False
        by_queue = w.activities(client)
        wanted = {name: d.klass.task_queue for name, d in catalog.items()}
        out: dict[str, list[Callable[..., Any]]] = {}
        for q, fns in by_queue.items():
            keep = [f for f in fns if getattr(f, "__temporal_activity_definition").name in wanted]
            if keep:
                out[q] = keep
        missing = set(wanted) - {getattr(f, "__temporal_activity_definition").name for fs in out.values() for f in fs}
        if missing:
            raise KeyError(f"no fake for catalogued activities: {sorted(missing)}")
        return cls(w, out)

    def for_queue(self, task_queue: str) -> list[Callable[..., Any]]:
        return list(self._by_queue.get(task_queue, []))

    @property
    def queues(self) -> list[str]:
        return sorted(self._by_queue)
