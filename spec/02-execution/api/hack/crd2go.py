#!/usr/bin/env python3
"""Generate api/v1alpha1/agentrun_types.go from the frozen C-CRD YAML.

Usage: uv run --with pyyaml python api/hack/crd2go.py ../00-contracts/agentrun-crd.yaml > api/v1alpha1/agentrun_types.go

Every validation in the CRD schema (type, required, enum, pattern, min/max length, minimum/maximum,
format, maxItems, list types, defaults, and every x-kubernetes-validations rule) is emitted as a
kubebuilder marker, so `controller-gen crd` on the output reproduces the frozen schema. CI runs this
generator and fails if the committed file differs (WS-02 R1-034).
"""
import json
import sys

import yaml

# Named Go types for enum fields (path -> type name). Everything else is a plain Go type.
NAMED = {
    "spec.role": "RunRole",
    "spec.workloadClass": "WorkloadClass",
    "spec.workflowType": "WorkflowType",
    "spec.runtimeClassName": "RuntimeClassName",
    "spec.priorityClassName": "PriorityClassName",
    "spec.workspace.backing": "WorkspaceBacking",
    "spec.workspace.enforcement": "WorkspaceEnforcement",
    "spec.cancelReason": "CancelReason",
    "spec.repo.provider": "RepoProvider",
    "spec.additionalRepos[].provider": "RepoProvider",
    "status.phase": "RunPhase",
}
# Struct names for object paths.
STRUCTS = {
    "spec": "AgentRunSpec",
    "status": "AgentRunStatus",
    "spec.agentDefinition": "DefinitionRef",
    "spec.environmentDefinition": "DefinitionRef",
    "spec.repo": "RepoRef",
    "spec.additionalRepos[]": "RepoRef",
    "spec.inputs": "RunInputs",
    "spec.artifacts": "RunArtifacts",
    "spec.resources": "RunResources",
    "spec.resources.requests": "ResourceQuantities",
    "spec.resources.limits": "ResourceQuantities",
    "spec.workspace": "RunWorkspace",
    "spec.timeouts": "RunTimeouts",
    "spec.limits": "RunLimits",
    "spec.steering": "RunSteering",
    "spec.traceContext": "TraceContext",
    "spec.callback": "RunCallback",
}
SPECIAL = {"status.conditions": "[]metav1.Condition"}

emitted = {}
named_enums = {}


def go_name(json_name):
    special = {"runId": "RunID", "jobId": "JobID", "tenantId": "TenantID", "workflowId": "WorkflowID",
               "repoId": "RepoID", "baseSha": "BaseSHA", "logicalActivityId": "LogicalActivityID",
               "releaseBundleId": "ReleaseBundleID", "manifestUri": "ManifestURI",
               "manifestSha256": "ManifestSHA256", "maxCostMicrosUsd": "MaxCostMicrosUSD",
               "artifactManifestUri": "ArtifactManifestURI", "resultUri": "ResultURI", "cpu": "CPU"}
    return special.get(json_name, json_name[0].upper() + json_name[1:])


def gostr(s):
    return json.dumps(s)


def markers(schema, path, field_level=True):
    out = []
    for key, marker in (("maxLength", "MaxLength"), ("minLength", "MinLength"), ("minimum", "Minimum"),
                        ("maximum", "Maximum"), ("maxItems", "MaxItems"), ("format", "Format")):
        if key in schema:
            out.append(f"// +kubebuilder:validation:{marker}={schema[key]}")
    if "pattern" in schema:
        out.append(f"// +kubebuilder:validation:Pattern=`{schema['pattern']}`")
    if "enum" in schema and path not in NAMED:
        out.append("// +kubebuilder:validation:Enum=" + ";".join(str(e) for e in schema["enum"]))
    if "default" in schema:
        out.append(f"// +kubebuilder:default={json.dumps(schema['default'])}")
    if schema.get("x-kubernetes-list-type"):
        out.append(f"// +listType={schema['x-kubernetes-list-type']}")
    for k in schema.get("x-kubernetes-list-map-keys", []):
        out.append(f"// +listMapKey={k}")
    for v in schema.get("x-kubernetes-validations", []):
        out.append(f"// +kubebuilder:validation:XValidation:rule={gostr(v['rule'])},message={gostr(v['message'])}")
    return out


def gotype(schema, path):
    if path in SPECIAL:
        return SPECIAL[path]
    t = schema.get("type")
    if path in NAMED:
        name = NAMED[path]
        named_enums.setdefault(name, schema.get("enum", []))
        return name
    if t == "object":
        name = STRUCTS[path]
        if name not in emitted:
            emitted[name] = None
            emitted[name] = struct(name, schema, path)
        return name
    if t == "array":
        return "[]" + gotype(schema["items"], path + "[]")
    if t == "string":
        if schema.get("format") == "date-time":
            return "metav1.Time"
        return "string"
    if t == "integer":
        return "int64" if schema.get("format") == "int64" else "int32"
    if t == "boolean":
        return "bool"
    if t == "number":
        return "float64"
    raise SystemExit(f"unhandled type at {path}: {schema}")


