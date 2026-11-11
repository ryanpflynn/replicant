"""v0 implementations of the bookkeeping, routing, graph, memory, budget, risk, verification and identity activities
on the bugfix path (catalog: activity_options.CATALOG)."""
from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import re
import tarfile
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from replicant_worker import ids, local_bundle, nodes, registry  # noqa: F401  (nodes registers the NodeCatalog)
from replicant_worker.activities import bundle as bundles
from replicant_worker.activities import s3
from replicant_worker.activities.base import implements
from replicant_worker.activities.config import settings
from replicant_worker.graphs.dsl import RISK_ORDER
from replicant_worker.graphs.graphspec import compose, parse_definition, validate_composed


# ------------------------------------------------------------------------------------------------ bundle / graph
@implements("registry.load_bundle_core")
async def load_bundle_core(a: dict[str, Any]) -> dict[str, Any]:
    b = bundles.load(a["release_bundle_id"])
    pol = b.policies()
    for need in ("router_rules", "repair_limits", "runner_polling", "gate_rules"):
        if need not in pol:
            raise ApplicationError(f"bundle has no {need} policy", type="NotInBundle", non_retryable=True)
    refs = {"memory": {"ref": "policy/memory-snapshot-default@1",
                       "digest": "sha256:" + hashlib.sha256(b"replicant.memory-snapshot/v0-empty").hexdigest()}}
    return {"release_bundle_id": b.release_bundle_id, "source": b.source, "policies": pol, "policy_refs": refs}


@implements("graph.compose")
async def graph_compose(a: dict[str, Any]) -> dict[str, Any]:
    b = bundles.load(a["release_bundle_id"])
    wt = a["workflow_type"]
    labels = a.get("labels") or []
    if local_bundle.REPO_SCAN_LABEL in labels and wt == "docs":
        pin, doc = b.graph_variant(local_bundle.REPO_SCAN_GRAPH)  # MEM repo scan (MEM-D7)
    elif local_bundle.READINESS_LABEL in labels and wt == "qa_only":
        pin, doc = b.graph_variant(local_bundle.READINESS_GRAPH)  # design §8c readiness probe
    else:
        pin, doc = b.graph_for(wt)  # CONFIG: through the bundle's spec.graphs mapping
    base = parse_definition(doc, version=pin["version"], digest=pin["digest"])
    cat = registry.catalog_entries()
    g = compose(base, [], catalog=cat)  # no codebase bindings (graph modifiers) in M1
    validate_composed(base, g, catalog=cat)
    env_name, source = select_environment(b, a.get("repo_profile"), a.get("repo_ref"))
    env_pin, _ = b.environment(env_name)
    return {"spec": g.to_json(), "composed_digest": g.composed_digest(), "environment": env_name,
            "environment_digest": env_pin["digest"], "graph": {**pin, "kind": "graph"}, "environment_source": source}


def select_environment(b: bundles.Bundle, repo_profile: dict[str, Any] | None,
                       repo_ref: dict[str, Any] | None) -> tuple[str, str]:
    """(environment name, source). Without a pinned repo_profile (jobs before §8c, replays): go-service, the M1
    environment. With one: an explicit codebase binding's environment wins, then the profile's inferred environment;
    either only when the bundle has that definition, else go-service (logged)."""
    if not repo_profile:
        return local_bundle.DEFAULT_ENVIRONMENT, "default"
    binding = b.binding_for(repo_ref)
    if binding and binding.get("environment"):
        name = str(binding["environment"]).split("/", 1)[-1].split("@", 1)[0]
        if b.has("environment", name):
            return name, "binding"
    name = str(repo_profile.get("environment") or "")
    if name and b.has("environment", name):
        return name, "repo_profile"
    if not name:  # no supported ecosystem found (onboarding-interfaces.md §11): the old behaviour
        return local_bundle.DEFAULT_ENVIRONMENT, "default"
    activity.logger.warning("repo profile environment not in the bundle; using go-service",
                            extra={"environment": name[:64], "profile_id": repo_profile.get("profile_id")})
    return local_bundle.DEFAULT_ENVIRONMENT, "default"


