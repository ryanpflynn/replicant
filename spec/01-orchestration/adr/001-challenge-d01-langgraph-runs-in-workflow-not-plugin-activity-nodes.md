# ADR-01-001: Challenge: D-01. LangGraph graphs run inside the workflow; node I/O runs as explicit-ID activities; the `temporalio-langgraph` plugin is not a runtime dependency

- **Status:** **Accepted** by the orchestrator (spec/00-contracts/FROZEN.md: "ADR-01-001 (LangGraph
  in-workflow, explicit activity IDs) accepted"), 2026-10-24. Raised by WS-01 as a challenge to D-01. The
  section "If rejected" is kept for the record.
- **Constraint challenged:** D-01, two clauses:
  1. "LangGraph defines workflow shapes and node logic, **integrated through Temporal's LangGraph
     integration**."
  2. "Each LangGraph node that does real work (I/O, LLM calls, Runner calls) **executes as a Temporal
     activity**."

## Context and evidence

The integration exists in two packages with identical code: `temporalio.contrib.langgraph` in
`temporalio==1.34.0` (extra `temporalio[langgraph]`) and the standalone `temporalio-langgraph==0.1.0`
(module `temporalio.langgraph`, PyPI classifier "Development Status :: 4 - Beta"). Both module docstrings say
"This package is experimental and may change in future versions". The docs label it Public Preview
[verified: introspected `temporalio==1.34.0` and `temporalio-langgraph==0.1.0` source, `diff -r` shows only
import-path differences, 2026-10-24; https://docs.temporal.io/develop/python/integrations/langgraph,
2026-10-24]. Reading the source (seven modules, 992 lines) shows three properties that conflict with other decided
constraints:

| # | Plugin behavior (source) | Conflicts with |
|---|---|---|
| E1 | Graphs are registered when the **Worker is constructed**: `LangGraphPlugin(graphs={name: StateGraph})`. At construction the plugin rewrites every node's `runnable.afunc` and registers one activity per node named `"<graph>.<node>"`. Workflows fetch graphs by name with `graph(name)` [verified: `_plugin.py` `__init__` and `graph()`, 2026-10-24] | **D-02**: inserting an existing node, changing parameters, and per-codebase modifications must need **no deploy**. Here a graph shape is fixed in the worker process. A config-only GraphDefinition change would need a worker restart at minimum, and a pinned job could not get a graph composed for its repository at start |
| E2 | Activity options come from node `metadata` and are merged **once, at construction** (`wrap_execute_activity(a, task_id=…, **opts)`). Every call of a node uses the same static kwargs. No per-invocation activity ID is passed, so Temporal's default sequence-number IDs are used [verified: `_activity.py` `wrap_execute_activity`, 2026-10-24] | **C-IDS §2.1 / ADR-008 / D-04**: every side-effecting activity needs an explicit logical ID `<node>.c<cycle>.n<ordinal>.<effect>`. A static `activity_id` in metadata would collide across cycles, and the default sequence ID restarts after Continue-As-New |
| E3 | An `execute_in: "activity"` node is **one** activity call: input → output (or `GraphInterrupt`) | **D-04 / ADR-002**: a Runner-call node must start the run, then *in workflow code* wait on a Signal hint with a timer, poll `runner.get_run`, and cancel on an orchestrator deadline. A single activity can do that only as a long-poll heartbeating activity, which ADR-002 rejected (one worker slot per run, heartbeat complexity) |

Experiments run for this ADR against Temporal CLI 1.9.1 / server 1.32.0 (dev server) with
`temporalio==1.34.0` and `langgraph==1.2.17` [verified: scratch experiments exp1 to exp3, reproduced by
`reference/tests/test_scenarios.py`, 2026-10-24]:

- A `StateGraph` built **inside workflow code from data** (no plugin) runs nodes that call
  `workflow.execute_activity(..., activity_id="plan.c0.n1.run")`. The explicit IDs appear in history,
  `interrupt()` inside an in-workflow node pauses the graph, `ainvoke(Command(resume=…))` resumes it, and
  `Replayer` replays every run without nondeterminism.
