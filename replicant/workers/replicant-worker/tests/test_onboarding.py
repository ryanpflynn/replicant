"""Repo onboarding on the worker (design §8c, onboarding-interfaces.md): model tiers and escalation, environment
selection in graph.compose, the run-plan fields from the pinned repo profile, observations and the readiness status.
Activities run against the contract fakes of test_activities (no network)."""
from __future__ import annotations

import copy
import json
from typing import Any

import fakes
import httpx
import pytest
from test_activities import JOB, RB, REPO, REQ, T, env, run  # noqa: F401  (env: autouse fixture)

from replicant_worker import local_bundle, registry, tiers
from replicant_worker.activities import bundle, core, inventory, staging
from replicant_worker.graphs.graphspec import parse_definition, validate

OPUS, SONNET, HAIKU = tiers.OPUS, tiers.SONNET, tiers.HAIKU
PROFILE = {"profile_id": "rp_01", "sha": "a" * 40, "environment": "python-service",
           "commands": {"install": "uv sync --frozen", "build": None, "test": "uv run pytest -q",
                        "lint": "uv run ruff check ."},
           "repo_config": "use", "config_base_sha": "a" * 40,
           "mcp_auto_allow": [{"name": "schema", "command": "npx", "args": ["-y", "@acme/schema-mcp@1.2.0"]}],
           "readiness": "ready"}


# ------------------------------------------------------------------------------------------------ tiers
def test_standard_tier_by_role() -> None:
    pol: dict[str, Any] = {}
    imp = tiers.resolve(pol, role="implementer", agent_name="implementer-claude", workflow_type="bugfix", facts={})
    assert imp["tier"] == "standard" and imp["escalated"] is None
    assert imp["params"] == {"model": SONNET, "fallbacks": [OPUS], "thinkingTokens": 8000, "reasoningEffort": "medium",
                             "small": HAIKU}
    rev = tiers.resolve(pol, role="reviewer", agent_name="reviewer-codex", workflow_type="bugfix", facts={})
    assert rev["params"]["model"] == OPUS != imp["params"]["model"]  # the reviewer is a different model
    assert tiers.resolve(pol, role="planner", agent_name="planner-claude", workflow_type="bugfix", facts={})["params"][
        "reasoningEffort"] == "high"
    rep = tiers.resolve(pol, role="repair", agent_name="implementer-claude", workflow_type="bugfix", facts={})
    assert rep["role"] == "repair" and rep["params"]["model"] == SONNET  # "repair": "implementer"
    scan = tiers.resolve(pol, role="planner", agent_name="scanner-claude", workflow_type="docs", facts={})
    assert scan["role"] == "scanner" and scan["params"]["model"] == SONNET
    assert tiers.resolve(pol, role="qa", agent_name="qa-runner", workflow_type="bugfix", facts={}) is None


def test_thorough_tier_resolves_references() -> None:
    cat = tiers.DEFAULT_CATALOG
    assert tiers.params_for(cat, "thorough", "implementer")["model"] == OPUS
    assert tiers.params_for(cat, "thorough", "implementer")["thinkingTokens"] == 32000
    assert tiers.params_for(cat, "thorough", "repair") == tiers.params_for(cat, "thorough", "implementer")
    assert tiers.params_for(cat, "thorough", "planner") == tiers.params_for(cat, "standard", "planner")  # standard.planner
    assert tiers.params_for(cat, "thorough", "reviewer")["model"] == OPUS
    assert tiers.params_for(cat, "nope", "implementer") is None
    with pytest.raises(ValueError):
        tiers.params_for({"tiers": {"a": {"implementer": "b.implementer"}, "b": {"implementer": "a.implementer"}}},
                         "a", "implementer")