# ------------------------------------------------------------------------------------------------ router (rules only)
def _facts(req: dict[str, Any]) -> dict[str, Any]:
    src = req.get("source") or {}
    c = req.get("constraints") or {}
    labels = set(c.get("labels") or [])
    for att in req.get("attachments") or []:
        labels |= set((att.get("metadata") or {}).get("labels") or [])
    targets = req.get("targets") or []
    return {"channel": src.get("channel"), "event_type": src.get("event_type"),
            "requested": req.get("workflow_type") or "auto", "priority": req.get("priority", "normal"),
            "labels": labels, "has_pull_request": any(t.get("pull_request") for t in targets),
            "paths": [p for t in targets for p in t.get("paths", [])]}


def _glob(path: str, pats: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, p) or (p.endswith("/**") and path.startswith(p[:-3] + "/")) for p in pats)


def _matches(m: dict[str, Any], f: dict[str, Any]) -> bool:
    if "channel" in m and f["channel"] not in m["channel"]:
        return False
    if "event_type" in m and f["event_type"] not in m["event_type"]:
        return False
    if "requested" in m and f["requested"] not in m["requested"]:
        return False
    if "priority" in m and f["priority"] not in m["priority"]:
        return False
    if "labels_any" in m and not (f["labels"] & set(m["labels_any"])):
        return False
    if "has_pull_request" in m and bool(m["has_pull_request"]) != f["has_pull_request"]:
        return False
    if "paths_any" in m and not any(_glob(p, m["paths_any"]) for p in f["paths"]):
        return False
    if "paths_all" in m and not (f["paths"] and all(_glob(p, m["paths_all"]) for p in f["paths"])):
        return False
    return True