- The same graph compiled through the plugin with `execute_in: "workflow"` behaves identically. The
  plugin then contributes only sandbox pass-through configuration and per-run graph lookup.
- A graph can be resumed at an arbitrary node after Continue-As-New through a conditional edge from
  `START`. The plugin's own Continue-As-New mechanism (`cache()`) re-feeds node results keyed by a hash of
  their arguments. It carries node outputs in the CAN payload, which the 256 KiB carry budget (C-STATE §3)
  cannot afford for 30-day jobs.

## Options

1. **Plugin, `execute_in: "activity"` nodes** (D-01 literal). Fails E1, E2, and E3.
2. **Plugin, `execute_in: "workflow"` nodes, all I/O inside nodes through our own activity calls.**
   Resolves E2 and E3. E1 remains: graphs must be known at worker construction.
3. **Replicant engine adapter: compile a per-job `StateGraph` in the workflow from the bundle's
   GraphDefinition, run it with LangGraph's Pregel runtime in the workflow sandbox, and do all node I/O
   through `NodeContext` activities with explicit IDs. No runtime dependency on the plugin. A fallback
   interpreter executes the same GraphSpec without LangGraph (chosen).**

## Decision

Option 3 ([temporal-langgraph.md](../temporal-langgraph.md)). Proposed replacement text for D-01's two
clauses:

> LangGraph defines workflow shapes (`StateGraph`, conditional edges, interrupts) and is executed **inside
> the Temporal workflow** following the execution model of Temporal's LangGraph integration for
> workflow-executed nodes (in-workflow graph, `InMemorySaver` only, interrupts surfaced to workflow code).
> Replicant's own engine adapter compiles each job's graph from its pinned Release Bundle at workflow
> start. **Every unit of real work inside a node (I/O, LLM calls, Runner calls) executes as a Temporal
> activity with an explicit logical activity ID**; the node itself is deterministic workflow code that
> sequences those activities and the waits between them. The `temporalio-langgraph` plugin may be adopted
> behind the adapter when it supports per-job graphs and per-call activity options.

**Strict-superset argument.** Every guarantee D-01 asks for is kept: Temporal is the spine and the only
source of truth; LangGraph defines shapes and routing; nothing with I/O runs in workflow code; interrupts
back human gates; the integration's maturity is handled by an adapter plus fallback (D-01 itself requires
that). The replacement is stricter in two places: activity granularity is finer than "one activity per
node" (each Runner call, poll, and cancel is its own activity), and graph shapes become configuration (D-02).

## If rejected

Option 2 satisfies the literal wording: register Replicant's node types as `execute_in: "workflow"`
nodes through `LangGraphPlugin`. The adapter (`GraphEngine`) isolates the choice, so this becomes a third
engine implementation (`PluginEngine`) in a new worker build. E1 then forces one more constraint:
per-job graph composition must be expressed as graphs pre-registered at worker start (every bundle's graphs
are loaded at worker start, and a newly promoted config-only bundle needs a rolling restart of its workers,
which is a restart and not a deploy).

## Consequences

- No coupling to plugin internals (`_plugin.py`, `_activity.py`, private LangGraph constants such as
  `CONFIG_KEY_SCRATCHPAD`, which the plugin imports from `langgraph._internal`). Replicant depends on
  LangGraph's public API (`StateGraph`, `add_conditional_edges`, `interrupt`, `Command`, `InMemorySaver`,
  `ainvoke(version="v2")`) and on Temporal core.
- The engine is fixed per worker build (`build_config.ENGINE`, NodeCatalog `spec.engine`). Switching engines
  is a new worker deployment version (CC-2) and never affects running jobs.
- A conformance suite runs every scenario on both engines and requires identical side-effect activity ID
  sequences ([temporal-langgraph.md §8](../temporal-langgraph.md#8-conformance-and-tests)).

## Revisit when

- The plugin accepts graphs at workflow start (per-run registration) and per-invocation activity options,
  including `activity_id`.
- LangGraph's Pregel runtime changes in a way that breaks in-workflow determinism (the conformance and
  replay suites catch this on the LangGraph upgrade PR, before any build ships).
