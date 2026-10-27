"""PregelEngine: GraphSpec -> LangGraph StateGraph, executed inside the Temporal workflow.

This follows the execution model of Temporal's LangGraph integration for `execute_in: "workflow"`
nodes (graph and conditional edges run in the workflow; `InMemorySaver` is the only checkpointer;
interrupts surface to workflow code; resume with `Command(resume=...)`), without depending on the
`temporalio-langgraph` plugin's internals, because the plugin registers graphs at Worker
construction and fixes activity options per node (temporal-langgraph.md section 2.3).

What the InMemorySaver stores and why it cannot diverge from Temporal history: it holds LangGraph
channel values and pending-interrupt bookkeeping for the current workflow *run* only, in worker
memory. It is never persisted or shared. On replay (cache eviction, worker restart) the workflow
re-executes from history, the graph runs again, every activity result comes from history, and the
saver is rebuilt identically. After Continue-As-New the new run starts with an empty saver and
resumes from JobState.resume_at, so no checkpoint ever crosses a run. Its content is a pure
function of history, so it cannot diverge.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt
    from typing_extensions import TypedDict

from ..graphspec import TERMINAL_PREFIX, GraphSpec
from .base import EngineResult

# Explicit so behavior never depends on the worker environment: LangGraph 1.2.17 defaults to 10,007, overridable
# by LANGGRAPH_DEFAULT_RECURSION_LIMIT. Graphs are bounded by maxVisits and the repair budget (invariant I-4).
RECURSION_LIMIT = 10_000


class ReplicantGraphState(TypedDict, total=False):
    start_at: str  # node to start (or resume) at; set by the host
    terminal: str | None  # terminal outcome once reached
    trail: Annotated[list[str], operator.add]  # node keys completed in this graph invocation


def _sink_name(target: str) -> str:
    return "end__" + target[len(TERMINAL_PREFIX):]


def _name(target: str) -> str:
    return _sink_name(target) if target.startswith(TERMINAL_PREFIX) else target


# ---- builders shared by PregelEngine.compile and the generated per-shape modules (graphs/generated) ----
def node(host: Any, key: str):
    """A graph node: delegates to the host (ReplicantJob), which runs the NodeType with a NodeContext."""
    async def fn(state: ReplicantGraphState) -> dict[str, Any]:
        await host.run_node(key, interrupt)
        return {"trail": [key]}
    fn.__name__ = f"node_{key}"
    return fn


def sink(outcome: str):
    """Terminal sink `end:<outcome>`: records the outcome, then END."""
    async def fn(state: ReplicantGraphState) -> dict[str, Any]:
        return {"terminal": outcome}
    return fn


def router(host: Any, key: str):
    """Conditional edge for a switch: FGL guards evaluated by the host against JobState."""
    async def fn(state: ReplicantGraphState) -> str:  # conditional edges must be async (workflow sandbox)
        return _name(host.route(key)[0])
    return fn


async def start_router(state: ReplicantGraphState) -> str:
    """START -> JobState.resume_at (or the entry): how a graph resumes at a node after Continue-As-New."""
    return state["start_at"]


def build_state_graph(spec: GraphSpec, host: Any) -> StateGraph:
    """GraphSpec -> uncompiled StateGraph (pure; deterministic for a given composed spec)."""
    g: StateGraph = StateGraph(ReplicantGraphState)
    for key in spec.nodes:
        g.add_node(key, node(host, key))
    terminals = sorted({t for e in spec.edges for t in e.targets if t.startswith(TERMINAL_PREFIX)})
    for t in terminals:
        g.add_node(_sink_name(t), sink(t[len(TERMINAL_PREFIX):]))
        g.add_edge(_sink_name(t), END)
    g.add_conditional_edges(START, start_router, list(spec.nodes))
    for e in spec.edges:
        if len(e.source) > 1:  # join: wait for all branches
            g.add_edge(list(e.source), _name(e.to))  # type: ignore[arg-type]
        elif e.switch:
            g.add_conditional_edges(e.source[0], router(host, e.source[0]), sorted({_name(t) for t in e.targets}))
        else:
            g.add_edge(e.source[0], _name(e.to))  # type: ignore[arg-type]
    return g


class PregelEngine:
    name = "langgraph-pregel"

    def __init__(self, host: Any) -> None:
        self.host = host
        self._app: Any = None
        self._cfg: dict[str, Any] = {}
        self._invocations = 0

    def compile(self, spec: GraphSpec) -> None:
        self._app = build_state_graph(spec, self.host).compile(checkpointer=InMemorySaver())

    async def start(self, start_at: str) -> EngineResult:
        self._invocations += 1
        info = workflow.info()
        self._cfg = {"configurable": {"thread_id": f"{info.workflow_id}:{info.run_id}:{self._invocations}"},
                     "recursion_limit": RECURSION_LIMIT}
        out = await self._app.ainvoke({"start_at": start_at, "terminal": None, "trail": []},
                                      self._cfg, version="v2")
        return self._result(out)

    async def resume(self, value: Any) -> EngineResult:
        out = await self._app.ainvoke(Command(resume=value), self._cfg, version="v2")
        return self._result(out)

    @staticmethod
    def _result(out: Any) -> EngineResult:
        if out.interrupts:
            payload = out.interrupts[0].value
            return EngineResult(kind="interrupt", node_key=payload["node_key"], payload=payload)
        return EngineResult(kind="terminal", outcome=out.value.get("terminal"))