def classify_request(req: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    """Deterministic router (router.md §2): the first matching rule decides. Never sees intent text."""
    f = _facts(req)
    allowed = (rules.get("allowed_types_by_channel") or {}).get(f["channel"])
    strict = rules.get("strictness") or []
    for r in rules["rules"]:
        if not _matches(r.get("match") or {}, f):
            continue
        d = r["decide"]
        if d.get("needs_intent"):
            return {"needs_intent": True, "candidates": list(d.get("candidates", [])), "fallback": rules["fallback"],
                    "rules_fired": [r["id"]], "requested_type": f["requested"]}
        wt = f["requested"] if d.get("workflow_type") == "requested" else d["workflow_type"]
        if d.get("require_priority") and f["priority"] != d["require_priority"]:
            wt = d.get("else", rules["fallback"])
        for esc in d.get("escalate_if") or []:
            if wt == esc.get("from") and esc.get("paths_not_all_match") and \
                    not (f["paths"] and all(_glob(p, esc["paths_not_all_match"]) for p in f["paths"])):
                wt = esc["to"]
        if allowed is not None and wt not in allowed:
            continue
        if f["requested"] not in ("auto", wt) and f["requested"] in strict and wt in strict \
                and strict.index(wt) < strict.index(f["requested"]):
            wt = f["requested"]  # D-19: never downgrade a requested type
        return {"workflow_type": wt, "method": "requested" if d.get("workflow_type") == "requested" else "rule",
                "rules_fired": [r["id"]], "requested_type": f["requested"]}
    return {"needs_intent": True, "candidates": ["bugfix"], "fallback": rules["fallback"], "rules_fired": ["no-rule"],
            "requested_type": f["requested"]}


@implements("router.classify")
async def router_classify(a: dict[str, Any]) -> dict[str, Any]:
    return classify_request(a["request"], a["router_rules"])


@implements("router.parse_intent")
async def router_parse_intent(a: dict[str, Any]) -> dict[str, Any]:
    # v0: deterministic routing only (plan.md §1.2). The workflow treats this failure as "model unavailable" and
    # uses the rules' fallback type (router.md failure modes).
    raise ApplicationError("LLM intent parsing is off in v0", type="IntentParsingDisabled", non_retryable=True)


# memory.* live in activities/memory.py (memory-service client, MEM)


# ------------------------------------------------------------------------------------------------ budget / cost
@implements("budget.allocate_run")
async def budget_allocate_run(a: dict[str, Any]) -> dict[str, Any]:
    """WS-03 §3.1 without usage-ingestor (M1): budget = min(agent limit, job cap); nothing is spent yet."""
    cap = float(a.get("job_cap_usd") or 50)
    agent = float(a.get("plan_budget_usd") or cap)
    budget = round(min(agent, cap), 2)
    if budget < 0.10:
        raise ApplicationError("job budget exhausted", {"scope": "job"}, type="BudgetExhausted", non_retryable=True)
    return {"budget_usd": budget, "extension_usd": 0.0, "job_cap_usd": cap, "job_spent_usd": 0.0}


@implements("budget.tenant_quota_status")
async def budget_tenant_quota_status(a: dict[str, Any]) -> dict[str, Any]:
    return {"available": True, "period": "local", "spent_usd": 0.0, "limit_usd": 0.0, "resets_at": None,
            "reason": "tenant_quota_unset"}


@implements("budget.apply_extension")
async def budget_apply_extension(a: dict[str, Any]) -> dict[str, Any]:
    raise ApplicationError("budget extensions are not part of v0", type="NotImplementedInV0", non_retryable=True)


@implements("cost.record_run")
async def cost_record_run(a: dict[str, Any]) -> dict[str, Any]:
    activity.logger.info("run usage", extra={"run_id": a.get("run_id"), "usage": a.get("usage")})
    return {}  # usage-ingestor is not in M1


@implements("cost.run_resource_usage")
async def cost_run_resource_usage(a: dict[str, Any]) -> dict[str, Any]:
    return {"compute_usd": 0.0}


@implements("cost.batch_spend")
async def cost_batch_spend(a: dict[str, Any]) -> dict[str, Any]:
    return {"spent_usd": 0.0}


@implements("platform.dependency_health")
async def platform_dependency_health(a: dict[str, Any]) -> dict[str, Any]:
    return {"dependency": a["dependency"], "state": "closed"}


# ------------------------------------------------------------------------------------------------ kill switches
@implements("ops.read_kill_switches")
async def ops_read_kill_switches(a: dict[str, Any]) -> dict[str, Any]:
    """The out-of-band mirror `kill-switches/v1/state.json` (replicant.kill-switches/v1) in the ops bucket. Absent
    means nothing engaged. Entries matching (global, the tenant, the job's repos, the bundle) are returned in the
    C-MSG ops.kill_switch shape the workflow applies."""
    uri = settings().kill_switch_mirror
    try:
        doc = s3.get_json(s3.parse(uri)) if uri else None
    except ApplicationError as e:
        if e.type == "ObjectNotFound":
            return {"engaged": []}
        raise
    if not doc:
        return {"engaged": []}
    want = {("global", "*"), ("tenant", a.get("tenant_id")), ("bundle", a.get("release_bundle_id"))}
    want |= {("repo", r) for r in a.get("repos") or []}
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for sw in doc.get("switches") or []:
        key = (sw.get("scope"), sw.get("scope_value") or "*")
        if key not in want or not sw.get("engaged"):
            continue
        g = grouped.setdefault(key, {"schema": "replicant.msg.kill-switch/v1", "scope": key[0], "scope_value": key[1],
                                     "state": "engaged", "switches": [], "issued_at": sw.get("issued_at") or ""})
        g["switches"].append(sw.get("mode"))
        g["issued_at"] = max(g["issued_at"], sw.get("issued_at") or "")
    return {"engaged": list(grouped.values())}


# ------------------------------------------------------------------------------------------------ diff bundle helpers
CI_PATHS = [".github/workflows/**", ".github/actions/**", ".gitea/workflows/**", ".gitlab-ci.yml", "Jenkinsfile"]
AGENT_FILES = ["**/.replicant*", ".replicant*", "**/CLAUDE.md", "CLAUDE.md", "**/AGENTS.md", "AGENTS.md", ".claude/**",
               ".mcp.json"]
SECRET_RES = [re.compile(p) for p in (r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
                                      r"gh[pousr]_[A-Za-z0-9]{36}", r"xox[baprs]-[A-Za-z0-9-]{10,}",
                                      r"sk-ant-[A-Za-z0-9_-]{20,}")]


def read_diff_bundle(uri: str) -> dict[str, Any]:
    """Download and open a diff bundle (tar + zstd: changes.patch, metadata.json). Returns
    {sha256, size, patch: bytes, metadata: dict, paths: [..]}."""
    raw = s3.get_bytes(s3.parse(uri))
    sha = hashlib.sha256(raw).hexdigest()
    data = raw
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        import zstandard
        data = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read()
    elif raw[:2] == b"\x1f\x8b":
        import gzip
        data = gzip.decompress(raw)
    patch, meta = b"", {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            name = m.name.lstrip("./")
            body = tf.extractfile(m).read()  # type: ignore[union-attr]
            if name == "changes.patch":
                patch = body
            elif name == "metadata.json":
                meta = json.loads(body or b"{}")
    paths = list(meta.get("paths") or patch_paths(patch))
    return {"sha256": sha, "size": len(raw), "patch": patch, "metadata": meta, "paths": paths}


def patch_paths(patch: bytes) -> list[str]:
    out: list[str] = []
    for line in patch.decode("utf-8", "replace").splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            if m and m.group(2) not in out:
                out.append(m.group(2))
    return out


def added_lines(patch: bytes) -> list[str]:
    return [ln[1:] for ln in patch.decode("utf-8", "replace").splitlines() if ln.startswith("+") and not ln.startswith("+++")]


# ------------------------------------------------------------------------------------------------ risk
def _risk_rules(a: dict[str, Any]) -> dict[str, Any]:
    """risk_rules of the job's bundle; inputs without `release_bundle_id` (histories from before CONFIG) use the
    defaults, which are the former constants."""
    rb = a.get("release_bundle_id")
    return bundles.load(rb).risk_rules() if rb else dict(bundles.RISK_DEFAULTS)


@implements("risk.classify")
async def risk_classify(a: dict[str, Any]) -> dict[str, Any]:
    """Deterministic risk rules (D-19), v0 subset: CI-executed paths -> critical, agent files -> critical,
    sensitive paths (bundle risk_rules) -> high, more than `large_change_files` (default 20) changed files -> medium,
    otherwise low. The plan stage has no diff and is low. Thresholds come from the job's bundle (CONFIG)."""
    rr = _risk_rules(a)
    if a["stage"] != "diff":
        return {"level": "low", "rules_fired": [f"v0.{a['stage']}.default_low"]}
    uri = (a.get("refs") or {}).get("latest_diff_bundle_uri")
    if not uri:
        return {"level": "low", "rules_fired": ["v0.diff.no_diff"]}
    d = read_diff_bundle(uri)
    rules, level = [], "low"
    if any(_glob(p, CI_PATHS) for p in d["paths"]):
        rules.append("path.ci_executed"); level = "critical"
    if any(_glob(p, AGENT_FILES) for p in d["paths"]):
        rules.append("path.agent_config"); level = "critical"
    if rr["sensitive_paths"] and any(_glob(p, rr["sensitive_paths"]) for p in d["paths"]) \
            and RISK_ORDER[level] < RISK_ORDER["high"]:
        rules.append("path.sensitive"); level = "high"
    n = int(rr["large_change_files"])
    if len(d["paths"]) > n and RISK_ORDER[level] < RISK_ORDER["medium"]:
        rules.append(f"size.files_over_{n}"); level = "medium"
    return {"level": level, "rules_fired": rules or ["v0.diff.default_low"], "paths": d["paths"][:200],
            "diff_sha256": d["sha256"]}


@implements("risk.llm_review")
async def risk_llm_review(a: dict[str, Any]) -> dict[str, Any]:
    return {"level": a["deterministic_level"]}  # v0: no LLM review (router_rules.llm_risk_review = false)


# ------------------------------------------------------------------------------------------------ verifier
@implements("verifier.check")
async def verifier_check(a: dict[str, Any]) -> dict[str, Any]:
    """Mechanical verification of the latest diff bundle: diff scope (forbidden and agent-config paths, request
    paths when given), secret scan of added lines (blocks publish), digest for the Publisher (G-03)."""
    uri = (a.get("refs") or {}).get("latest_diff_bundle_uri")
    if not uri:
        raise ApplicationError("no diff bundle to verify", type="InvalidInput", non_retryable=True)
    d = read_diff_bundle(uri)
    findings: list[dict[str, Any]] = []
    forbidden = _risk_rules(a)["forbidden_paths"]
    for p in d["paths"]:
        if _glob(p, AGENT_FILES) or _glob(p, [".github/workflows/**", ".github/actions/**"]):
            findings.append({"check": "diff_scope", "path": p, "rule": "never_publish_paths"})
        elif forbidden and _glob(p, forbidden):
            findings.append({"check": "diff_scope", "path": p, "rule": "forbidden_path"})
    scope = a.get("paths_scope") or []
    if scope:
        findings += [{"check": "diff_scope", "path": p, "rule": "outside_requested_paths"}
                     for p in d["paths"] if not _glob(p, scope)]
    added = added_lines(d["patch"])
    for i, ln in enumerate(added):
        for rx in SECRET_RES:
            if rx.search(ln):
                findings.append({"check": "secret_scan", "line": i, "rule": rx.pattern[:40]})  # never the match
    if not d["patch"].strip():
        findings.append({"check": "diff_scope", "rule": "empty_patch"})
    # verifier.check is read-only (default activity ID), so the report key is content-addressed by the diff digest
    key = f"tenants/{a['tenant_id']}/jobs/{a['job_id']}/steps/verify.c0.n1/node_report/verification-{d['sha256'][:16]}.json"
    report = {"schema": "replicant.verification-report/v0", "job_id": a["job_id"], "diff_sha256": d["sha256"],
              "paths": d["paths"][:500], "checks": a.get("checks") or ["diff_scope", "secret_scan", "policy"],
              "findings": findings, "passed": not findings}
    try:
        uri = s3.put_json_once(s3.ref("artifacts", key), report)["uri"]
    except ApplicationError as e:  # same diff verified with other checks earlier: keep the first report
        if e.type != "ObjectConflict":
            raise
        uri = s3.ref("artifacts", key).uri
    return {"passed": not findings, "findings": len(findings), "report_uri": uri, "paths": d["paths"][:200],
            "diff_sha256": d["sha256"]}


@implements("checks.run_step")
async def checks_run_step(a: dict[str, Any]) -> dict[str, Any]:
    raise ApplicationError("auxiliary checks are not part of the v0 bugfix path", type="NotImplementedInV0",
                           non_retryable=True)


# ------------------------------------------------------------------------------------------------ identity (local)
def _denied() -> set[str]:
    import os
    return {p for p in (os.environ.get("REPLICANT_LOCAL_DENY_PRINCIPALS") or "").split(",") if p}


@implements("identity.check_decision")
async def identity_check_decision(a: dict[str, Any]) -> dict[str, Any]:
    """M1 has no identity-service (static dev principals, CONTROL-02). Any human principal (usr_) may decide unless
    listed in REPLICANT_LOCAL_DENY_PRINCIPALS; listed principals are named in the eligibility; the requester only when
    requester_may_decide. Recorded in DEVIATIONS."""
    p, el = a["principal_id"], a.get("eligible") or {}
    if p in _denied():
        return {"allow": False, "reason": "not_in_approver_group"}
    if p == a.get("requester") and not el.get("requester_may_decide"):
        return {"allow": False, "reason": "self_approval"}
    if el.get("principals") and p not in el["principals"] and not el.get("okta_groups"):
        return {"allow": False, "reason": "not_in_approver_group"}
    return {"allow": p.startswith("usr_"), "reason": "ok" if p.startswith("usr_") else "not_human"}


@implements("identity.check_action")
async def identity_check_action(a: dict[str, Any]) -> dict[str, Any]:
    p = a["principal_id"]
    return {"allow": p not in _denied(), "reason": "ok" if p not in _denied() else "not_authorized"}


@implements("identity.check_repair_trigger")
async def identity_check_repair_trigger(a: dict[str, Any]) -> dict[str, Any]:
    return {"allowed_user_ids": list(a.get("actors") or [])}


# ------------------------------------------------------------------------------------------------ artifacts
@implements("artifacts.expire_session_state")
async def artifacts_expire_session_state(a: dict[str, Any]) -> dict[str, Any]:
    # The session-state sweep (retention-sweeper) is not in M1; the projector records session_purge_due_at.
    return {"scheduled": True, "after_days": a.get("after_days")}


@implements("artifacts.resolve_superseded_plan")
async def artifacts_resolve_superseded_plan(a: dict[str, Any]) -> dict[str, Any]:
    return {"plan_uri": None, "plan_approved": False}
