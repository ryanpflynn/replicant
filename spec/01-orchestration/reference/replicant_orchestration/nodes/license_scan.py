"""Worked example (extensibility.md section 6): node type `license_policy_scan`.

Path (b) "new node code": this module is the entire code change. It ships in a new replicant-worker
build (new Worker Deployment Version + NodeCatalog). Nothing in control-plane-api, the UI, the
Runner, or the controller changes: they treat node types as opaque catalog entries (CC-2).

Path (a) "configuration only": once a build containing this node is deployed, inserting it into a
graph for some repositories is a codebase-binding graph modifier (new tenant-derived bundle; no deploy).

What it does: before implementation, run a utility sandbox (QA role, workload class `utility`,
agent definition `license-scanner`, whose environment profile runs `go-licenses report ./...` and
`osv-scanner --format json`) to produce an SBOM (registered artifact type `sbom`, C-ART), then
evaluate it against the node's allow/deny configuration in a pure activity. Violations end the job
before any code is written, or (failOn: warn) are passed to the implementer as constraints.
"""

from __future__ import annotations

from typing import Any

from ..activity_options import CATALOG, IO_INTERNAL, ActivityDef
from ..context import NodeContext
from ..registry import NodeResult, NodeType, node_type

# The new activity ships in the same build (code). It is read-only: SBOM + config -> verdict.
CATALOG["policy.evaluate_licenses"] = ActivityDef("policy.evaluate_licenses", IO_INTERNAL, False)


@node_type
class LicensePolicyScan(NodeType):
    node_type = "license_policy_scan"
    role = "auxiliary"
    effect_class = "check_only"  # can only fail the job or add constraints: tightening (WS-06 ADR-001)
    effects = ("plan", "budget", "resnap", "stage", "stage_agent", "run", "record_cost", "cancel")
    config_schema = {
        "type": "object", "additionalProperties": False, "required": ["deny"],
        "properties": {
            "agent": {"type": "string", "default": "license-scanner", "x-replicant-ref": "agent"},
            "allow": {"type": "array", "items": {"type": "string", "maxLength": 64}, "description": "SPDX IDs"},
            "deny": {"type": "array", "minItems": 1, "items": {"type": "string", "maxLength": 64}},
            "failOn": {"enum": ["deny", "warn"], "default": "deny"},
            "ecosystems": {"type": "array", "items": {"enum": ["go", "npm", "maven", "pypi", "cargo"]}, "default": ["go"]},
        },
    }
    output_schema = {"type": "object", "properties": {
        "violations": {"type": "integer"}, "denied": {"type": "array", "items": {"type": "string"}}}}
    description = "Pre-implementation license and dependency policy scan (SBOM via utility sandbox)."

    async def run(self, ctx: NodeContext, config: dict[str, Any]) -> NodeResult:
        out = await ctx.run_agent(role="qa", agent=config.get("agent", "license-scanner"),
                                  environment=ctx.host.binding_environment(), workload_class="utility",
                                  resource_profile="license-scan", network_profile="coding-default",
                                  inputs={"ecosystems": config.get("ecosystems", ["go"])})
        sbom = out.outputs.get("sbom") or out.outputs.get("qa_report")
        ctx.set_ref("license_sbom_uri", sbom)
        v = await ctx.act("policy.evaluate_licenses", {"job_id": ctx.s.job_id, "sbom_uri": sbom,
                                                       "allow": config.get("allow", []), "deny": config["deny"]})
        denied = sorted(set(v.get("denied", [])))[:50]
        if denied and config.get("failOn", "deny") == "deny":
            return NodeResult(outcome="violations", exports={"violations": len(denied), "denied": denied})
        return NodeResult(outcome="pass", exports={"violations": len(denied), "denied": denied})
