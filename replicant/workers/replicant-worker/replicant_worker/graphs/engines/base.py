"""Graph engine types shared by the workflow host and InterpreterEngine.

InterpreterEngine (graphs/engines/interpreter.py) is the only graph engine (ORCH-D1, docs/v0/DEVIATIONS.md):
it walks the GraphSpec with a small deterministic loop inside the workflow; all I/O happens in
explicit-ID activities. The engine is advertised in the NodeCatalog (`spec.engine`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


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