def struct(name, schema, path):
    req = set(schema.get("required", []))
    lines = []
    for v in schema.get("x-kubernetes-validations", []):
        lines.append(f"// +kubebuilder:validation:XValidation:rule={gostr(v['rule'])},message={gostr(v['message'])}")
    lines.append(f"type {name} struct {{")
    for fname, fs in schema.get("properties", {}).items():
        fpath = f"{path}.{fname}"
        typ = gotype(fs, fpath)
        if fs.get("description"):
            for dl in fs["description"].strip().splitlines():
                lines.append(f"\t// {dl}".rstrip())
        optional = fname not in req
        if optional:
            lines.append("\t// +optional")
        for m in markers(fs, fpath):
            lines.append("\t" + m)
        if fpath in NAMED:
            # enum marker lives on the named type; field-level markers above still apply
            pass
        ptr = optional and (typ.startswith(("RunLimits", "RunSteering", "TraceContext", "metav1.Time"))
                            or (fs.get("type") in ("integer",) and fpath != "status.observedGeneration"))
        gtyp = ("*" + typ) if ptr else typ
        tag = fname + (",omitempty" if optional else "")
        lines.append(f"\t{go_name(fname)} {gtyp} `json:\"{tag}\"`")
    lines.append("}")
    return "\n".join(lines)


def main():
    crd = list(yaml.safe_load_all(open(sys.argv[1])))[0]
    ver = crd["metadata"]["annotations"].get("replicant.example.com/contract-version", "?")
    root = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    gotype(root["properties"]["spec"], "spec")
    gotype(root["properties"]["status"], "status")
    out = [
        "// Code generated by api/hack/crd2go.py from spec/00-contracts/agentrun-crd.yaml. DO NOT EDIT.",
        f"// Source contract: C-CRD {ver}. Regenerate on every C-CRD version; CI diffs the output.",
        "",
        "// +kubebuilder:object:generate=true",
        "// +groupName=replicant.example.com",
        "package v1alpha1",
        "",
        'import metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"',
        "",
    ]
    for name, enum in sorted(named_enums.items()):
        out.append(f"// {name} is a closed enum from the CRD schema.")
        out.append("// +kubebuilder:validation:Enum=" + ";".join(enum))
        out.append(f"type {name} string")
        out.append("")
    for name in emitted:
        out.append(emitted[name])
        out.append("")
    v = crd["spec"]["versions"][0]
    out.append("// AgentRun is the unit of isolated work (D-05).")
    out.append("//")
    out.append("// +kubebuilder:object:root=true")
    out.append("// +kubebuilder:subresource:status")
    names = crd["spec"]["names"]
    out.append(f"// +kubebuilder:resource:scope={crd['spec']['scope']},shortName={';'.join(names.get('shortNames', []))},categories={';'.join(names.get('categories', []))}")
    for sf in v.get("selectableFields", []):
        out.append(f"// +kubebuilder:selectablefield:JSONPath=`{sf['jsonPath']}`")
    for c in v.get("additionalPrinterColumns", []):
        pr = f",priority={c['priority']}" if "priority" in c else ""
        out.append(f"// +kubebuilder:printcolumn:name={gostr(c['name'])},type={c['type']},JSONPath=`{c['jsonPath']}`{pr}")
    for r in root.get("x-kubernetes-validations", []):
        out.append(f"// +kubebuilder:validation:XValidation:rule={gostr(r['rule'])},message={gostr(r['message'])}")
    out += [
        "type AgentRun struct {",
        "\tmetav1.TypeMeta   `json:\",inline\"`",
        "\tmetav1.ObjectMeta `json:\"metadata,omitempty\"`",
        "",
        "\tSpec AgentRunSpec `json:\"spec\"`",
        "\t// +optional",
        "\tStatus AgentRunStatus `json:\"status,omitempty\"`",
        "}",
        "",
        "// AgentRunList is a list of AgentRuns.",
        "//",
        "// +kubebuilder:object:root=true",
        "type AgentRunList struct {",
        "\tmetav1.TypeMeta `json:\",inline\"`",
        "\tmetav1.ListMeta `json:\"metadata,omitempty\"`",
        "\tItems           []AgentRun `json:\"items\"`",
        "}",
    ]
    print("\n".join(out))


if __name__ == "__main__":
    main()