def test_project_selection_and_unknown_tier() -> None:
    pol = {"model_tier_selection": {"default_tier": "standard", "workflows": {"bugfix": "thorough"}}}
    assert tiers.resolve(pol, role="implementer", agent_name="i", workflow_type="bugfix", facts={})["tier"] == "thorough"
    assert tiers.resolve(pol, role="implementer", agent_name="i", workflow_type="feature", facts={})["tier"] == "standard"
    pol = {"model_tier_selection": {"default_tier": "platinum", "workflows": {}}}
    assert tiers.resolve(pol, role="implementer", agent_name="i", workflow_type="bugfix", facts={})["tier"] == "standard"


@pytest.mark.parametrize("facts,why", [
    ({"repair_cycle": 2}, "repair_cycle"), ({"repair_cycle": 1}, None), ({"risk": "high"}, "risk"),
    ({"risk": "critical"}, "risk"), ({"risk": "medium"}, None), ({"failed_attempt": True}, "failed_attempt"), ({}, None)])
def test_escalation(facts: dict[str, Any], why: str | None) -> None:
    for role in ("implementer", "repair"):
        t = tiers.resolve({}, role=role, agent_name="implementer-claude", workflow_type="bugfix", facts=facts)
        assert (t["tier"], t["escalated"]) == (("thorough", why) if why else ("standard", None))
        assert t["params"]["model"] == (OPUS if why else SONNET)
    # only the implementer and repair escalate
    rev = tiers.resolve({}, role="reviewer", agent_name="reviewer-codex", workflow_type="bugfix", facts=facts)
    assert rev["tier"] == "standard" and rev["escalated"] is None


def test_escalation_follows_the_selection_policy() -> None:
    pol = {"model_tier_selection": {"default_tier": "standard", "workflows": {},
                                    "escalation": {"repair_cycle_at_least": 3, "risk_at_least": "critical",
                                                   "after_failed_attempt": False}}}
    for facts in ({"repair_cycle": 2}, {"risk": "high"}, {"failed_attempt": True}):
        assert tiers.resolve(pol, role="implementer", agent_name="i", workflow_type="bugfix", facts=facts)["tier"] == "standard"
    assert tiers.resolve(pol, role="implementer", agent_name="i", workflow_type="bugfix",
                         facts={"repair_cycle": 3})["escalated"] == "repair_cycle"


def test_apply_tier_to_spec() -> None:
    spec = copy.deepcopy(local_bundle.documents()["agent/implementer-claude@1"]["spec"])
    out = tiers.apply(spec, tiers.params_for(tiers.DEFAULT_CATALOG, "thorough", "implementer"))
    assert out["model"]["primary"] == OPUS and out["model"]["fallbacks"] == [SONNET]
    assert out["model"]["parameters"]["thinkingTokens"] == 32000 and out["model"]["parameters"]["reasoningEffort"] == "high"
    assert out["model"]["parameters"]["maxOutputTokens"] == spec["model"]["parameters"]["maxOutputTokens"]
    assert spec["model"]["primary"] == SONNET  # the definition is not modified


def test_workflow_tier_facts() -> None:
    from replicant_worker.workflows.context import tier_facts
    from replicant_worker.workflows.state import JobState
    s = JobState(job_id=JOB, tenant_id=T, release_bundle_id=RB, worker_build_id="", requester="usr_x", priority="normal",
                 title="t", targets=[], request=None, request_summary={})
    assert tier_facts(s, 1) == {"repair_cycle": 0, "risk": "low", "failed_attempt": False}
    s.cycle, s.risk_floor, s.qa_fix_used = 2, "high", 1
    assert tier_facts(s, 1) == {"repair_cycle": 2, "risk": "high", "failed_attempt": True}
    s.qa_fix_used, s.risk = 0, {"level": "critical"}
    assert tier_facts(s, 2) == {"repair_cycle": 2, "risk": "critical", "failed_attempt": True}


