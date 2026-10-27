"""Graph engine adapter interface (temporal-langgraph.md section 3).

Replicant's workflow code talks only to `GraphEngine`. Two implementations exist and must be
behaviorally identical (same activity commands, in the same order for sequential nodes):

* `PregelEngine` (default): compiles the GraphSpec to a LangGraph `StateGraph` and runs it
  inside the workflow (all nodes are workflow-side; I/O happens in explicit-ID activities).
* `InterpreterEngine` (fallback): walks the same GraphSpec with a small deterministic loop and no
  LangGraph runtime dependency.

The engine is fixed per worker build (NodeCatalog `spec.engine`), so a running job never changes
engine; switching engines is a new worker deployment version (CC-2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from ..graphspec import GraphSpec


class BoundaryReached(Exception):
    """Raised before running node `node_key` when a Continue-As-New boundary applies there.
    kind: phase | repair_cycle | history | migration | revoked."""

    def __init__(self, node_key: str, kind: str) -> None:
        super().__init__(f"boundary {kind} before {node_key}")
        self.node_key = node_key
        self.kind = kind


@dataclass
class EngineResult:
    kind: str  # terminal | interrupt
    outcome: str | None = None  # terminal outcome (without the "end:" prefix)
    node_key: str | None = None  # interrupted node
    payload: dict[str, Any] | None = None  # interrupt payload


# host callbacks the engines use
RunNode = Callable[[str, Callable[[dict[str, Any]], Any]], Awaitable[None]]
Route = Callable[[str], list[str]]  # node key -> next targets (guards evaluated by the host)


class GraphEngine(Protocol):
    name: str

    def compile(self, spec: GraphSpec) -> None: ...

    async def start(self, start_at: str) -> EngineResult: ...

    async def resume(self, value: Any) -> EngineResult: ...
