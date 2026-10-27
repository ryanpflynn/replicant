"""InterpreterEngine: the fallback. Walks a GraphSpec with a deterministic loop; no LangGraph.

Semantics match PregelEngine exactly for what Replicant uses:
* a node runs via host.run_node (same NodeContext, same activities, same IDs);
* single-successor and switch edges route via host.route (same FGL guards);
* static fan-out from one node to N single-node branches that share one join edge runs the
  branches concurrently (asyncio.gather, deterministic under Temporal's event loop), then the join;
* interrupt: the node raises; the interpreter returns control to the host; on resume the node is
  re-entered and `ctx.interrupt` returns the resume value (LangGraph's node re-execution rule).
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..graphspec import TERMINAL_PREFIX, GraphSpec
from .base import EngineResult


class _Interrupted(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("interrupt")
        self.payload = payload


class InterpreterEngine:
    name = "interpreter"

    def __init__(self, host: Any) -> None:
        self.host = host
        self.spec: GraphSpec | None = None
        self._joins: dict[frozenset[str], str] = {}
        self._interrupted: str | None = None
        self._resume: tuple[str, Any] | None = None

    def compile(self, spec: GraphSpec) -> None:
        self.spec = spec
        self._joins = {frozenset(e.source): e.to for e in spec.edges if len(e.source) > 1}  # type: ignore[misc]

    def _interrupt_fn(self, key: str):
        def fn(payload: dict[str, Any]) -> Any:
            if self._resume is not None and self._resume[0] == key:
                value = self._resume[1]
                self._resume = None
                return value
            raise _Interrupted(payload)
        return fn

    async def start(self, start_at: str) -> EngineResult:
        self._resume = None
        return await self._walk(start_at)

    async def resume(self, value: Any) -> EngineResult:
        assert self._interrupted is not None
        self._resume = (self._interrupted, value)
        return await self._walk(self._interrupted)

    async def _walk(self, cur: str) -> EngineResult:
        while True:
            if cur.startswith(TERMINAL_PREFIX):
                return EngineResult(kind="terminal", outcome=cur[len(TERMINAL_PREFIX):])
            try:
                await self.host.run_node(cur, self._interrupt_fn(cur))
            except _Interrupted as i:
                self._interrupted = cur
                return EngineResult(kind="interrupt", node_key=cur, payload=i.payload)
            if self._resume is not None and self._resume[0] == cur:
                self._resume = None  # the node completed without consuming the resume value
            nxt = self.host.route(cur)
            if len(nxt) == 1:
                cur = nxt[0]
                continue
            join = self._joins.get(frozenset(nxt))
            if join is None:
                raise RuntimeError(f"fan-out from {cur} to {nxt} has no join edge")
            no_interrupt = self._interrupt_fn("__parallel__")
            await asyncio.gather(*[self.host.run_node(b, no_interrupt) for b in nxt])
            cur = join