# ------------------------------------------------------------------------------------------------ run plan
def _plan(role: str = "implementer", agent: str = "implementer-claude", **extra: Any) -> tuple[dict, dict]:
    from replicant_worker import ids
    run_id = ids.run_id_for(T, ids.idempotency_key(JOB, f"implement.c0.n{len(extra)}.run"))
    a = {"job_id": JOB, "tenant_id": T, "run_id": run_id, "role": role, "node_key": "implement", "cycle": 0,
         "release_bundle_id": RB, "parent_job_id": None, "ordinal": 1, "workflow_type": "bugfix", "agent_ref": agent,
         "environment_ref": extra.pop("environment_ref", "go-service"), "workload_class": "coding",
         "resource_profile": "default", "timeouts": None, "network_profile": "coding-default",
         "steering_requested": True, "deterministic": False, "received_at": REQ["received_at"], "base_sha": "8" * 40,
         "job_cap_usd": 40, **extra}
    return a, run(staging.agents_build_run_plan, a, activity_id="implement.c0.n1.plan")


def test_plan_without_tiers_or_profile_is_unchanged(env) -> None:  # noqa: F811
    _, plan = _plan()
    doc = env["s3"].json("local-replicant-local-artifacts", plan["run_plan"]["key"])
    assert "tier" not in plan and doc["agent"] == local_bundle.documents()["agent/implementer-claude@1"]["spec"]
    assert doc["repo_instructions"] == "quote_as_context"
    assert not {"repo_config", "config_base_sha", "mcp_auto_allow"} & set(doc) and "commands" not in doc["environment"]


def test_plan_applies_the_tier(env) -> None:  # noqa: F811
    _, plan = _plan(tier_facts={"repair_cycle": 2, "risk": "low", "failed_attempt": False})
    doc = env["s3"].json("local-replicant-local-artifacts", plan["run_plan"]["key"])
    assert plan["tier"] == {"tier": "thorough", "role": "implementer", "escalated": "repair_cycle", "model": OPUS,
                            "reasoning_effort": "high", "thinking_tokens": 32000}
    assert doc["agent"]["model"]["primary"] == OPUS and doc["agent"]["model"]["parameters"]["reasoningEffort"] == "high"
    assert plan["models"][0] == OPUS and SONNET in plan["models"] and HAIKU in plan["models"]  # gateway allowlist
    errs = [e for e in fakes.errors(fakes.ws03_validator("run-plan.schema.json"), doc) if "image" not in e]
    assert not errs, errs
    _, rev = _plan(role="reviewer", agent="reviewer-codex", tier_facts={})
    assert rev["tier"]["model"] == OPUS and rev["tier"]["tier"] == "standard"


def test_plan_from_repo_profile(env) -> None:  # noqa: F811
    _, plan = _plan(environment_ref="python-service", repo_profile=PROFILE, repo=dict(REPO))
    doc = env["s3"].json("local-replicant-local-artifacts", plan["run_plan"]["key"])
    assert doc["environment"]["commands"] == PROFILE["commands"]
    assert doc["repo_config"] == "use" and doc["config_base_sha"] == "a" * 40
    assert doc["mcp_auto_allow"] == PROFILE["mcp_auto_allow"]
    assert doc["environment"]["codebase_type"] == "python_service"
    steps = doc["environment"]["bootstrap_steps"]
    assert [x["name"] for x in steps] == ["seed-cache", "repo-install"] and steps[1]["run"] == "uv sync --frozen"
    assert plan["environment_ref"]["name"] == "python-service"
    # QA runs the commands itself: only the cache seed (the entrypoint runs `seed-*` steps before replicant-qa)
    _, qa = _plan(role="qa", agent="qa-runner", environment_ref="python-service", repo_profile=PROFILE)
    qdoc = env["s3"].json("local-replicant-local-artifacts", qa["run_plan"]["key"])
    assert [x["name"] for x in qdoc["environment"]["bootstrap_steps"]] == ["seed-cache"]
    assert qdoc["environment"]["commands"]["test"] == "uv run pytest -q"
    errs = [e for e in fakes.errors(fakes.ws03_validator("run-plan.schema.json"), doc) if "image" not in e]
    assert not errs, errs


