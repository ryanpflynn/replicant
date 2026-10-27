"""GraphDefinition model, composition with graph modifiers, and compile-time invariants.

This module is pure Python with no Temporal imports. It is used in three places:

1. the registry publish path (control-plane-api) and config-repo CI: `validate_definition`;
2. activity `graph.compose` (replicant-worker, replicant-jobs queue): `compose` + `validate_composed`,
   whose JSON result is recorded in workflow history;
3. workflow code: `GraphSpec.from_json` (pure) before compiling to an engine.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from replicant_worker.graphs.dsl import FglError, Guard
from replicant_worker.ids import NODE_KEY_RE

TERMINAL_PREFIX = "end:"
PHASES = frozenset({
    # C-IDS section 6 vocabulary plus the values requested in CCR-01-02
    "intake", "routing", "clarification", "context", "planning", "plan_gate", "implementing", "qa",
    "review", "verify", "publish_gate", "rebase_reverify", "publishing", "pr_review", "repairing",
    "memory", "finalizing", "done",
    "pre_checks", "fan_out", "budget_gate", "migrating", "takeover",
})
DIFF_PRODUCING_ROLES = frozenset({"implementer"})
BOUNDARY_KINDS = frozenset({"phase", "repair_cycle"})


class GraphValidationError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Branch:
    when: str
    to: str


@dataclass
class EdgeSpec:
    source: list[str]  # one node, or several for a join (wait-for-all) edge
    to: str | None = None
    switch: list[Branch] = field(default_factory=list)
    default: str | None = None

    @property
    def targets(self) -> list[str]:
        t = [b.to for b in self.switch]
        if self.to:
            t.append(self.to)
        if self.default:
            t.append(self.default)
        return t


@dataclass
class NodeSpec:
    key: str
    type: str
    phase: str
    config: dict[str, Any] = field(default_factory=dict)
    when: str | None = None
    max_visits: int | None = None
    origin: str = "graph"  # graph | modifier:<binding ref>


@dataclass
class Boundary:
    kind: str  # phase | repair_cycle
    before: str


@dataclass
class GraphSpec:
    name: str
    version: int
    digest: str
    workflow_type: str
    entry: str
    nodes: dict[str, NodeSpec]
    edges: list[EdgeSpec]
    boundaries: list[Boundary] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    modifiers_applied: list[str] = field(default_factory=list)

    # ---- serialization (the composed spec crosses the activity boundary as JSON) ----
    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["nodes"] = [asdict(n) for n in self.nodes.values()]
        return d

    @staticmethod
    def from_json(d: dict[str, Any]) -> "GraphSpec":
        nodes = {n["key"]: NodeSpec(**n) for n in d["nodes"]}
        edges = [
            EdgeSpec(source=list(e["source"]), to=e.get("to"),
                     switch=[Branch(**b) for b in e.get("switch", [])], default=e.get("default"))
            for e in d["edges"]
        ]
        return GraphSpec(
            name=d["name"], version=d["version"], digest=d["digest"], workflow_type=d["workflow_type"],
            entry=d["entry"], nodes=nodes, edges=edges,
            boundaries=[Boundary(**b) for b in d.get("boundaries", [])],
            params=d.get("params", {}), modifiers_applied=list(d.get("modifiers_applied", [])),
        )

    def composed_digest(self) -> str:
        body = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(body).hexdigest()

    # ---- topology helpers ----
    def successors(self, key: str) -> list[str]:
        out: list[str] = []
        for e in self.edges:
            if key in e.source:
                out.extend(e.targets)
        return out

    def outgoing(self, key: str) -> list[EdgeSpec]:
        return [e for e in self.edges if key in e.source]


# --------------------------------------------------------------------------------------------
# Parsing the authored GraphDefinition (envelope already validated against the C-DEF envelope)
# --------------------------------------------------------------------------------------------

def parse_definition(doc: dict[str, Any], *, version: int, digest: str) -> GraphSpec:
    spec = doc["spec"]
    nodes: dict[str, NodeSpec] = {}
    for n in spec["nodes"]:
        nodes[n["key"]] = NodeSpec(
            key=n["key"], type=n["type"], phase=n["phase"], config=dict(n.get("config", {})),
            when=n.get("when"), max_visits=n.get("maxVisits"),
        )
    edges = [_parse_edge(e) for e in spec["edges"]]
    return GraphSpec(
        name=doc["metadata"]["name"], version=version, digest=digest,
        workflow_type=spec["workflowType"], entry=spec["entry"], nodes=nodes, edges=edges,
        boundaries=[Boundary(kind=b["kind"], before=b["before"]) for b in spec.get("boundaries", [])],
        params=dict(spec.get("params", {})),
    )


def _parse_edge(e: dict[str, Any]) -> EdgeSpec:
    src = e["from"]
    return EdgeSpec(
        source=list(src) if isinstance(src, list) else [src],
        to=e.get("to"),
        switch=[Branch(when=b["when"], to=b["to"]) for b in e.get("switch", [])],
        default=e.get("default"),
    )


# --------------------------------------------------------------------------------------------
# Composition: apply codebase-binding graph modifiers (D-02 configuration-level change)
# --------------------------------------------------------------------------------------------

def compose(base: GraphSpec, modifiers: Iterable[dict[str, Any]], *, catalog: dict[str, dict]) -> GraphSpec:
    """Apply modifiers in order. Each modifier is one entry of a codebase binding's
    `graphModifiers` list (WS-03 owns the binding document; WS-01 owns this sub-schema)."""
    g = copy.deepcopy(base)
    for m in modifiers:
        op = m["op"]
        origin = f"modifier:{m.get('source', 'inline')}"
        if op in ("insertBefore", "insertAfter"):
            new = m["node"]
            node = NodeSpec(key=new["key"], type=new["type"], phase=new["phase"],
                            config=dict(new.get("config", {})), when=new.get("when"),
                            max_visits=new.get("maxVisits"), origin=origin)
            if node.key in g.nodes:
                raise GraphValidationError([f"modifier inserts duplicate node key {node.key!r}"])
            anchor = m["anchor"]
            if anchor not in g.nodes:
                raise GraphValidationError([f"modifier anchor {anchor!r} not in graph"])
            g.nodes[node.key] = node
            branches = [Branch(**b) for b in m.get("branches", [])]
            if op == "insertBefore":
                for e in g.edges:  # retarget every edge into the anchor
                    e.to = node.key if e.to == anchor else e.to
                    e.default = node.key if e.default == anchor else e.default
                    for b in e.switch:
                        b.to = node.key if b.to == anchor else b.to
                if g.entry == anchor:
                    g.entry = node.key
                g.edges.append(EdgeSpec(source=[node.key], switch=branches,
                                        **({"default": anchor} if branches else {"to": anchor})))
                for b in g.boundaries:  # a boundary before the anchor now precedes the new node
                    if b.before == anchor:
                        b.before = node.key
            else:  # insertAfter: anchor's outgoing edges move to the new node
                for e in g.edges:
                    if e.source == [anchor]:
                        e.source = [node.key]
                g.edges.append(EdgeSpec(source=[anchor], to=node.key))
                if branches:
                    raise GraphValidationError(["insertAfter does not take branches; use insertBefore"])
        elif op == "replace":
            t = _target(g, m)
            if catalog.get(t.type, {}).get("protected"):
                raise GraphValidationError([f"cannot replace protected node {t.key!r}"])
            new_type = m["with"]["type"]
            if catalog.get(new_type, {}).get("role") != catalog.get(t.type, {}).get("role"):
                raise GraphValidationError([f"replace must keep the role of {t.key!r}"])
            t.type = new_type
            t.config = dict(m["with"].get("config", {}))
            t.origin = origin
        elif op == "setConfig":
            t = _target(g, m)
            t.config = _deep_merge(t.config, m["config"])
            t.origin = origin
        elif op == "skipWhen":
            t = _target(g, m)
            if catalog.get(t.type, {}).get("protected"):
                raise GraphValidationError([f"cannot skip protected node {t.key!r}"])
            t.when = f"not ({m['when']})" if t.when is None else f"({t.when}) and not ({m['when']})"
            t.origin = origin
        elif op == "requireGateWhen":
            # Adds a condition under which a policy gate must open (gates may be added, never removed)
            t = _target(g, m)
            if catalog.get(t.type, {}).get("role") != "human_gate":
                raise GraphValidationError(["requireGateWhen targets human_gate nodes only"])
            t.when = m["when"] if t.when is None else f"({t.when}) or ({m['when']})"
            t.origin = origin
        else:
            raise GraphValidationError([f"unknown modifier op {op!r}"])
        g.modifiers_applied.append(f"{op}:{m.get('source', 'inline')}")
    g.digest = base.digest  # the authored digest stays; the composed digest is computed separately
    return g


def _target(g: GraphSpec, m: dict[str, Any]) -> NodeSpec:
    key = m["target"]
    if key not in g.nodes:
        raise GraphValidationError([f"modifier target {key!r} not in graph"])
    return g.nodes[key]


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


# --------------------------------------------------------------------------------------------
# Invariants (run at publish time, at bundle build, and inside graph.compose)
# --------------------------------------------------------------------------------------------

def validate(g: GraphSpec, *, catalog: dict[str, dict]) -> None:
    """`catalog` maps nodeType -> NodeCatalog entry (role, protected, bounded, configSchema...)."""
    p: list[str] = []
    for key, n in g.nodes.items():
        if not NODE_KEY_RE.match(key):
            p.append(f"node key {key!r} does not match the logical activity node_key grammar")
        if n.type not in catalog:
            p.append(f"node {key!r}: type {n.type!r} not in NodeCatalog")
            continue
        if n.phase not in PHASES:
            p.append(f"node {key!r}: unknown phase {n.phase!r}")
        entry = catalog[n.type]
        if n.when is not None:
            if entry.get("protected") and entry["role"] != "human_gate":
                p.append(f"node {key!r}: protected role {entry['role']} cannot have a when guard")
            try:
                Guard(n.when)
            except FglError as e:
                p.append(f"node {key!r}: bad guard: {e}")
    for e in g.edges:
        for s in e.source:
            if s not in g.nodes:
                p.append(f"edge from unknown node {s!r}")
        for t in e.targets:
            if not (t in g.nodes or t.startswith(TERMINAL_PREFIX)):
                p.append(f"edge to unknown target {t!r}")
        if e.switch and e.default is None:
            p.append(f"switch edge from {e.source} needs a default")
        if e.switch and e.to:
            p.append(f"edge from {e.source} has both to and switch")
        for b in e.switch:
            try:
                Guard(b.when)
            except FglError as ex:
                p.append(f"edge from {e.source}: bad guard {b.when!r}: {ex}")
    if g.entry not in g.nodes:
        p.append(f"entry {g.entry!r} not a node")
    for b in g.boundaries:
        if b.kind not in BOUNDARY_KINDS or b.before not in g.nodes:
            p.append(f"bad boundary {b}")
    if p:
        raise GraphValidationError(p)

    # I-1 reachability: every node reachable from entry, every node reaches a terminal
    reach = _reachable(g, [g.entry])
    for k in g.nodes:
        if k not in reach:
            p.append(f"I-1: node {k!r} unreachable from entry")
        if not any(t.startswith(TERMINAL_PREFIX) for t in _reachable(g, [k], include_terminals=True, join_mode="any")):
            p.append(f"I-1: node {k!r} cannot reach a terminal")

    role = {k: catalog[n.type]["role"] for k, n in g.nodes.items()}
    # I-3 publish safety: after any diff-producing node, every path to a publisher passes through
    # qa, a verifier, a diff-stage risk classification, and a publish_approval human gate.
    publishers = [k for k, r in role.items() if r == "publisher"]
    producers = [k for k, r in role.items() if r in DIFF_PRODUCING_ROLES]
    must_pass = {
        "qa": lambda k: role[k] == "qa",
        "verifier": lambda k: role[k] == "verifier" and g.nodes[k].type != "risk_classify",
        "risk(diff)": lambda k: g.nodes[k].type == "risk_classify" and g.nodes[k].config.get("stage") == "diff",
        "publish gate": lambda k: role[k] == "human_gate" and g.nodes[k].config.get("gateKind") == "publish_approval",
    }
    for d in producers:
        for name, pred in must_pass.items():
            blocked = {k for k in g.nodes if pred(k)}
            first = [t for e in g.edges if e.source == [d] for t in e.targets]
            reach_wo = _reachable(g, first, blocked=blocked)
            for pub in publishers:
                if pub in reach_wo:
                    p.append(f"I-3: path {d!r} -> {pub!r} bypasses {name}")
    # I-4 every cycle is bounded: each strongly connected component with a cycle contains a node
    # with maxVisits or a node type the catalog declares `bounded` (e.g. pr_feedback_wait).
    for scc in _sccs(g):
        cyclic = len(scc) > 1 or any(k in g.successors(k) for k in scc)
        if cyclic and not any(g.nodes[k].max_visits or catalog[g.nodes[k].type].get("bounded") for k in scc):
            p.append(f"I-4: unbounded cycle through {sorted(scc)}")
    # I-5 protected nodes from the authored graph must survive composition (checked by caller via
    # validate_composed); gates may be added by modifiers but never removed.
    # I-7 engine equivalence: a switch source has exactly one outgoing edge; join sources have the join as
    # their only outgoing edge; static fan-out targets are join branches; interrupting nodes never sit in a
    # parallel branch.
    for k in g.nodes:
        outs = g.outgoing(k)
        if any(e.switch for e in outs) and len(outs) != 1:
            p.append(f"I-7: {k!r} mixes a switch with other outgoing edges")
        joins = [e for e in outs if len(e.source) > 1]
        if joins and len(outs) != 1:
            p.append(f"I-7: join branch {k!r} must have the join as its only outgoing edge")
        static = [e.to for e in outs if len(e.source) == 1 and e.to and not e.switch]
        if len(static) > 1:
            branches = set(static)
            if not any(set(e.source) == branches for e in g.edges if len(e.source) > 1):
                p.append(f"I-7: fan-out from {k!r} to {sorted(branches)} has no matching join edge")
            for b in branches:
                if b in g.nodes and catalog[g.nodes[b].type].get("interrupts"):
                    p.append(f"I-7: interrupting node {b!r} in a parallel branch")
    if p:
        raise GraphValidationError(p)


def validate_composed(base: GraphSpec, composed: GraphSpec, *, catalog: dict[str, dict]) -> None:
    validate(composed, catalog=catalog)
    p = []
    for k, n in base.nodes.items():
        if catalog[n.type].get("protected"):
            c = composed.nodes.get(k)
            if c is None or c.type != n.type:
                p.append(f"I-5: protected node {k!r} removed or retyped by modifiers")
    if p:
        raise GraphValidationError(p)


def _reachable(g: GraphSpec, start: list[str], *, blocked: set[str] | None = None,
               include_terminals: bool = False, join_mode: str = "all") -> set[str]:
    """Nodes reachable from `start` without passing through `blocked`. With join_mode="all" a join
    (wait-for-all) edge contributes its target only when every source is reachable (its runtime
    semantics; used for the no-bypass invariant I-3). join_mode="any" is the optimistic view used
    for I-1 (can this node reach a terminal at all)."""
    blocked = blocked or set()
    seen: set[str] = set()
    stack = [s for s in start if s not in blocked]
    while True:
        while stack:
            k = stack.pop()
            if k in seen:
                continue
            seen.add(k)
            if k.startswith(TERMINAL_PREFIX):
                continue
            for e in g.edges:
                if k in e.source and len(e.source) == 1:
                    stack.extend(t for t in e.targets if t not in seen and t not in blocked)
        added = False
        for e in g.edges:
            ok = all if join_mode == "all" else any
            if len(e.source) > 1 and ok(src in seen for src in e.source):
                for t in e.targets:
                    if t not in seen and t not in blocked:
                        stack.append(t)
                        added = True
        if not added:
            break
    return seen if include_terminals else {k for k in seen if not k.startswith(TERMINAL_PREFIX)}


def _sccs(g: GraphSpec) -> list[set[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    st: list[str] = []
    out: list[set[str]] = []
    counter = [0]

    def strong(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        st.append(v)
        on.add(v)
        for w in g.successors(v):
            if w.startswith(TERMINAL_PREFIX):
                continue
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = set()
            while True:
                w = st.pop()
                on.discard(w)
                comp.add(w)
                if w == v:
                    break
            out.append(comp)

    for v in g.nodes:
        if v not in index:
            strong(v)
    return out
