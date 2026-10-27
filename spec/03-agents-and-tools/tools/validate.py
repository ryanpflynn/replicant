"""Validate WS-03 schemas and examples.

Run from the repository root:
    uv run --with jsonschema --with referencing --with pyyaml --with openapi-spec-validator \
        python spec/03-agents-and-tools/tools/validate.py

Checks:
  1. Every WS-03 schema (and the C-DEF / C-RUNNER contract schemas it references) is a valid
     Draft 2020-12 schema.
  2. Every example validates against its schema; run results validate against BOTH the frozen
     contract envelope (run-result.schema.json) and the WS-03 role-result schema.
  3. Measured resource profiles reproduce exactly from their recorded stats with formula rp-1.
  4. Kind-specific semantic lints that JSON Schema cannot express (selectors, outputs, D-10 paths).
  5. The gateway interface OpenAPI document is valid OpenAPI 3.1.
  6. Negative cases are rejected (so the schemas are not vacuous).
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = pathlib.Path(__file__).resolve().parents[3]
WS = ROOT / "spec" / "03-agents-and-tools"
CONTRACTS = ROOT / "spec" / "00-contracts" / "schemas"
sys.path.insert(0, str(WS / "tools"))
import rp1  # noqa: E402

failures: list[str] = []


def load(p: pathlib.Path):
    text = p.read_text()
    return yaml.safe_load(text) if p.suffix in (".yaml", ".yml") else json.loads(text)


def build_registry() -> tuple[Registry, dict[str, dict]]:
    schemas: dict[str, dict] = {}
    resources = []
    ws01 = ROOT / "spec" / "01-orchestration" / "schemas" / "graph-definition.v1.json"  # owner of graphModifier (R1-003)
    for p in sorted(list(CONTRACTS.glob("*.schema.json")) + list((WS / "schemas").glob("*.schema.json")) + [ws01]):
        s = json.loads(p.read_text())
        Draft202012Validator.check_schema(s)
        schemas[p.name] = s
        res = Resource.from_contents(s)
        resources.append((s["$id"], res))
    return Registry().with_resources(resources), schemas


def validator(registry: Registry, schema: dict) -> Draft202012Validator:
    return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())


def check(name: str, v: Draft202012Validator, doc) -> None:
    errs = sorted(v.iter_errors(doc), key=lambda e: list(e.path))
    for e in errs:
        failures.append(f"{name}: {'/'.join(map(str, e.path))}: {e.message[:300]}")
    print(f"  {'FAIL' if errs else 'ok  '} {name}")


def expect_invalid(name: str, v: Draft202012Validator, doc) -> None:
    ok = v.is_valid(doc)
    if ok:
        failures.append(f"negative case accepted: {name}")
    print(f"  {'FAIL' if ok else 'ok  '} (rejects) {name}")


D10_PATHS = {"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENTS.override.md", ".claude/**", ".codex/**", ".mcp.json", ".cursor/**"}


def lint_agent(name: str, doc: dict) -> None:
    spec = doc["spec"]
    if spec["tools"]["edit"] and set(spec["roles"]) <= {"reviewer", "planner"}:
        failures.append(f"{name}: read-only roles must have tools.edit=false")
    if "reviewer" in spec["roles"] and spec["outputContract"].get("workspaceIntegrity", "off") != "enforce":
        failures.append(f"{name}: reviewer must set workspaceIntegrity enforce (one rule, R1-090)")
    if spec["cli"] == "codex" and spec["tools"]["shell"]["mode"] == "allowlist":
        failures.append(f"{name}: codex cannot express per-command allowlists")
    if spec["cli"] == "codex" and spec["codex"]["mode"] == "app_server" and doc["metadata"]["scope"].get("tenant_id") != "replicant-eval":
        failures.append(f"{name}: codex app_server is experimental; only replicant-eval may use it (ADR-003)")
    if spec["cli"] == "claude-agent-sdk" and doc["metadata"]["scope"].get("tenant_id") != "replicant-eval":
        failures.append(f"{name}: claude-agent-sdk only in tenant replicant-eval in v1 (ADR-002, XD-03-11)")
    if spec["tools"].get("editPaths") and not spec["tools"]["edit"]:
        failures.append(f"{name}: editPaths without edit")
    print(f"  ok   lint {name}")


def lint_environment(name: str, doc: dict) -> None:
    spec = doc["spec"]
    for pname, prof in spec["resourceProfiles"].items():
        pol = prof.get("workspace", {}).get("overflow", {}).get("policy")
        if pol in ("generic_ephemeral_volume_above", "always_generic_ephemeral_volume"):
            print(f"  WARN {name}: profile {pname} uses deprecated overflow alias {pol} (R1V-031)")
        d = prof["derivation"]
        if d["method"] == "measured":
            bl = d["baseline"]
            st = bl["stats"]
            if bl["formulaVersion"] == "rp-2":
                prof_calc, policy = rp1.derive_rp2(rp1.from_derivation_stats(st), bl["runtimeClass"], rp1.host_from_stats(st))
            else:
                policy = prof.get("memoryRequestPolicy", "median_peak")
                prof_calc = rp1.derive(rp1.from_derivation_stats(st), policy)
            got = rp1.render(prof_calc)
            have = {"requests": prof["requests"], "limits": prof["limits"], "sizeLimit": prof["workspace"]["sizeLimit"]}
            if got != have:
                failures.append(f"{name}: profile {pname} does not match {bl['formulaVersion']}: expected {got}, have {have}")
            if prof.get("memoryRequestPolicy", "median_peak") != policy:
                failures.append(f"{name}: profile {pname} memoryRequestPolicy {prof.get('memoryRequestPolicy')} != {policy}")
        ws = rp1.GI * int(prof["workspace"]["sizeLimit"].rstrip("Gi")) if prof["workspace"]["sizeLimit"].endswith("Gi") else None
        eph = prof["limits"]["ephemeralStorage"]
        if ws and eph.endswith("Gi") and int(eph[:-2]) * rp1.GI < ws:
            failures.append(f"{name}: profile {pname} limits.ephemeralStorage < workspace.sizeLimit")
    def gi(q):
        return int(q[:-2]) * (1024 if q.endswith("Ti") else 1) if q.endswith(("Gi", "Ti")) else int(q[:-2]) / 1024
    for svc in spec.get("services", []):
        roles = svc.get("forRoles", ["qa"])
        est = gi(svc["data"]["sizeEstimate"]) if "data" in svc else 0
        for role in roles:
            pname = spec["profileByRole"].get(role, "default")
            ws = gi(spec["resourceProfiles"][pname]["workspace"]["sizeLimit"])
            if ws < est + 10:
                failures.append(f"{name}: profile {pname} sizeLimit {ws}Gi < service data {est}Gi + 10Gi (R1-074 lint)")
    for role, pname in spec["profileByRole"].items():
        if pname not in spec["resourceProfiles"]:
            failures.append(f"{name}: profileByRole.{role} -> unknown profile {pname}")
    print(f"  ok   lint {name} (rp-1/rp-2 recomputed)")


def main() -> int:
    registry, schemas = build_registry()
    print("meta-validation: ok for", len(schemas), "schemas")
    V = {k: validator(registry, s) for k, s in schemas.items()}

    print("examples:")
    ex = WS / "examples"
    pairs = [
        ("agent-implementer-claude.yaml", "agent-definition.schema.json"),
        ("agent-reviewer-codex.yaml", "agent-definition.schema.json"),
        ("agent-qa-runner.yaml", "agent-definition.schema.json"),
        ("agent-contract-probe.yaml", "agent-definition.schema.json"),
        ("environment-nextjs-app.yaml", "environment-definition.schema.json"),
        ("environment-go-service.yaml", "environment-definition.schema.json"),
        ("policy-budgets.yaml", "policy-budgets.schema.json"),
        ("policy-codebase-bindings.yaml", "policy-codebase_bindings.schema.json"),
        ("run-inputs-implementer.json", "run-inputs.schema.json"),
        ("run-plan-implementer.json", "run-plan.schema.json"),
        ("run-outputs-implementer.json", "run-outputs.schema.json"),
        ("gateway-usage-event.json", "gateway-usage-event.schema.json"),
        ("rightsizing-proposal.json", "rightsizing-proposal.schema.json"),
    ]
    docs = {}
    for f, s in pairs:
        p = ex / f
        if not p.exists():
            failures.append(f"missing example {f}")
            continue
        docs[f] = load(p)
        check(f, V[s], docs[f])
        if s.startswith("agent") or s.startswith("environment") or s.startswith("policy"):
            check(f + " (C-DEF envelope)", V["definition-envelope.schema.json"], docs[f])
    for f, role_schema in [("run-result-implementer.json", "role-result-implementer.schema.json"), ("run-result-reviewer.json", "role-result-reviewer.schema.json")]:
        d = load(ex / f)
        check(f + " (C-RUNNER run-result envelope)", V["run-result.schema.json"], d)
        check(f + " (role_result)", V[role_schema], d["role_result"])

    print("semantic lints:")
    for f in ("agent-implementer-claude.yaml", "agent-reviewer-codex.yaml", "agent-qa-runner.yaml", "agent-contract-probe.yaml"):
        lint_agent(f, docs[f])
    for f in ("environment-nextjs-app.yaml", "environment-go-service.yaml"):
        if f in docs:
            lint_environment(f, docs[f])
    # run inputs embed the verified agent spec: it must equal the agent example
    if docs["run-plan-implementer.json"]["agent"] != docs["agent-implementer-claude.yaml"]["spec"]:
        failures.append("run-plan agent differs from the implementer agent spec")
    import hashlib
    plan_bytes = (ex / "run-plan-implementer.json").read_bytes()
    rp_obj = [o for o in docs["run-inputs-implementer.json"]["objects"] if o["place"] == "run_plan"][0]
    if hashlib.sha256(plan_bytes).hexdigest() != rp_obj["sha256"]:
        failures.append("manifest run_plan sha256 does not match run-plan-implementer.json")

    print("negative cases:")
    a = copy.deepcopy(docs["agent-implementer-claude.yaml"])
    a["spec"]["cliImage"]["image"] = "123456789012.dkr.ecr.us-east-1.amazonaws.com/replicant/cli-claude-code:2.1.310"
    expect_invalid("agent with tag instead of digest", V["agent-definition.schema.json"], a)
    a = copy.deepcopy(docs["agent-implementer-claude.yaml"])
    a["spec"]["model"]["primary"] = "anthropic/claude-sonnet-latest"
    expect_invalid("agent with -latest model alias", V["agent-definition.schema.json"], a)
    a = copy.deepcopy(docs["agent-implementer-claude.yaml"])
    a["spec"]["env"]["ANTHROPIC_BASE_URL"] = "https://evil.example.com"
    expect_invalid("agent setting protected env ANTHROPIC_BASE_URL", V["agent-definition.schema.json"], a)
    a = copy.deepcopy(docs["agent-implementer-claude.yaml"])
    a["spec"]["tools"]["web"]["search"] = True
    expect_invalid("agent enabling web search", V["agent-definition.schema.json"], a)
    a = copy.deepcopy(docs["agent-reviewer-codex.yaml"])
    a["spec"]["steering"]["live"] = True
    expect_invalid("codex exec with steering.live=true", V["agent-definition.schema.json"], a)
    a = copy.deepcopy(docs["agent-reviewer-codex.yaml"])
    a["spec"]["outputContract"]["outputs"]["diff_bundle"] = "optional"
    expect_invalid("reviewer allowed to produce a diff", V["agent-definition.schema.json"], a)
    a = copy.deepcopy(docs["agent-reviewer-codex.yaml"])
    a["spec"]["gateway"]["protocol"] = "anthropic_messages"
    expect_invalid("codex with anthropic protocol", V["agent-definition.schema.json"], a)
    e = copy.deepcopy(docs["environment-nextjs-app.yaml"])
    del e["spec"]["resourceProfiles"]["default"]["derivation"]["baseline"]
    expect_invalid("measured profile without baseline", V["environment-definition.schema.json"], e)
    e = copy.deepcopy(docs["environment-nextjs-app.yaml"])
    e["spec"]["resourceProfiles"]["default"]["workspace"]["overflow"] = {"policy": "never", "thresholdSize": "200Gi"}
    expect_invalid("overflow threshold with a non-auto policy", V["environment-definition.schema.json"], e)
    r = load(ex / "run-inputs-implementer.json")
    r["objects"][0]["key"] = "tenants/payments/jobs/job_01m5p7x7r0bt69j0h0fbxepw90/runs/run_chxtsxqzcpds1fc1c4dvw9e4em/artifacts/x"
    expect_invalid("inputs object outside inputs/ prefix", V["run-inputs.schema.json"], r)
    r = load(ex / "run-inputs-implementer.json")
    r["objects"].append(dict(r["objects"][0]))
    expect_invalid("manifest with two run_plan objects", V["run-inputs.schema.json"], r)
    r = load(ex / "run-inputs-implementer.json")
    mem = next(o for o in r["objects"] if o["place"] == "system_append_memory")
    r["objects"].append(dict(mem, name="memory_injection_2"))
    expect_invalid("manifest with two system_append_memory objects (XD-W2-04-01)", V["run-inputs.schema.json"], r)
    r = load(ex / "run-inputs-implementer.json")
    mem = next(o for o in r["objects"] if o["place"] == "system_append_memory")
    mem["type"], mem["content_type"] = "agent_prompt", "text/markdown"
    expect_invalid("memory block staged as a prompt file (XD-W2-04-01)", V["run-inputs.schema.json"], r)
    u = load(ex / "gateway-usage-event.json")
    u["evaluation_mode"] = "eval"
    expect_invalid("eval usage event without budget_account (XD-FIX-R2-061)", V["gateway-usage-event.schema.json"], u)
    e = copy.deepcopy(docs["environment-nextjs-app.yaml"])
    e["spec"]["resourceProfiles"]["default"]["workspace"]["overflow"] = {"policy": "generic_ephemeral_volume_above"}
    expect_invalid("deprecated overflow alias without thresholdSize (R1V-031)", V["environment-definition.schema.json"], e)
    e = copy.deepcopy(docs["environment-nextjs-app.yaml"])
    e["spec"]["resourceProfiles"]["default"]["workspace"]["overflow"] = {"policy": "auto", "storageClassName": "replicant-workspace-gp3"}
    expect_invalid("storageClassName with a current overflow policy (XD-FIX-R1-279)", V["environment-definition.schema.json"], e)
    a = copy.deepcopy(docs["agent-qa-runner.yaml"])
    del a["spec"]["qaHarness"]
    expect_invalid("qa agent without qaHarness", V["agent-definition.schema.json"], a)
    b = copy.deepcopy(docs["policy-codebase-bindings.yaml"])
    b["spec"]["bindings"][2]["graphModifiers"][0] = {"op": "insert_before", "nodeType": "x", "anchor": "implement"}
    expect_invalid("binding with non-WS-01 graphModifier", V["policy-codebase_bindings.schema.json"], b)

    print("openapi:")
    try:
        from openapi_spec_validator import validate as oas_validate
        oas = yaml.safe_load((WS / "gateway-interface.openapi.yaml").read_text())
        oas_validate(oas)
        print("  ok   gateway-interface.openapi.yaml (OpenAPI", oas["openapi"] + ")")
    except FileNotFoundError:
        failures.append("missing gateway-interface.openapi.yaml")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"gateway-interface.openapi.yaml: {exc}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