def test_repo_profile_fields_are_sanitised() -> None:
    f = staging.repo_profile_fields({"repo_config": "maybe", "sha": "not-a-sha",
                                     "mcp_auto_allow": [{"name": "bad name", "command": "x"}, {"name": "ok", "command": ""},
                                                        {"name": "ok2", "command": "uvx", "args": ["pkg==1.0", 3]}]})
    assert f == {"repo_config": "ignore", "config_base_sha": None,
                 "mcp_auto_allow": [{"name": "ok2", "command": "uvx", "args": ["pkg==1.0", "3"]}]}


def test_repo_instructions_from_the_binding(env) -> None:  # noqa: F811
    b = bundle._local(RB)
    assert staging._repo_instructions(b, REPO) == "quote_as_context"
    _bindings(b, [{"name": "ledger", "priority": 100, "match": {"repoIds": ["1"]}, "repoInstructions": "ignore",
                   "environment": "environment/node-service"}], default={"repoInstructions": "quote_as_context"})
    assert staging._repo_instructions(b, REPO) == "ignore"
    assert staging._repo_instructions(b, {**REPO, "repo_id": "2", "name": "other"}) == "quote_as_context"


# ------------------------------------------------------------------------------------------------ environments
def _bindings(b: bundle.Bundle, items: list[dict[str, Any]], default: dict[str, Any] | None = None) -> None:
    ref = "policy/codebase-bindings@1"
    b.docs[ref] = {"apiVersion": "replicant.example.com/v1", "kind": "Policy",
                   "metadata": {"name": "codebase-bindings", "scope": {"level": "global"}, "owners": ["x"]},
                   "spec": {"policyType": "codebase_bindings", "bindings": items,
                            "default": default or {"environment": "environment/generic"}}}
    b.digests[ref] = "sha256:" + "0" * 64


def test_select_environment() -> None:
    b = bundle._local(RB)
    assert core.select_environment(b, None, REPO) == ("go-service", "default")  # old jobs and replays
    assert core.select_environment(b, PROFILE, REPO) == ("python-service", "repo_profile")
    assert core.select_environment(b, {**PROFILE, "environment": "node-service"}, REPO) == ("node-service", "repo_profile")
    assert core.select_environment(b, {**PROFILE, "environment": "rust-service"}, REPO) == ("go-service", "default")
    assert core.select_environment(b, {**PROFILE, "environment": None}, REPO) == ("go-service", "default")
    _bindings(b, [{"name": "pay", "priority": 200, "match": {"repos": ["git.replicant.localhost:3300/acme-payments/*"]},
                   "environment": "environment/node-service"},
                  {"name": "missing", "priority": 900, "match": {"repoIds": ["1"]}, "environment": "environment/nextjs-app"}])
    # an explicit binding overrides the profile; one naming an environment the bundle lacks does not
    assert b.binding_for(REPO)["name"] == "missing"
    assert core.select_environment(b, PROFILE, {**REPO, "repo_id": "9"}) == ("node-service", "binding")
    assert core.select_environment(b, PROFILE, REPO) == ("python-service", "repo_profile")
    assert core.select_environment(b, None, {**REPO, "repo_id": "9"}) == ("go-service", "default")


def test_graph_compose_environment_and_readiness_graph() -> None:
    c = run(core.graph_compose, {"release_bundle_id": RB, "workflow_type": "bugfix", "repo": "x"})
    assert c["environment"] == "go-service" and c["environment_source"] == "default"
    c = run(core.graph_compose, {"release_bundle_id": RB, "workflow_type": "bugfix", "repo": "x",
                                 "repo_profile": PROFILE, "repo_ref": REPO})
    assert c["environment"] == "python-service"
    assert c["environment_digest"] == local_bundle.pinned("environment/python-service@1")["digest"]
    r = run(core.graph_compose, {"release_bundle_id": RB, "workflow_type": "qa_only", "repo": "x",
                                 "labels": [local_bundle.READINESS_LABEL], "repo_profile": {**PROFILE, "environment": "node-service"},
                                 "repo_ref": REPO})
    assert r["graph"]["name"] == local_bundle.READINESS_GRAPH and r["environment"] == "node-service"
    assert [n["key"] for n in r["spec"]["nodes"]] == ["context", "qa"]
    q = run(core.graph_compose, {"release_bundle_id": RB, "workflow_type": "qa_only", "repo": "x"})
    assert q["graph"]["name"] == "qa-only"


