"""Validate WS-05 schemas and examples.

Run from the repository root:

    uv run --with jsonschema --with pyyaml --with referencing python spec/05-qa/tools/validate_examples.py

Checks:
  1. Every WS-05 schema is a valid Draft 2020-12 schema.
  2. The three EnvironmentDefinition examples validate against the frozen C-DEF envelope, against an
     published WS-03 kind schema (spec/03-agents-and-tools/schemas), and
     against the WS-05 QA schema directly.
  3. Semantic lints that the registry publish linter (configuration.md section 5) also runs:
     resource sums, data budgets versus workspace size, profile existence, quarantine expiry format.
  4. QA report / run result / feedback examples validate; run_result also validates against the frozen
     C-RUNNER run-result envelope; run IDs match the C-IDS derivation; artifact types are registered in
     C-ART (or pending in CCR-05-01); keys sit under the canonical run or step prefix.
  5. Negative cases are rejected.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import re
import sys

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = pathlib.Path(__file__).resolve().parents[3]
QA = ROOT / "spec" / "05-qa"
CONTRACTS = ROOT / "spec" / "00-contracts"
WS03 = ROOT / "spec" / "03-agents-and-tools"

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"

# Registered in C-ART section 4 (artifact-layout.md) plus types pending in CCR-05-01.
CART_TYPES = {
    "run_inputs", "task_packet", "briefing", "memory_injection", "steering_message", "run_manifest",
    "run_result", "diff_bundle", "plan", "handoff", "review_report", "qa_report", "junit_xml", "build_log",
    "health_log", "screenshot", "playwright_trace", "video", "a11y_report", "sbom", "image_metadata",
    "transcript", "cli_events", "sandbox_log", "cli_session_state", "workspace_archive", "approval_packet",
    "approval_record", "pr_event_payload", "intake_attachment", "job_artifact_index", "takeover_package",
    "memory_candidates", "memory_snapshot_export", "memory_export", "golden_task_snapshot", "eval_output",
    "registry_blob",
}
PENDING_CCR_TYPES = {"qa_generated_flow"}
CART_TYPES |= {"secret_scan_report"}  # CCR-09-05, accepted 2026-10-24

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"FAIL  {msg}")


def ok(msg: str) -> None:
    print(f"ok    {msg}")


def load_json(p: pathlib.Path) -> dict:
    return json.loads(p.read_text())


def derived_run_id(tenant: str, key: str) -> str:
    d = hashlib.sha256(f"{tenant}\n{key}".encode()).digest()
    n = int.from_bytes(d[:17], "big") >> 6
    return "run_" + "".join(ALPHABET[(n >> (5 * (25 - i))) & 31] for i in range(26))


def cpu(q: str) -> float:
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


def mem(q: str) -> int:
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}
    m = re.fullmatch(r"([0-9]+)(Ki|Mi|Gi|Ti)?", q)
    assert m, q
    return int(m.group(1)) * units.get(m.group(2) or "", 1)


# ---------------------------------------------------------------- schemas
schema_files = {
    "qa-activities-io": QA / "schemas" / "qa-activities-io.schema.json",
    "approval-packet-qa": QA / "schemas" / "approval-packet-qa.schema.json",
    "qa-config": QA / "schemas" / "qa-config.schema.json",
    "qa-report": QA / "schemas" / "qa-report.schema.json",
    "role-result-qa": QA / "schemas" / "role-result-qa.schema.json",
    "qa-feedback": QA / "schemas" / "qa-feedback.schema.json",
}
schemas = {k: load_json(p) for k, p in schema_files.items()}
for k, s in schemas.items():
    try:
        Draft202012Validator.check_schema(s)
        ok(f"schema {k} is valid Draft 2020-12")
    except Exception as e:  # noqa: BLE001
        fail(f"schema {k}: {e}")

envelope = load_json(CONTRACTS / "schemas" / "definition-envelope.schema.json")
run_result_contract = load_json(CONTRACTS / "schemas" / "run-result.schema.json")
# WS-03's published kind schema (spec.qa only requires the discriminator; WS-05's schema validates the rest).
ws03_env = load_json(WS03 / "schemas" / "environment-definition.schema.json")
ws03_common = load_json(WS03 / "schemas" / "common.schema.json")

registry = Registry().with_resources(
    [(s["$id"], Resource.from_contents(s)) for s in [*schemas.values(), ws03_env, ws03_common, envelope, run_result_contract]]
)


def validator(schema: dict) -> Draft202012Validator:
    return Draft202012Validator(schema, registry=registry, format_checker=Draft202012Validator.FORMAT_CHECKER)


def check(name: str, schema: dict, doc: object) -> bool:
    errs = sorted(validator(schema).iter_errors(doc), key=lambda e: list(e.path))
    if errs:
        for e in errs[:10]:
            fail(f"{name}: {'/'.join(map(str, e.path))}: {e.message[:300]}")
        return False
    ok(f"{name} validates")
    return True


# ---------------------------------------------------------------- environment definitions
AGENTD_PORTS = set(range(7800, 7804))  # WS-03 replicant-agentd loopback listeners
# replicant-agentd, watchdog, uploader requests/limits from WS-02 manifests/sandbox-pod.example.yaml
FIXED_SIDECARS = {"cpu": 0.1 + 0.02 + 0.2, "mem_req": (96 + 32 + 256) * 2**20, "cpu_lim": 1 + 0.2 + 1, "mem": (256 + 64 + 512) * 2**20}
NODE_ALLOC = {"cpu": 93.0, "mem": 360 * 2**30}  # m8id.24xlarge allocatable estimate (WS-02 runtime-evaluation.md section 9)


def lint_env(name: str, doc: dict) -> None:
    spec = doc["spec"]
    qa = spec["qa"]
    prof_name = spec["profileByRole"].get("qa", "default")
    if prof_name not in spec["resourceProfiles"]:
        fail(f"{name}: L2 profileByRole.qa {prof_name!r} not in resourceProfiles")
        return
    ok(f"{name}: L2 profileByRole.qa -> {prof_name}")
    prof = spec["resourceProfiles"][prof_name]
    qa_services = [s for s in spec.get("services", []) if "qa" in s.get("forRoles", ["qa"])]
    sidecars = [*qa_services, *qa.get("stubs", [])]
    ib = qa.get("imageBuild", {})
    extra = [{"resources": ib["builderResources"]}] if ib.get("enabled") else []
    req_cpu = cpu(prof["requests"]["cpu"]) + sum(cpu(s["resources"]["requests"]["cpu"]) for s in sidecars + extra) + FIXED_SIDECARS["cpu"]
    req_mem = mem(prof["requests"]["memory"]) + sum(mem(s["resources"]["requests"]["memory"]) for s in sidecars + extra) + FIXED_SIDECARS["mem_req"]
    lim_mem = mem(prof["limits"]["memory"]) + sum(mem(s["resources"]["limits"]["memory"]) for s in sidecars + extra) + FIXED_SIDECARS["mem"]
    lim_cpu = cpu(prof["limits"]["cpu"]) + sum(cpu(s["resources"]["limits"]["cpu"]) for s in sidecars + extra) + FIXED_SIDECARS["cpu_lim"]
    # L3: pod fits at least twice per node (WS-03 rule 0.45 x allocatable) at limits.
    if lim_cpu > 0.45 * NODE_ALLOC["cpu"] or lim_mem > 0.45 * NODE_ALLOC["mem"]:
        fail(f"{name}: L3 pod limits {lim_cpu:g} CPU / {lim_mem / 2**30:.1f} GiB exceed 0.45 x node allocatable")
    else:
        ok(f"{name}: L3 pod totals req {req_cpu:g} CPU / {req_mem / 2**30:.2f} GiB, limits {lim_cpu:g} CPU / {lim_mem / 2**30:.2f} GiB (incl. agentd/watchdog/uploader); Kata VM memory {(lim_mem + 256 * 2**20) / 2**30:.2f} GiB")
    ws = mem(prof["workspace"]["sizeLimit"])
    budgets = mem(ib.get("cacheBudget", "0Gi")) if ib.get("enabled") else 0
    if budgets > ws // 2:
        fail(f"{name}: L4 image-build cache budget {budgets / 2**30:.1f} GiB exceeds 50% of workspace")
    else:
        ok(f"{name}: L4 workspace budgets {budgets / 2**30:.1f} GiB of {ws / 2**30:.0f} GiB")
    ports: list[int] = []
    for s in qa_services:
        ports += [p["port"] for p in s["ports"]]
    ports += [s["port"] for s in qa.get("stubs", [])]
    ports += [p + 1 for s in qa.get("stubs", []) if s["server"] == "gripmock" for p in [s["port"]]]  # gripmock admin port
    ports += qa.get("app", {}).get("start", {}).get("ports", [])
    if len(ports) != len(set(ports)) or AGENTD_PORTS & set(ports):
        fail(f"{name}: L5 port collision or agentd port use {sorted(ports)}")
    else:
        ok(f"{name}: L5 ports unique and clear of agentd 7800-7803 {sorted(ports)}")
    if ib.get("enabled"):
        if spec["isolationHint"].get("qa_build") != "kata-clh":
            fail(f"{name}: L6 imageBuild needs isolationHint.qa_build kata-clh")
        else:
            ok(f"{name}: L6 imageBuild with isolationHint.qa_build kata-clh")
    if "qa" in spec["bootstrap"].get("forRoles", []):
        fail(f"{name}: L15 bootstrap.forRoles must not include qa (the harness runs bootstrap for qa)")
    else:
        ok(f"{name}: L15 bootstrap not run by the entrypoint for qa")
    svc_names = {s["name"] for s in qa_services}
    bad = [o["name"] for o in qa.get("serviceOptions", []) if o["name"] not in svc_names]
    unwrapped = [o["name"] for o in qa.get("serviceOptions", []) if o.get("wrapLogs", True) and not next(s for s in qa_services if s["name"] == o["name"]).get("command")] if not bad else []
    if bad:
        fail(f"{name}: L16 serviceOptions name(s) {bad} not among qa services {sorted(svc_names)}")
    else:
        ok(f"{name}: L16 serviceOptions match qa services {sorted(svc_names)}")
    if unwrapped:
        print(f"warn  {name}: L16 wrapLogs on services without command {unwrapped} (logs come from sandbox_log only)")
    if qa["timeouts"]["totalSeconds"] > 14400:
        fail(f"{name}: L7 totalSeconds above the replicant.env-qa/v1 maximum (14,400)")
    # Runner A-8 is exactly sizeLimit <= limits.ephemeralStorage (XD-FIX-R1-036); no margin of our own. The
    # controller renders the agent container's limit as ephemeralStorage + H + F + O (printed for information).
    import math
    gi = 2**30
    for pn, pr in spec["resourceProfiles"].items():
        sl, es = mem(pr["workspace"]["sizeLimit"]), mem(pr["limits"]["ephemeralStorage"])
        if sl > es:
            fail(f"{name}: A-8 profile {pn} sizeLimit {pr['workspace']['sizeLimit']} > limits.ephemeralStorage {pr['limits']['ephemeralStorage']}")
        else:
            h = math.ceil((0.05 * sl + gi) / gi); f_ = 8
            o_ = math.ceil(spec["outputs"]["maxOutputBytes"] / gi) + 1 if "outputs" in spec and "maxOutputBytes" in spec["outputs"] else None
            rendered = f"{es // gi} + {h} + {f_} + {o_} = {es // gi + h + f_ + o_}Gi" if o_ is not None else f"{es // gi} + {h} + {f_} + O"
            ok(f"{name}: A-8 profile {pn} sizeLimit {pr['workspace']['sizeLimit']} <= ephemeralStorage {pr['limits']['ephemeralStorage']} (rendered agent limit {rendered})")
    # WS-03 lint (XD-FIX-R1-074, replaces WS-05 L4 data rule): every profile used by a role in a service's forRoles
    # has workspace.sizeLimit >= sum(data.sizeEstimate) + 10Gi.
    for role, pn in {**{"qa": spec["profileByRole"].get("qa", "default")}, **spec["profileByRole"]}.items():
        est = sum(mem(sv["data"]["sizeEstimate"]) for sv in spec["services"] if "data" in sv and role in sv.get("forRoles", ["qa"]))
        sl = mem(spec["resourceProfiles"][pn]["workspace"]["sizeLimit"])
        if sl < est + 10 * gi:
            fail(f"{name}: L4 profile {pn} (role {role}) sizeLimit {sl // gi}Gi < data {est / gi:.0f}Gi + 10Gi")
        else:
            ok(f"{name}: L4 profile {pn} (role {role}) sizeLimit {sl // gi}Gi >= service data {est / gi:.0f}Gi + 10Gi")
    # WS-09 VAP (XD-FIX-R1-212): every image is ECR (replicant|mirror)/<1-4 segments>@sha256; wrapped services must be replicant/svc-* and every QA-declared container has an ephemeral-storage limit.
    img_re = re.compile(r"^[0-9]{12}[.]dkr[.]ecr[.]us-east-1[.]amazonaws[.]com/(replicant|mirror)(/[a-z0-9][a-z0-9._-]*){1,4}@sha256:[0-9a-f]{64}$")  # XD-FIX-R1-212
    images = [spec["image"]["resolved"]["image"]] + [s["image"] for s in qa_services] + [s["image"] for s in qa.get("stubs", [])]
    if ib.get("enabled"):
        images.append(ib["builderImage"])
    bad_img = [i for i in images if not img_re.match(i)]
    wrap_of = {o["name"]: o.get("wrapLogs", True) for o in qa.get("serviceOptions", [])}
    no_eph = [s["name"] for s in qa_services if "ephemeralStorage" not in s["resources"]["limits"]]
    if bad_img or no_eph:
        fail(f"{name}: VAP images {bad_img} / services without ephemeral-storage limit {no_eph}")
    else:
        wrapped_bad = [s["name"] for s in qa_services if wrap_of.get(s["name"], True) and "/replicant/svc-" not in s["image"]]
        if wrapped_bad:
            fail(f"{name}: services with wrapLogs need a replicant/svc-* image carrying replicant-svc-wrap: {wrapped_bad}")
        ok(f"{name}: VAP {len(images)} images under ECR replicant/ or mirror/ by digest; all QA containers have ephemeral-storage limits")
    # XD-02-15: QA services keep data under /workspace/.services/<name>, never a separate dataVolume.
    dv = [s["name"] for s in qa_services if "dataVolume" in s]
    if dv:
        fail(f"{name}: L4 QA services with dataVolume {dv} (data belongs under /workspace/.services/<name>)")
    else:
        ok(f"{name}: L4 QA service data on the enforced workspace")
    known_profiles = {"coding-default", "review-readonly", "qa-with-mirror", "utility-minimal", "takeover", "eval-coding", "eval-scoring", "eval-snapshot"}  # XD-FIX-R1-213
    unknown = sorted(set(spec["networkProfile"].values()) - known_profiles)
    if unknown:
        fail(f"{name}: network profiles not defined by WS-02/WS-09: {unknown}")
    else:
        ok(f"{name}: network profiles {sorted(set(spec['networkProfile'].values()))} are defined")


env_examples = sorted((QA / "examples").glob("env-*.yaml"))
for p in env_examples:
    doc = yaml.safe_load(p.read_text())
    check(f"{p.name} (C-DEF envelope)", envelope, doc)
    check(f"{p.name} (WS-03 environment-definition kind schema)", ws03_env, doc)
    check(f"{p.name} spec.qa (WS-05 replicant.env-qa/v1)", schemas["qa-config"], doc["spec"]["qa"])
    lint_env(p.name, doc)

# R1V-031: WS-03 accepts the old overflow names only as deprecated aliases until WS-05 migrates; no example may use them.
_aliases = {"generic_ephemeral_volume_above", "always_generic_ephemeral_volume"}
_bad = [(p.name, pn) for p in env_examples for pn, pr in yaml.safe_load(p.read_text())["spec"]["resourceProfiles"].items()
        if pr["workspace"]["overflow"]["policy"] in _aliases or "storageClassName" in pr["workspace"]["overflow"]]
if _bad:
    fail(f"R1V-031: deprecated overflow alias or storageClassName in {_bad}")
else:
    ok("R1V-031: every profile uses a canonical overflow policy (never|auto|always|local_only), no storageClassName")

# ---------------------------------------------------------------- reports and results
JOB = "job_01m5p7x7r0bt69j0h0fbxepw90"
expected_runs = {
    "run_chxtsxqzcpds1fc1c4dvw9e4em": f"ik1:{JOB}:qa.c0.n1.run",
    "run_ata247zxh0zx4y24bdv2vns95e": f"ik1:{JOB}:qa.c0.n2.run",
    "run_gj3h3ep3phjfs9e23v768zrca5": f"ik1:{JOB}:implementer.c0.n1.run",
}
for rid, key in expected_runs.items():
    if derived_run_id("payments", key) != rid:
        fail(f"run id derivation mismatch for {key}")
    else:
        ok(f"C-IDS derivation {key} -> {rid}")


def walk_refs(obj: object, out: list[dict]) -> None:
    if isinstance(obj, dict):
        if {"type", "bucket_class", "key"} <= obj.keys():
            out.append(obj)
        for v in obj.values():
            walk_refs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_refs(v, out)


report = load_json(QA / "examples" / "qa-report-failed-classified.json")
check("qa-report-failed-classified.json", schemas["qa-report"], report)
refs: list[dict] = []
walk_refs(report, refs)
run_prefix = f"tenants/{report['tenant_id']}/jobs/{report['job_id']}/runs/{report['run_id']}/"
for r in refs:
    if r["type"] not in CART_TYPES | PENDING_CCR_TYPES:
        fail(f"artifact type {r['type']} not registered in C-ART")
    if not r["key"].startswith(run_prefix):
        fail(f"artifact key outside run prefix: {r['key']}")
    seg = r["key"][len(run_prefix):].split("/", 1)[0]
    if seg != r["type"]:
        fail(f"artifact key segment {seg!r} does not equal type {r['type']!r} (C-ART key layout)")
ok(f"{len(refs)} artifact refs in qa report use registered types and canonical keys")
if not report["classification_facts"]["feedback_key"].startswith(f"tenants/payments/jobs/{JOB}/steps/qa.c0.n1/qa_report/"):
    fail("feedback_key not under the step prefix")
if "routing" in report:
    fail("classified report still carries routing (R1-118: the node decides)")
io = schemas["qa-activities-io"]
check("qa-classify-output-skip-added.json (ClassifyOutput)", {"$ref": io["$id"] + "#/$defs/ClassifyOutput"}, load_json(QA / "examples" / "qa-classify-output-skip-added.json"))
check("qa-test-inventory-skip-added.json (qa_report.test_inventory)", {"$ref": schemas["qa-report"]["$id"] + "#/properties/test_inventory"}, load_json(QA / "examples" / "qa-test-inventory-skip-added.json"))
inv = load_json(QA / "examples" / "qa-test-inventory-skip-added.json")
want = (inv["tests_removed_total"] + inv["tests_skipped_added_total"] + len(inv["deleted_test_files"]) + len(inv["skip_markers_added"])) > 0
if inv["deleted_or_skipped"] != want:
    fail("test_inventory.deleted_or_skipped inconsistent with its lists")
else:
    ok("R2-006 inventory: deleted_or_skipped consistent")

rr = load_json(QA / "examples" / "run-result-qa-passed.json")
check("run-result-qa-passed.json (C-RUNNER run-result envelope)", run_result_contract, rr)
check("run-result-qa-passed.json role_result (replicant.role-result.qa/v1)", schemas["role-result-qa"], rr["role_result"])
outcome_map = {"pass": "completed", "pass_with_warnings": "completed", "fail": "failed", "inconclusive": "failed", "not_applicable": "no_changes"}  # R1-116
if outcome_map[rr["role_result"]["verdict"]] != rr["outcome"]:
    fail("run_result.outcome does not match the verdict mapping")
else:
    ok("run_result.outcome matches verdict mapping")

hr = load_json(QA / "examples" / "qa-harness-result-passed.json")
hr_schema = dict(schemas["role-result-qa"]); hr_schema = {**hr_schema["$defs"]["harness_result"], "$defs": hr_schema["$defs"]}
hr_schema["properties"] = {**hr_schema["properties"], "role_result": schemas["role-result-qa"]}
check("qa-harness-result-passed.json (/replicant/out/qa/result.json, XD-FIX-R1-073)", hr_schema, hr)
bad_hr = copy.deepcopy(hr); bad_hr["exit_class"] = "environment_services"
if list(validator(hr_schema).iter_errors(bad_hr)):
    ok("negative rejected: exit_class environment_services with a non-inconclusive verdict")
else:
    fail("negative accepted: environment_services with verdict pass")
pk = load_json(QA / "examples" / "approval-packet-qa-inconclusive.json")
check("approval-packet-qa-inconclusive.json (replicant.approval-packet.qa/v1)", schemas["approval-packet-qa"], pk)
bad_pk = copy.deepcopy(pk); bad_pk["verdict"] = "fail"; bad_pk["reason_code"] = "regression"
if list(validator(schemas["approval-packet-qa"]).iter_errors(bad_pk)):
    ok("negative rejected: rerun_qa offered on a non-inconclusive verdict (R2-027)")
else:
    fail("negative accepted: rerun_qa on verdict fail")
fb = load_json(QA / "examples" / "qa-feedback-fix-loop.json")
check("qa-feedback-fix-loop.json", schemas["qa-feedback"], fb)

# ---------------------------------------------------------------- negative cases
go = yaml.safe_load((QA / "examples" / "env-go-grpc-ledger-api.yaml").read_text())
negatives = []
n1 = copy.deepcopy(go["spec"]["qa"]); del n1["app"]; negatives.append(("server without app", n1))
n2 = copy.deepcopy(go["spec"]["qa"]); n2["env"]["AWS_CONTAINER_CREDENTIALS_FULL_URI"] = "http://x"; negatives.append(("protected env name (WS-03 env_map)", n2))
n9 = copy.deepcopy(go["spec"]["qa"]); del n9["schema"]; negatives.append(("missing replicant.env-qa/v1 discriminator", n9))
n10 = copy.deepcopy(go["spec"]["qa"]); del n10["imageBuild"]["builderImage"]; negatives.append(("image build without builder image", n10))
n11 = copy.deepcopy(go["spec"]["qa"]); n11["app"]["start"]["mode"] = "image"; negatives.append(("image-mode app (withdrawn in v1)", n11))
n3 = copy.deepcopy(go["spec"]["qa"]); n3["stubs"][0]["image"] = "docker.io/library/postgres:17"; negatives.append(("stub image without digest", n3))
n4 = copy.deepcopy(go["spec"]["qa"]); n4["app"]["health"]["readiness"] = []; negatives.append(("empty readiness", n4))
n5 = copy.deepcopy(go["spec"]["qa"]); n5["stubs"][0]["contract"]["version"] = "pinned"; negatives.append(("pinned stub without digest", n5))
n6 = copy.deepcopy(go["spec"]["qa"]); n6["variants"]["nightly"] = {"skip": ["agent"]}; negatives.append(("unknown variant key", n6))
n7 = copy.deepcopy(go["spec"]["qa"]); n7["seed"][0]["workdir"] = "../../etc"; negatives.append(("path traversal", n7))
n8 = copy.deepcopy(go["spec"]["qa"]); del n8["app"]["health"]["readiness"]; negatives.append(("server health without readiness", n8))
for label, doc in negatives:
    if list(validator(schemas["qa-config"]).iter_errors(doc)):
        ok(f"negative rejected: {label}")
    else:
        fail(f"negative accepted: {label}")
bad_rr = copy.deepcopy(rr["role_result"]); bad_rr["top_failures"] = bad_rr["top_failures"] * 21
if list(validator(schemas["role-result-qa"]).iter_errors(bad_rr)):
    ok("negative rejected: role_result with more than 20 top_failures")
else:
    fail("negative accepted: role_result with 21 top_failures")
bad_rep = copy.deepcopy(report); del bad_rep["classification_facts"]
if list(validator(schemas["qa-report"]).iter_errors(bad_rep)):
    ok("negative rejected: classified report without classification_facts")
else:
    fail("negative accepted: classified report without classification_facts")
n12 = copy.deepcopy(go["spec"]["qa"]); del n12["stubs"][0]["resources"]["limits"]["ephemeralStorage"]
if list(validator(schemas["qa-config"]).iter_errors(n12)):
    ok("negative rejected: stub without ephemeral-storage limit (R1-114)")
else:
    fail("negative accepted: stub without ephemeral-storage limit")
bad_rr2 = copy.deepcopy(rr["role_result"]); del bad_rr2["test_inventory_delta"]
if list(validator(schemas["role-result-qa"]).iter_errors(bad_rr2)):
    ok("negative rejected: role_result without test_inventory_delta (R2-006)")
else:
    fail("negative accepted: role_result without test_inventory_delta")

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("all WS-05 checks passed")
