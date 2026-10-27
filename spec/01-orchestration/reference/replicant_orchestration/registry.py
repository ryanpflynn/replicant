"""Node registry: node types are Python classes registered with @node_type, discovered by the
worker at import time, and published by worker CI as the build's NodeCatalog (C-DEF section 2).

A node type is workflow-side, deterministic orchestration code. It does all I/O through
`NodeContext` (context.py), which turns every unit of real work into an activity with an explicit
logical activity ID. Node code never imports LangGraph: the engine adapter owns that coupling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, ClassVar

if TYPE_CHECKING:
    from .context import NodeContext

NODE_CATALOG_SCHEMA_VERSION = 1


@dataclass
class NodeResult:
    """What a node returns. `outcome` is routable by edge guards (`nodes.<key>.outcome`);
    `exports` are small typed values for guards (declared in outputSchema); `refs` are S3 URIs
    recorded in JobState.refs under the given names."""

    outcome: str
    exports: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)


class NodeFailed(Exception):
    """Unrecoverable node failure (after the node's own retries). Ends the job as failed unless
    the node type maps it to a routable outcome itself."""

    def __init__(self, reason: str, message: str = "", *, retryable: bool = False,
                 details: dict | None = None) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message
        self.retryable = retryable
        self.details = details or {}  # first ApplicationError detail when it is a dict (typed errors)


class NodeType:
    node_type: ClassVar[str]
    role: ClassVar[str]  # C-DEF NodeCatalog role enum
    protected: ClassVar[bool] = False  # cannot be removed, retyped, or skipped by modifiers
    bounded: ClassVar[bool] = False  # loops through this node are bounded by the node itself
    interrupts: ClassVar[bool] = False  # may call ctx.interrupt (gates, waits); never in a parallel branch
    # XD-06-01: check_only (can only fail the job or raise risk), workspace_write (produces a diff),
    # external_side_effect (anything visible outside Replicant, or that steers later nodes)
    effect_class: ClassVar[str] = "external_side_effect"
    effects: ClassVar[tuple[str, ...]] = ()  # logical-ID effect names this node may use
    config_schema: ClassVar[dict[str, Any]] = {"type": "object"}
    input_schema: ClassVar[dict[str, Any]] = {"type": "object"}
    output_schema: ClassVar[dict[str, Any]] = {"type": "object"}
    description: ClassVar[str] = ""

    async def run(self, ctx: "NodeContext", config: dict[str, Any]) -> NodeResult:  # pragma: no cover
        raise NotImplementedError


_REGISTRY: dict[str, type[NodeType]] = {}


def node_type(cls: type[NodeType]) -> type[NodeType]:
    if cls.node_type in _REGISTRY and _REGISTRY[cls.node_type] is not cls:
        raise ValueError(f"duplicate node type {cls.node_type}")
    _REGISTRY[cls.node_type] = cls
    return cls


def get(node_type_name: str) -> NodeType:
    return _REGISTRY[node_type_name]()


def registered() -> dict[str, type[NodeType]]:
    return dict(_REGISTRY)


def catalog_entries() -> dict[str, dict[str, Any]]:
    """The validation view of the catalog (used by graphspec.validate)."""
    return {
        name: {"role": c.role, "protected": c.protected, "bounded": c.bounded,
               "interrupts": c.interrupts, "effectClass": c.effect_class, "configSchema": c.config_schema}
        for name, c in sorted(_REGISTRY.items())
    }


def build_node_catalog(*, deployment_name: str, build_id: str, image_digest: str,
                       carry_schemas: list[str], fgl_version: int, engine: str,
                       patch_of: str | None = None) -> dict[str, Any]:
    """Produce the NodeCatalog registry document for this build (published by worker CI with
    POST /v1/registry/publish). Conforms to the C-DEF envelope's NodeCatalog constraints; the
    extra node fields (`protected`, `bounded`, `interrupts`, `effects`) and spec fields
    (`carrySchemas`, `fglVersion`, `engine`, `catalogSchemaVersion`) are WS-01's kind schema."""
    nodes = []
    for name, c in sorted(_REGISTRY.items()):
        nodes.append({
            "nodeType": name, "role": c.role, "effectClass": c.effect_class, "protected": c.protected, "bounded": c.bounded,
            "interrupts": c.interrupts, "effects": list(c.effects), "description": c.description,
            "inputSchema": c.input_schema, "outputSchema": c.output_schema, "configSchema": c.config_schema,
        })
    return {
        "apiVersion": "replicant.example.com/v1",
        "kind": "NodeCatalog",
        "metadata": {"name": deployment_name, "scope": {"level": "global"}, "owners": ["replicant-platform"],
                     "labels": {"build-id": build_id}},
        "spec": {
            "workerDeployment": {"deploymentName": deployment_name, "buildId": build_id, "imageDigest": image_digest,
                                 **({"patchOf": patch_of} if patch_of else {})},
            "catalogSchemaVersion": NODE_CATALOG_SCHEMA_VERSION,
            "engine": engine,
            "fglVersion": fgl_version,
            "carrySchemas": carry_schemas,
            "migrationProtocol": "replicant.migration-handoff/v1",
            "nodes": nodes,
        },
    }


NodeFn = Callable[["NodeContext", dict[str, Any]], Awaitable[NodeResult]]