def test_readiness_graph_and_router_rule() -> None:
    doc = local_bundle.readiness_graph()
    g = parse_definition(doc, version=1, digest="sha256:" + "0" * 64)
    validate(g, catalog=registry.catalog_entries())
    req = {**REQ, "workflow_type": "auto", "constraints": {"labels": [local_bundle.READINESS_LABEL]},
           "source": {**REQ["source"], "channel": "ui"}}
    d = core.classify_request(req, local_bundle.policies()["router_rules"])
    assert d["workflow_type"] == "qa_only" and d["rules_fired"] == ["v0-readiness"]


def test_environment_definitions() -> None:
    docs = local_bundle.documents()
    py, node = docs["environment/python-service@1"]["spec"], docs["environment/node-service@1"]["spec"]
    assert py["image"]["resolved"]["image"] == "localhost/replicant/env-python-service:dev"
    assert node["image"]["resolved"]["image"] == "localhost/replicant/env-node-service:dev"
    assert (py["codebaseType"], node["codebaseType"]) == ("python_service", "node_service")
    assert py["packageMirrors"] == ["pypi"] and node["packageMirrors"] == ["npm"]
    # qa runs only the seed-* step (the entrypoint skips the rest for qa); the deps step is the no-profile fallback
    assert "qa" in py["bootstrap"]["forRoles"] and py["bootstrap"]["steps"][0]["name"] == "seed-cache"
    assert "uv sync --frozen" in py["bootstrap"]["steps"][1]["run"]
    assert "pnpm install --frozen-lockfile" in node["bootstrap"]["steps"][1]["run"]
    assert py["resourceProfiles"]["default"]["limits"]["memory"] == "2Gi"  # laptop-sized like go-service


# ------------------------------------------------------------------------------------------------ observations, readiness
QA_RR = {"schema": "replicant.role-result.qa/v1", "verdict": "fail",
         "stages": [{"stage": "install", "status": "passed"},
                    {"stage": "unit", "status": "failed", "cause": "missing_tool", "detail": "vips"},
                    {"stage": "lint", "status": "failed"}],
         "cli": {}}


def test_qa_causes_and_readiness_status() -> None:
    causes = inventory.qa_causes(QA_RR)
    assert causes == [{"stage": "unit", "cause": "missing_tool", "detail": "vips"},
                      {"stage": "lint", "cause": "lint_failure", "detail": None}]
    assert inventory.readiness_status("fail", causes) == "blocked"
    assert inventory.readiness_status("fail", [{"stage": "unit", "cause": "test_failure"}]) == "warnings"
    assert inventory.readiness_status("pass", []) == "ready"
    assert inventory.readiness_status("pass_with_warnings", [{"stage": "lint", "cause": "lint_failure"}]) == "warnings"
    assert inventory.readiness_status("not_applicable", []) == "warnings"
    assert inventory.readiness_status("inconclusive", [{"cause": "missing_tool"}]) == "blocked"
    assert inventory.readiness_status(None, []) == "blocked"
    assert inventory.qa_causes({}, failure_reason="EnvironmentSetupFailed") == [
        {"stage": "prepare", "cause": "install_failure", "detail": "EnvironmentSetupFailed"}]


def test_observation_items() -> None:
    rr = {"cli": {"repo_config": {
        "mode": "use", "settings_hash": "sha256:" + "5" * 64,
        "hooks": [{"event": "PostToolUse", "command_hash": "sha256:" + "1" * 64, "runs": 4, "failures": 0, "cause": None},
                  {"event": "Stop", "command_hash": "sha256:" + "2" * 64, "runs": 1, "failures": 1, "cause": "missing_tool"}],
        "mcp_skipped": [{"name": "jira", "identity": "url:https://jira.acme.internal", "reason": "not_allowed"}],
        "mcp_allowed": ["schema"]},
        "mcp_servers": [{"name": "schema", "status": "failed"}]}}
    items = inventory.observation_items(rr, environment="python-service", mcp_auto_allow=PROFILE["mcp_auto_allow"],
                                        qa_causes_list=[{"stage": "unit", "cause": "test_failure", "detail": "x"},
                                                        {"stage": "unit", "cause": "missing_tool", "detail": "lib vips"}])
    assert items == [
        {"kind": "hook", "identity": "hook:PostToolUse:sha256:" + "1" * 64, "status": "loaded", "cause": None},
        {"kind": "hook", "identity": "hook:Stop:sha256:" + "2" * 64, "status": "failed", "cause": "missing_tool"},
        {"kind": "mcp_server", "identity": "url:https://jira.acme.internal", "status": "skipped", "cause": "not_allowed"},
        {"kind": "mcp_server", "identity": "stdio:npx:@acme/schema-mcp@1.2.0", "status": "failed", "cause": "start_failed"},
        {"kind": "probe_failure", "identity": "probe:python-service:missing_tool:lib-vips", "status": "failed",
         "cause": "missing_tool"}]  # a job's failing test is not an environment observation
    probe = inventory.observation_items({}, environment="node-service", mcp_auto_allow=[], all_causes=True,
                                        qa_causes_list=[{"stage": "unit", "cause": "test_failure", "detail": None}])
    assert probe == [{"kind": "probe_failure", "identity": "probe:node-service:test_failure", "status": "failed",
                      "cause": "test_failure"}]


def _result(env, key: str, role_result: dict[str, Any]) -> str:  # noqa: F811
    env["s3"].objs[("local-replicant-local-artifacts", key)] = json.dumps(
        {"schema": "replicant.run-result/v1", "run_id": "run_x", "outcome": "failed", "role_result": role_result}).encode()
    return f"s3://local-replicant-local-artifacts/{key}"


def test_record_observations_and_readiness(env) -> None:  # noqa: F811
    seen: list[tuple[str, str, dict]] = []

    def ok(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer cp-secret"
        seen.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(202, json={})
    env["router"].on("POST", "/internal/v1/inventory/observations", ok)
    env["router"].on("PUT", "/internal/v1/repos/", ok)
    uri = _result(env, "tenants/payments/jobs/j/runs/r/run_result.json", QA_RR)
    r = run(inventory.inventory_record_observations, {
        "job_id": JOB, "tenant_id": T, "run_id": "run_x", "role": "qa", "repo_id": "7", "workflow_type": "qa_only",
        "result_uri": uri, "environment": "node-service", "mcp_auto_allow": [], "readiness": True,
        "tier": None}, activity_id="probe.c0.n1.observe")
    assert r == {"recorded": True, "items": 2}
    m, path, body = seen[-1]
    assert (m, path) == ("POST", "/internal/v1/inventory/observations")
    assert set(body) == {"job_id", "run_id", "repo_id", "workflow_type", "role", "items", "tier", "model", "settings_hash"}
    assert body["items"][0]["identity"] == "probe:node-service:missing_tool:vips"
    r = run(inventory.inventory_record_readiness, {
        "job_id": JOB, "tenant_id": T, "repo_id": "7", "profile_id": "rp_01", "outcome": "qa_failed", "verdict": "fail",
        "qa_result_uri": uri, "qa_failure_reason": None, "environment": "node-service"}, activity_id="finalize.c0.n1.readiness")
    assert r == {"recorded": True, "status": "blocked"}
    m, path, body = seen[-1]
    assert (m, path) == ("PUT", "/internal/v1/repos/7/readiness")
    assert body == {"job_id": JOB, "profile_id": "rp_01", "status": "blocked",
                    "causes": [{"stage": "unit", "cause": "missing_tool", "detail": "vips"},
                               {"stage": "lint", "cause": "lint_failure", "detail": None}]}
    # nothing observed and no tier: no call
    n = len(seen)
    r = run(inventory.inventory_record_observations, {
        "job_id": JOB, "tenant_id": T, "run_id": "run_y", "role": "implementer", "repo_id": "7", "workflow_type": "bugfix",
        "result_uri": _result(env, "tenants/payments/jobs/j/runs/y/run_result.json", {"cli": {}}),
        "environment": "python-service", "mcp_auto_allow": [], "tier": None}, activity_id="implement.c0.n1.observe")
    assert r["recorded"] is False and len(seen) == n


def test_inventory_calls_are_best_effort(env) -> None:  # noqa: F811
    env["router"].on("PUT", "/internal/v1/repos/", lambda req: httpx.Response(404, json={"code": "not_found"}))
    r = run(inventory.inventory_record_readiness, {
        "job_id": JOB, "tenant_id": T, "repo_id": "7", "profile_id": "rp_01", "outcome": "qa_completed", "verdict": "pass",
        "qa_result_uri": None, "qa_failure_reason": None}, activity_id="finalize.c0.n1.readiness")
    assert r == {"recorded": False, "reason": "http_404", "status": "ready"}
    r = run(inventory.inventory_record_observations, {
        "job_id": JOB, "tenant_id": T, "run_id": "run_z", "role": "implementer", "repo_id": "7", "workflow_type": "bugfix",
        "result_uri": "s3://local-replicant-local-artifacts/missing.json", "environment": "python-service",
        "mcp_auto_allow": [], "tier": {"tier": "standard", "model": SONNET}}, activity_id="implement.c0.n1.observe")
    assert r["recorded"] is False  # control-plane-api has no route here (404): dropped, never raised


# ------------------------------------------------------------------------------------------------ pr_inspect via replicant_scm
def test_pr_inspect_reads_through_gitea_client(env) -> None:  # noqa: F811
    import asyncio

    from temporalio.exceptions import ApplicationError

    from replicant_worker.activities import pr_inspect
    r = env["router"]
    r.on("GET", "/api/v1/repos/acme/billing/raw/CLAUDE.md", lambda req: httpx.Response(200, content=b"# rules\n" * 10))
    r.on("GET", "/api/v1/repos/acme/billing/raw/logo.png", lambda req: httpx.Response(200, content=b"\x89PNG\x00\x00"))
    r.on("GET", "/api/v1/repos/acme/billing/raw/secret", lambda req: httpx.Response(403, json={}))
    r.on("GET", "/api/v1/repos/acme/billing/git/trees/", lambda req: httpx.Response(200, json={
        "tree": [{"path": "CLAUDE.md", "type": "blob", "size": 80}, {"path": "src", "type": "tree"}], "truncated": False}))
    g = pr_inspect.Gitea("http://gitea.test", "acme", "billing", "tok")
    assert asyncio.run(g.file_at("a" * 40, "CLAUDE.md", max_bytes=16)) == "# rules\n# rules\n"
    assert asyncio.run(g.file_at("a" * 40, "logo.png")) is None  # binary
    assert asyncio.run(g.file_at("a" * 40, "missing.md")) is None  # 404
    assert asyncio.run(g.tree("a" * 40)) == ["CLAUDE.md"]
    with pytest.raises(ApplicationError) as ei:
        asyncio.run(g.file_at("a" * 40, "secret"))
    assert ei.value.type == "PermissionDenied"
    raw = [c for c in r.calls if "/raw/CLAUDE.md" in c.url.path][0]
    assert raw.headers["authorization"] == "token tok" and raw.url.params["ref"] == "a" * 40
