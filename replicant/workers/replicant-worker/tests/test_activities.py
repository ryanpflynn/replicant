"""Unit tests of the v0 activities against contract fakes (no network, no containers).

Each peer is faked from its contract: runner-local from C-RUNNER 4.0.0 (RunSpec with run_scope, Problem codes),
the gateway shim from gateway-interface.openapi.yaml (RunPolicy), control-plane-api from its OpenAPI
(postNotification), S3 from C-ART write-once semantics. Run: uv run pytest workers/replicant-worker/tests/test_activities.py
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import io
import json
import tarfile
from typing import Any

import fakes
import httpx
import pytest
import zstandard
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from replicant_worker import ids, local_bundle, resources
from replicant_worker.activities import (
    base,
    bundle,
    config,
    core,
    gates,
    notify,
    qa,
    repo_bundle,
    runner,
    s3,
    staging,
)

JOB = ids.derived("job", "test", "activities")
T = "payments"
RB = "rb_ds5p3wmcj4dag2e9a8ncp4py6k"
ART = "local-replicant-local-artifacts"
REQ = json.loads((resources.CONTRACTS / "job-request.schema.json").read_text())["examples"][0]
_REAL_BUILD = repo_bundle.build
REPO = {"provider": "gitea", "host": "git.replicant.localhost:3300", "owner": "acme-payments", "name": "ledger-api", "repo_id": "1"}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    for f, v in (("runner", "runner-secret"), ("gw", "gw-secret"), ("cp", "cp-secret")):
        (tmp_path / f).write_text(v)
    monkeypatch.setenv("REPLICANT_ENV", "local")
    monkeypatch.setenv("REPLICANT_RUNNER_URL", "http://runner.test")
    monkeypatch.setenv("REPLICANT_RUNNER_TOKEN_FILE", str(tmp_path / "runner"))
    monkeypatch.setenv("REPLICANT_GATEWAY_URL", "http://gw.test")
    monkeypatch.setenv("REPLICANT_GATEWAY_SERVICE_TOKEN_FILE", str(tmp_path / "gw"))
    monkeypatch.setenv("REPLICANT_CONTROL_PLANE_URL", "http://cp.test")
    monkeypatch.setenv("REPLICANT_CONTROL_PLANE_TOKEN_FILE", str(tmp_path / "cp"))
    monkeypatch.setenv("REPLICANT_GATEWAY_SCRIPT_SET", "ledger-api-v0")
    monkeypatch.delenv("REPLICANT_GATEWAY_CASSETTE_SET", raising=False)
    config.settings.cache_clear()
    bundle._cache.clear()
    fs3 = fakes.FakeS3()
    s3.set_client(fs3)
    router = fakes.Router()
    base.set_http(router.client())
    builds: list[dict[str, Any]] = []

    async def fake_build(repo, refs, dest, *, job_id):  # the SCM fetch is covered by test_repo_bundle_real_git
        builds.append({"repo": dict(repo), "refs": dict(refs), "job_id": job_id})
        dest.write_bytes(b"# v2 git bundle\n" + json.dumps(refs, sort_keys=True).encode())
    monkeypatch.setattr(repo_bundle, "build", fake_build)
    yield {"s3": fs3, "router": router, "builds": builds}
    s3.set_client(None)
    base.set_http(None)
    config.settings.cache_clear()


def run(fn, arg, *, activity_id: str = "1", workflow_id: str = JOB) -> Any:
    e = ActivityEnvironment()
    e.info = dataclasses.replace(e.info, workflow_id=workflow_id, activity_id=activity_id)
    return asyncio.run(e.run(fn, arg))


def diff_bundle(paths_content: dict[str, str]) -> bytes:
    patch = "".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+{c}\n" for p, c in paths_content.items())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for n, b in (("changes.patch", patch.encode()),
                     ("metadata.json", json.dumps({"base_sha": "8" * 40, "paths": list(paths_content)}).encode())):
            ti = tarfile.TarInfo(n)
            ti.size = len(b)
            tf.addfile(ti, io.BytesIO(b))
    return zstandard.ZstdCompressor().compress(buf.getvalue())


# ------------------------------------------------------------------------------------------------ JCS, run scope
def test_spec_digest_matches_c_runner_test_vector() -> None:
    import yaml
    d = yaml.safe_load((resources.CONTRACTS / "runner-api.openapi.yaml").read_text())
    ex = d["components"]["examples"]["ImplementerRunSpec"]["value"]
    assert runner.spec_digest(ex) == "sha256:a05ea6bc4aea3f59a4b8f7a20dea9badc8e8d1dcbb8f21b696224337bbabde06"
    ex2 = copy.deepcopy(ex)
    ex2["run_scope"] = {**ex["run_scope"], "scopes": ["gateway:invoke"]}
    ex2["trace_context"] = {"traceparent": "00-fc0575170c8597ce9805552723c08d6e-7f3e9d2c1b0a4958-01"}
    assert runner.spec_digest(ex2) == runner.spec_digest(ex)
    ex2["limits"]["max_cost_usd"] = 14
    assert runner.spec_digest(ex2) != runner.spec_digest(ex)


def test_no_run_grant_signing_left() -> None:
    """SIMPLIFY-D50: the worker holds no signing key and sends no run grant; the activity input keeps its `grant` key
    so recorded workflow histories replay unchanged."""
    import importlib.util
    assert importlib.util.find_spec("replicant_worker.activities.grants") is None
    assert not any(f.name.startswith("grant") for f in dataclasses.fields(config.Settings))
    sc = runner.run_scope({"repo_ids": ["1", "2"], "audiences": ["runner-sandbox", "ai-gateway", "ai-gateway"],
                           "scopes": ["gateway:invoke"], "acting_for": "usr_x", "budget_usd": 3, "lifetime_seconds": 9})
    assert sc == {"repo_ids": ["1", "2"], "audiences": ["ai-gateway", "runner-sandbox"], "scopes": ["gateway:invoke"],
                  "acting_for": "usr_x"}


# ------------------------------------------------------------------------------------------------ bundle, router
def test_local_bundle_documents_are_contract_valid() -> None:
    env_v = fakes.openapi_validator  # noqa: F841
    envelope = json.loads((resources.CONTRACTS / "schemas" / "definition-envelope.schema.json").read_text())
    ev = __import__("jsonschema").Draft202012Validator(envelope)
    agent_v = fakes.ws03_validator("agent-definition.schema.json")
    envd_v = fakes.ws03_validator("environment-definition.schema.json")
    for ref, doc in local_bundle.documents().items():
        assert not fakes.errors(ev, doc), (ref, fakes.errors(ev, doc))
        if ref.startswith("agent/"):
            errs = [e for e in fakes.errors(agent_v, doc) if "cliImage" not in e and "image" not in e]
            assert not errs, (ref, errs)  # local image refs are tags, not digests (recorded deviation)
        if ref.startswith("environment/"):
            errs = [e for e in fakes.errors(envd_v, doc) if "image" not in e]
            assert not errs, (ref, errs)
    rb = json.loads((resources.CONTRACTS / "schemas" / "release-bundle.schema.json").read_text())
    full = {"apiVersion": "replicant.example.com/v1", "kind": "ReleaseBundle",
            "metadata": {"display_version": "2026.10.24-1", "created_at": "2026-10-24T00:00:00Z", "created_by": "svc_replicant-worker"},
            "spec": local_bundle.bundle_spec("b20261024-000000000000")}
    errs = fakes.errors(__import__("jsonschema").Draft202012Validator(rb), full)
    assert not errs, errs
    pol = local_bundle.policies()
    assert pol["repair_limits"]["repair_cycles"]["default"] == 2  # bounded PR review loop (MEM-PR-D9)
    assert pol["router_rules"]["llm_risk_review"] is False


def test_router_rules_route_gitea_issue_to_bugfix() -> None:
    rules = local_bundle.policies()["router_rules"]
    req = copy.deepcopy(REQ)
    req["source"]["event_type"], req["workflow_type"] = "issues.labeled", "auto"
    d = run(core.router_classify, {"request": req, "router_rules": rules})
    assert d == {"workflow_type": "bugfix", "method": "rule", "rules_fired": ["v0-issue-trigger-is-bugfix"], "requested_type": "auto"}
    req["workflow_type"] = "bugfix"
    assert run(core.router_classify, {"request": req, "router_rules": rules})["rules_fired"] == ["requested-type-honored"]
    req["workflow_type"], req["source"] = "auto", {"channel": "ui"}
    assert run(core.router_classify, {"request": req, "router_rules": rules})["needs_intent"] is True
    with pytest.raises(ApplicationError) as ei:
        run(core.router_parse_intent, {"candidates": ["bugfix"]})
    assert ei.value.non_retryable


def test_bundle_core_and_compose_from_local_set() -> None:
    b = run(core.load_bundle_core, {"release_bundle_id": RB})
    assert b["source"] == "local" and {"router_rules", "repair_limits", "runner_polling", "gate_rules"} <= set(b["policies"])
    c = run(core.graph_compose, {"release_bundle_id": RB, "workflow_type": "bugfix", "repo": "x"})
    assert c["environment"] == "go-service" and c["environment_digest"].startswith("sha256:")
    assert {n["key"] for n in c["spec"]["nodes"]} >= {"implement", "qa", "review", "publish_gate", "publish", "pr_wait"}


def test_bundle_from_registry_bucket(env) -> None:
    # runner-local resolves the same objects (RUNNER-06): pointer -> bundle blob (= JCS(spec)) -> definition blobs,
    # laid out by the shared replicant_cdef writer (CONTROL-06)
    rb, objs = local_bundle.bundle_registry_objects("b20261024-000000000000")
    for k, body in objs.items():
        env["s3"].objs[("local-replicant-local-registry", k)] = body
    b = bundle.load(rb)
    assert b.source == "registry"
    pin, _ = b.agent("implementer-claude")
    assert pin == local_bundle.pinned("agent/implementer-claude@1")
    # a blob whose bytes do not hash to its key is refused (C-DEF §6), never silently replaced
    import replicant_cdef
    bad_rb, digest, body = replicant_cdef.bundle_blob({**local_bundle.bundle_spec("b20261024-000000000001")})
    env["s3"].objs[("local-replicant-local-registry", replicant_cdef.blob_key(digest))] = body + b" "
    k, pb = replicant_cdef.bundle_pointer(bad_rb, digest)
    env["s3"].objs[("local-replicant-local-registry", k)] = pb
    bundle._cache.clear()
    assert bad_rb != rb
    with pytest.raises(ApplicationError) as ei:
        bundle.load(bad_rb)
    assert ei.value.type == "DefinitionDigestMismatch"


def test_cdef_vectors_and_shared_digests() -> None:
    """The shared C-DEF vectors (libs/python/replicant-cdef) hold for the worker's own JCS too."""
    import importlib.resources

    import replicant_cdef
    from replicant_worker.activities import jcs
    v = json.loads(importlib.resources.files("replicant_cdef").joinpath("vectors.json").read_text())
    assert jcs.digest(v["definition"]["document"]) == v["definition"]["digest"] == \
        replicant_cdef.definition_digest(v["definition"]["document"])
    assert jcs.canonical(v["definition"]["document"]).decode() == v["definition"]["body_utf8"]
    rb, digest = replicant_cdef.bundle_digest(v["bundle"]["spec"])
    assert (rb, digest) == (v["bundle"]["release_bundle_id"], v["bundle"]["digest"])
    assert jcs.digest(v["bundle"]["spec"]) == digest
    for ref, doc in local_bundle.documents().items():
        assert local_bundle.pinned(ref)["digest"] == replicant_cdef.definition_digest(doc)


# ------------------------------------------------------------------------------------------------ the Runner-call sequence
def _gateway(env) -> list[dict[str, Any]]:
    v = fakes.openapi_validator("03-agents-and-tools/gateway-interface.openapi.yaml", "RunPolicy")
    seen: list[dict[str, Any]] = []

    def put(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer gw-secret"
        body = json.loads(req.content)
        assert not fakes.errors(v, body), fakes.errors(v, body)
        seen.append(body)
        return httpx.Response(200, json=body)
    env["router"].on("PUT", "/internal/v1/runs/", put)

    def job_budget(req: httpx.Request) -> httpx.Response:
        assert json.loads(req.content) == {"tenant_id": T, "job_budget_usd": 40.0} and req.headers["idempotency-key"]
        seen.append({"job_budget": True})
        return httpx.Response(200, json={})
    env["router"].on("PUT", "/internal/v1/jobs/", job_budget)
    return seen


def _runner(env, *, first_status: int = 201, problem: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    v = fakes.openapi_validator("00-contracts/runner-api.openapi.yaml", "RunSpec")
    seen: list[dict[str, Any]] = []
    state = {"n": 0}

    def post(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        assert req.headers["authorization"] == "Bearer runner-secret"
        key = json.loads(req.headers["idempotency-key"])  # RFC 9651 sf-string
        spec = json.loads(req.content)
        assert not fakes.errors(v, spec), fakes.errors(v, spec)
        assert "run_grant" not in spec
        run_id = ids.run_id_for(spec["tenant_id"], key)
        seen.append({"key": key, "spec": spec, "digest": runner.spec_digest(spec)})
        if problem and state["n"] == 1:
            return httpx.Response(problem.pop("status"), json=problem, headers={"Retry-After": "7"})
        return httpx.Response(first_status if state["n"] == 1 else 200,
                              json={"run_id": run_id, "phase": "Pending", "terminal": False, "idempotency_key": key,
                                    "spec_digest": runner.spec_digest(spec)})
    env["router"].on("POST", "/v1/runs", post)
    return seen


def _run_spec(plan: dict[str, Any], staged: dict[str, Any], *, lid: str, role: str, max_cost: float) -> dict[str, Any]:
    """The decision fields exactly as NodeContext._run_spec builds them (graphs/README.md §2.4)."""
    repo = {**REPO, "base_ref": "main", "base_sha": "8" * 40}
    return {"schema": "replicant.run-spec/v1", "tenant_id": T, "job_id": JOB, "workflow_id": JOB, "logical_activity_id": lid,
            "role": role, "workload_class": "coding", "workflow_type": "bugfix", "release_bundle_id": RB,
            "agent_definition": {"kind": "agent", **plan["agent_ref"]},
            "environment_definition": {"kind": "environment", **plan["environment_ref"]},
            "resource_profile": plan["resource_profile"], "repo": repo,
            "inputs": dict(staged["inputs"]), "artifact_prefixes": dict(staged["artifact_prefixes"]),
            "network_profile": plan["network_profile"],
            "timeouts": {k: int(plan["timeouts"][k]) for k in ("startup_seconds", "active_deadline_seconds", "finalize_seconds")},
            "limits": {"max_cost_usd": max_cost}, "steering": {"enabled": True}, "priority": "normal",
            "callback": {"kind": "temporal_signal", "namespace": "replicant-local", "workflow_id": JOB, "signal_name": "run.status_changed"}}


def _sequence(env, role: str = "implementer", agent: str = "implementer-claude", node: str = "implement",
              refs: dict[str, str] | None = None) -> dict[str, Any]:
    lid = f"{node}.c0.n1.run"
    key = ids.idempotency_key(JOB, lid)
    run_id = ids.run_id_for(T, key)
    b = {"job_id": JOB, "tenant_id": T, "run_id": run_id, "role": role, "node_key": node, "cycle": 0,
         "release_bundle_id": RB, "parent_job_id": None}
    brief = run(staging.context_build_briefing, {"job_id": JOB, "tenant_id": T, "release_bundle_id": RB,
                                                  "memory_snapshot_id": ids.derived("msnap", JOB), "request": REQ, "refs": {},
                                                  "briefing": "standard", "cycle": 0, "working_memory": None},
                activity_id="context.c0.n1.briefing")
    plan = run(staging.agents_build_run_plan, {**b, "ordinal": 1, "workflow_type": "bugfix", "agent_ref": agent,
                                               "environment_ref": "go-service", "workload_class": "coding",
                                               "resource_profile": "default", "timeouts": None, "network_profile": "coding-default",
                                               "steering_requested": True, "deterministic": False,
                                               "received_at": REQ["received_at"], "base_sha": "8" * 40, "job_cap_usd": 40},
               activity_id=f"{node}.c0.n1.plan")
    pdoc = env["s3"].json(ART, plan["run_plan"]["key"])
    v = fakes.ws03_validator("run-plan.schema.json")
    errs = [e for e in fakes.errors(v, pdoc) if "image" not in e]
    assert not errs, errs
    alloc = run(core.budget_allocate_run, {**b, "plan_budget_usd": plan["max_cost_usd"], "job_cap_usd": 40}, activity_id=f"{node}.c0.n1.alloc")
    srefs = {"briefing_uri": brief["briefing_uri"], "task_packet_uri": brief["task_packet_uri"], **(refs or {})}
    stage_in = {**b, "refs": srefs, "memory_snapshot_id": ids.derived("msnap", JOB), "working_memory": None, "steering": [],
                "steering_refs": [], "approver_feedback": [], "latest_gate_decision": None, "run_plan": plan["run_plan"],
                "qa_resume": None, "qa_config_remove": None, "extra": {}, "agent_definition": plan["agent_ref"],
                "environment_definition": plan["environment_ref"]}
    staged = run(staging.context_stage_inputs, stage_in, activity_id=f"{node}.c0.n1.stage")
    again = run(staging.context_stage_inputs, stage_in, activity_id=f"{node}.c0.n1.stage")  # retry: same manifest digest
    assert again["inputs"] == staged["inputs"]
    man = env["s3"].json(ART, staged["inputs"]["manifest_uri"].split("/", 3)[3])
    errs = fakes.errors(fakes.ws03_validator("run-inputs.schema.json"), man)
    assert not errs, errs
    assert hashlib.sha256(env["s3"].objs[(ART, staged["inputs"]["manifest_uri"].split("/", 3)[3])]).hexdigest() == \
        staged["inputs"]["manifest_sha256"]
    max_cost = round(min(alloc["budget_usd"], plan["max_cost_usd"], 40.0), 2)
    gw = run(staging.agents_stage_inputs, {**b, "plan": plan, "budget_usd": max_cost, "inputs": staged["inputs"],
                                           "stub_selection": None, "job_cap_usd": 40.0},
             activity_id=f"{node}.c0.n1.stage_agent")
    spec = _run_spec(plan, staged, lid=lid, role=role, max_cost=max_cost)
    grant = {"role": role, "repo_ids": ["1"], "audiences": plan["audiences"], "scopes": plan["scopes"], "budget_usd": max_cost,
             "acting_for": REQ["requester"]["principal_id"], "lifetime_seconds": sum(plan["timeouts"].values()) + 300}
    return {"key": key, "lid": lid, "run_id": run_id, "plan": plan, "staged": staged, "manifest": man, "gw": gw, "spec": spec,
            "start": {"idempotency_key": key, "logical_activity_id": lid, "role": role, "run_id": run_id, "workflow_id": JOB,
                      "tenant_id": T, "job_id": JOB, "run_spec": spec, "grant": grant}}


def test_stub_script_set_per_repo(env, monkeypatch) -> None:
    """REPLICANT_GATEWAY_SCRIPT_SETS picks the script set by the plan's repo_name; other repos keep the default set."""
    monkeypatch.setenv("REPLICANT_GATEWAY_SCRIPT_SETS", "billing-py=walkthrough-billing-py, bad, =x")
    config.settings.cache_clear()
    assert config.settings().script_sets == {"billing-py": "walkthrough-billing-py"}

    def stub(repo: str | None) -> dict:
        plan = {"repo_name": repo} if repo else {}
        return staging.stub_for({"role": "implementer", "node_key": "implement", "cycle": 0, "plan": plan})
    assert stub("billing-py") == {"root": "scripts", "set": "walkthrough-billing-py", "key": "implement.c0.n1"}
    assert stub("ledger-api")["set"] == stub(None)["set"] == "ledger-api-v0"


def test_runner_call_sequence_end_to_end(env) -> None:
    gw_seen, runner_seen = _gateway(env), _runner(env)
    s = _sequence(env)
    assert gw_seen[0] == {"job_budget": True}
    gw_seen = gw_seen[1:]
    assert gw_seen[0]["stub"] == {"root": "scripts", "set": "ledger-api-v0", "key": "implement.c0.n1"}
    assert gw_seen[0]["budget_usd"] == s["spec"]["limits"]["max_cost_usd"]
    places = [o["place"] for o in s["manifest"]["objects"]]
    assert places.count("run_plan") == 1 and "first_message" in places and "system_append" in places
    st = run(runner.runner_start_run, s["start"], activity_id=s["lid"])
    st2 = run(runner.runner_start_run, s["start"], activity_id=s["lid"])  # retry: same body, same spec_digest
    assert st["run_id"] == st2["run_id"] == s["run_id"]
    assert runner_seen[0]["digest"] == runner_seen[1]["digest"]
    assert runner_seen[0]["key"] == f"ik1:{JOB}:implement.c0.n1.run"
    sc = runner_seen[0]["spec"]["run_scope"]   # C-RUNNER 4.0.0: what the Runner checks against core's intake record
    assert sc["acting_for"] == REQ["requester"]["principal_id"] and sc["repo_ids"] == ["1"]
    assert sc["audiences"] == sorted(s["plan"]["audiences"]) and sc["scopes"] == sorted(s["plan"]["scopes"])
    # read_result: outputs become physical URIs in their bucket classes
    pre = f"tenants/{T}/jobs/{JOB}/runs/{s['run_id']}/"
    env["s3"].objs[(ART, pre + "run_result/result.json")] = json.dumps({
        "schema": "replicant.run-result/v1", "run_id": s["run_id"], "job_id": JOB, "role": "implementer", "outcome": "completed",
        "summary": "fixed", "usage": {"wall_seconds": 12}, "outputs": {"diff_bundle": pre + "diff_bundle/changes.tar.zst",
                                                                     "session_state": pre + "cli_session_state/s.tar.zst"}}).encode()
    rr = run(runner.runner_read_result, {"run_id": s["run_id"], "result_uri": f"s3://{ART}/{pre}run_result/result.json"})
    assert rr["outcome"] == "completed"
    assert rr["outputs"]["diff_bundle"] == f"s3://{ART}/{pre}diff_bundle/changes.tar.zst"
    assert rr["outputs"]["session_state"].startswith("s3://local-replicant-local-session/")


def test_runner_error_mapping(env) -> None:
    _gateway(env)
    _runner(env, problem={"status": 429, "code": "tenant_quota_exceeded", "retryable": True, "quota_used": 3, "quota_limit": 3})
    s = _sequence(env)
    with pytest.raises(ApplicationError) as ei:
        run(runner.runner_start_run, s["start"], activity_id=s["lid"])
    assert ei.value.type == "CapacityUnavailable" and ei.value.non_retryable and ei.value.details[0]["retry_after_seconds"] == 7
    env["router"].routes.clear()
    _runner(env, problem={"status": 422, "code": "admission_denied", "retryable": False, "title": "no"})
    with pytest.raises(ApplicationError) as ei:
        run(runner.runner_start_run, s["start"], activity_id=s["lid"])
    assert ei.value.type == "admission_denied" and ei.value.non_retryable
    env["router"].routes.clear()
    seen = _runner(env, problem={"status": 422, "code": "run_scope_invalid", "retryable": False})
    with pytest.raises(ApplicationError) as ei:  # the run is not what core accepted for the job: never retried
        run(runner.runner_start_run, s["start"], activity_id=s["lid"])
    assert ei.value.type == "run_scope_invalid" and ei.value.non_retryable and len(seen) == 1
    env["router"].routes.clear()
    env["router"].on("GET", "/v1/runs/", lambda r: httpx.Response(503, json={"code": "runner_unavailable", "retryable": True}))
    with pytest.raises(ApplicationError) as ei:
        run(runner.runner_get_run, {"run_id": s["run_id"]})
    assert not ei.value.non_retryable


def test_reviewer_and_qa_inputs_carry_the_diff(env) -> None:
    _gateway(env)
    pre = f"tenants/{T}/jobs/{JOB}/runs/run_aaaaaaaaaaaaaaaaaaaaaaaaaa/diff_bundle/changes.tar.zst"
    env["s3"].objs[(ART, pre)] = diff_bundle({"internal/ledger/ledger.go": "fixed"})
    plan_key = f"tenants/{T}/jobs/{JOB}/runs/run_aaaaaaaaaaaaaaaaaaaaaaaaaa/plan/plan.md"
    env["s3"].objs[(ART, plan_key)] = b"# plan"
    refs = {"latest_diff_bundle_uri": f"s3://{ART}/{pre}", "plan_uri": f"s3://{ART}/{plan_key}"}
    s = _sequence(env, role="reviewer", agent="reviewer-codex", node="review", refs=refs)
    objs = {o["name"]: o for o in s["manifest"]["objects"]}
    assert objs["diff_bundle"]["place"] == "apply_diff" and objs["changes_patch"]["path"] == "changes.patch"
    assert objs["plan"]["path"] == "plan.md"
    q = _sequence(env, role="qa", agent="qa-runner", node="qa", refs=refs)
    assert {o["place"] for o in q["manifest"]["objects"]} >= {"apply_diff", "run_plan"}


# ------------------------------------------------------------------------------------------------ repo bundle (SIMPLIFY-D40)
def test_repo_bundle_staged_per_run_and_reused_per_job(env) -> None:
    """Every run's manifest carries one repo_bundle under its own inputs/ prefix; the job builds it once per commit set."""
    _gateway(env)
    a = _sequence(env)
    b = _sequence(env, role="reviewer", agent="reviewer-codex", node="review")
    for s_ in (a, b):
        rb = [o for o in s_["manifest"]["objects"] if o["place"] == "repo_bundle"]
        assert len(rb) == 1 and rb[0]["name"] == "repo_bundle" and rb[0]["content_type"] == repo_bundle.CONTENT_TYPE
        assert rb[0]["key"] == f"tenants/{T}/jobs/{JOB}/runs/{s_['run_id']}/inputs/repo.bundle"
        body = env["s3"].objs[(ART, rb[0]["key"])]
        assert hashlib.sha256(body).hexdigest() == rb[0]["sha256"] and len(body) == rb[0]["size_bytes"]
    assert len(env["builds"]) == 1, env["builds"]  # second run: the job's cached bundle, server-side copied
    assert env["builds"][0]["refs"] == {repo_bundle.REF_BASE: "8" * 40} and env["builds"][0]["job_id"] == JOB
    assert env["builds"][0]["repo"]["name"] == REQ["targets"][0]["repo"]["name"]  # from the task packet (no patch)


def test_repo_bundle_refs_and_patched_repo(env) -> None:
    """config_base_sha (repo_config use) adds refs/replicant/config; the workflow's `repo` (repo-bundle-v1) wins."""
    assert repo_bundle.wanted_refs({"workspace": {"base_sha": "a" * 40}, "config_base_sha": "b" * 40}) == \
        {repo_bundle.REF_BASE: "a" * 40, repo_bundle.REF_CONFIG: "b" * 40}
    assert repo_bundle.wanted_refs({"workspace": {"base_sha": "a" * 40}, "config_base_sha": "a" * 40}) == {repo_bundle.REF_BASE: "a" * 40}
    assert repo_bundle.wanted_refs({"workspace": {}}, "c" * 40) == {repo_bundle.REF_BASE: "c" * 40}
    assert repo_bundle.wanted_refs({"workspace": {}}) == {}
    k1 = repo_bundle.cache_key(T, JOB, REPO, {repo_bundle.REF_BASE: "a" * 40})
    assert k1.startswith(f"tenants/{T}/jobs/{JOB}/repo-bundles/") and k1 != \
        repo_bundle.cache_key(T, JOB, REPO, {repo_bundle.REF_BASE: "b" * 40})
    out = asyncio.run(repo_bundle.stage(tenant=T, job=JOB, run_prefix=f"tenants/{T}/jobs/{JOB}/runs/run_x/", repo=REPO,
                                        refs={repo_bundle.REF_BASE: "a" * 40}))
    assert out and env["builds"][-1]["repo"] == REPO
    assert asyncio.run(repo_bundle.stage(tenant=T, job=JOB, run_prefix="p/", repo=None, refs={repo_bundle.REF_BASE: "a" * 40})) is None
    assert asyncio.run(repo_bundle.stage(tenant=T, job=JOB, run_prefix="p/", repo=REPO, refs={})) is None


def _git(cwd, *args: str) -> str:
    import subprocess
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.skipif(__import__("shutil").which("git") is None, reason="git not on PATH")
def test_repo_bundle_real_git(env, tmp_path, monkeypatch) -> None:
    """The real build: broker read token, fetch of the wanted commits (tip and non-tip) from the SCM, `git bundle`
    with exactly the replicant refs; an unknown commit is non-retryable."""
    from replicant_worker.activities import pr_inspect
    pr_inspect._tokens.clear()
    up = tmp_path / "scm" / "acme-payments" / "ledger-api.git"
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    (work / "a.txt").write_text("one\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "one")
    first = _git(work, "rev-parse", "HEAD")
    (work / "a.txt").write_text("two\n")
    _git(work, "commit", "-qam", "two")
    second = _git(work, "rev-parse", "HEAD")
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(up))
    (tmp_path / "broker").write_text("caller-secret")
    monkeypatch.setenv("REPLICANT_TOKEN_BROKER_TOKEN_FILE", str(tmp_path / "broker"))
    monkeypatch.setenv("REPLICANT_TOKEN_BROKER_URL", "http://broker.test")
    monkeypatch.setenv("REPLICANT_GITEA_BASE_URL", f"file://{tmp_path / 'scm'}")
    asks: list[dict[str, Any]] = []

    def mint(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer caller-secret"
        asks.append(json.loads(req.content))
        return httpx.Response(200, json={"token": "scm-read-token", "expires_at": "2099-01-01T00:00:00Z"})
    env["router"].on("POST", "/v1/installation-tokens", mint)
    dest = tmp_path / "out.bundle"
    refs = {repo_bundle.REF_BASE: second, repo_bundle.REF_CONFIG: first}
    asyncio.run(_REAL_BUILD(REPO, refs, dest, job_id=JOB))
    assert asks and asks[0]["permissions"] == {"contents": "read", "metadata": "read"} and asks[0]["purpose"] == repo_bundle.PURPOSE
    heads = dict(reversed(line.split()) for line in _git(tmp_path, "bundle", "list-heads", str(dest)).splitlines())
    assert heads == refs
    clone = tmp_path / "sandbox"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "fetch", "-q", str(dest), "+refs/replicant/*:refs/replicant/*")
    assert _git(clone, "rev-list", "--count", repo_bundle.REF_BASE) == "2"
    with pytest.raises(ApplicationError) as ei:
        asyncio.run(_REAL_BUILD(REPO, {repo_bundle.REF_BASE: "f" * 40}, tmp_path / "x.bundle", job_id=JOB))
    assert ei.value.type == "RepoCommitNotFound" and ei.value.non_retryable
    assert "scm-read-token" not in str(ei.value)


# ------------------------------------------------------------------------------------------------ QA, risk, verifier
def test_qa_classify_shapes(env) -> None:
    step = "qa.c0.n1"
    run_id = "run_bbbbbbbbbbbbbbbbbbbbbbbbbb"
    pre = f"tenants/{T}/jobs/{JOB}/runs/{run_id}/"
    env["s3"].objs[(ART, pre + "run_result/result.json")] = json.dumps({
        "schema": "replicant.run-result/v1", "run_id": run_id, "job_id": JOB, "role": "qa", "outcome": "completed", "summary": "",
        "usage": {"wall_seconds": 1}, "outputs": {},
        "role_result": {"schema": "replicant.role-result.qa/v1", "verdict": "pass", "reason_code": "all_passed",
                        "counts": {"failed": 0}}}).encode()
    a = {"job_id": JOB, "tenant_id": T, "step_id": step, "run_id": run_id, "workflow_type": "bugfix", "run_phase": "Succeeded",
         "result_uri": f"s3://{ART}/{pre}run_result/result.json", "fix_iteration": 0, "previous_blocking": []}
    out = run(qa.qa_classify, a, activity_id="qa.c0.n1.classify")
    assert out["verdict"] == "pass" and out["risk_floor_hint"] == "low" and out["agentrun_exit_class"] == "pass"
    out = run(qa.qa_classify, {**a, "step_id": "qa.c0.n2", "run_phase": "Failed", "failure_reason": "EnvironmentSetupFailed",
                               "result_uri": None} if False else {k: v for k, v in {**a, "step_id": "qa.c0.n2", "run_phase": "Failed",
                               "failure_reason": "EnvironmentSetupFailed"}.items() if k != "result_uri"}, activity_id="qa.c0.n2.classify")
    assert out["verdict"] == "inconclusive" and out["env_failure"]["signature"] == "env_setup_failed"
    assert run(qa.qa_stage_contract_stubs, {"job_id": JOB, "tenant_id": T, "step_id": step, "run_id": run_id,
                                           "environment_definition_digest": "sha256:" + "1" * 64,
                                           "targets": ["git.replicant.localhost:3300/acme-payments/ledger-api"]}) == {"staged": [], "missing": []}


def test_verifier_and_risk(env) -> None:
    key = f"tenants/{T}/jobs/{JOB}/runs/run_aaaaaaaaaaaaaaaaaaaaaaaaaa/diff_bundle/changes.tar.zst"
    raw = diff_bundle({"internal/ledger/ledger.go": "fixed"})
    env["s3"].objs[(ART, key)] = raw
    refs = {"latest_diff_bundle_uri": f"s3://{ART}/{key}"}
    v = run(core.verifier_check, {"job_id": JOB, "tenant_id": T, "refs": refs, "checks": None, "paths_scope": []})
    assert v["passed"] and v["diff_sha256"] == hashlib.sha256(raw).hexdigest() and v["paths"] == ["internal/ledger/ledger.go"]
    assert run(core.risk_classify, {"job_id": JOB, "tenant_id": T, "stage": "diff", "refs": refs, "targets": [],
                                    "workflow_type": "bugfix"})["level"] == "low"
    bad = f"tenants/{T}/jobs/{JOB}/runs/run_cccccccccccccccccccccccccc/diff_bundle/changes.tar.zst"
    env["s3"].objs[(ART, bad)] = diff_bundle({".github/workflows/ci.yml": "x", "a.go": "AKIAABCDEFGHIJKLMNOP"})
    refs = {"latest_diff_bundle_uri": f"s3://{ART}/{bad}"}
    v = run(core.verifier_check, {"job_id": JOB, "tenant_id": T, "refs": refs, "checks": None, "paths_scope": []})
    assert not v["passed"] and v["findings"] == 2
    assert run(core.risk_classify, {"job_id": JOB, "tenant_id": T, "stage": "diff", "refs": refs, "targets": [],
                                    "workflow_type": "bugfix"})["level"] == "critical"


# ------------------------------------------------------------------------------------------------ gates, notifications
def test_gate_routing_local_rules() -> None:
    facts = {"workflow_type": "bugfix", "risk_level": "low", "rules_fired": [], "requester": REQ["requester"]["principal_id"],
             "tenant_id": T, "repos": [], "require_gates": [], "cycle": 0, "changed_paths": [], "release_bundle_id": RB}
    plan = run(gates.gates_resolve_routing, {"gate_kind": "plan_approval", "facts": {**facts, "risk_level": "critical"}})
    pub = run(gates.gates_resolve_routing, {"gate_kind": "publish_approval", "facts": facts})
    assert plan["required"] is False  # plan gate off locally (plan.md §1.2), whatever the risk
    assert pub["required"] is True and pub["channels"] == ["ui", "cli", "slack"] and pub["eligible"]["requester_may_decide"]
    import jsonschema
    sch = json.loads((resources.ORCH / "schemas" / "gate-routing.v1.json").read_text())
    jsonschema.validate(pub, sch)


def test_packet_and_approval_record(env) -> None:
    key = f"tenants/{T}/jobs/{JOB}/runs/run_aaaaaaaaaaaaaaaaaaaaaaaaaa/diff_bundle/changes.tar.zst"
    raw = diff_bundle({"a.go": "b"})
    env["s3"].objs[(ART, key)] = raw
    gid = ids.gate_id_for(JOB, "publish_approval", 0, 1)
    a = {"job_id": JOB, "tenant_id": T, "gate_id": gid, "kind": "publish_approval", "revision": 1,
         "refs": {"latest_diff_bundle_uri": f"s3://{ART}/{key}"}, "risk": {"level": "low"}, "extra": {"node_key": "publish_gate"}}
    p1 = run(gates.gates_build_packet, a, activity_id="publish_gate.c0.n1.packet_publish__r1")
    p2 = run(gates.gates_build_packet, a, activity_id="publish_gate.c0.n1.packet_publish__r1")
    assert p1 == p2 and p1["diff_sha256"] == hashlib.sha256(raw).hexdigest()
    body = env["s3"].objs[(ART, f"tenants/{T}/jobs/{JOB}/gates/{gid}/packet-r1.json")]
    assert p1["digest"] == "sha256:" + hashlib.sha256(body).hexdigest()
    dec = ids.derived("dec", "ui", gid)
    rec = run(gates.gates_write_approval_record, {"job_id": JOB, "tenant_id": T, "gate_id": gid, "gate_kind": "publish_approval",
                                                  "decision": {"decision_id": dec, "decision": "approve", "principal_id": "usr_" + "a" * 26,
                                                               "channel": "api", "decided_at": "2026-10-24T08:00:00Z", "comment": "lgtm"},
                                                  "answer_text": None, "packet_uri": p1["packet_uri"], "source": "update",
                                                  "presented_digest": p1["digest"], "subject": {"diff_sha256": p1["diff_sha256"]}})
    text = env["s3"].objs[(ART, f"tenants/{T}/jobs/{JOB}/gates/{gid}/text-{dec}.json")]
    assert rec["text_sha256"] == hashlib.sha256(text).hexdigest()  # = sha256 of the approval_text object (approvals.sql)
    audit = env["s3"].json("local-replicant-local-audit", rec["key"])
    assert "lgtm" not in json.dumps(audit) and audit["text_sha256"] == rec["text_sha256"]  # audit holds no free text


def test_notifications(env) -> None:
    import yaml
    d = yaml.safe_load((resources.SPEC / "..").joinpath("..").exists() and "{}" or "{}")  # noqa: F841
    seen: list[dict[str, Any]] = []

    def post(req: httpx.Request) -> httpx.Response:
        b = json.loads(req.content)
        assert req.headers["authorization"] == "Bearer cp-secret" and b["dedupe_key"].startswith("ik1:")
        assert {"dedupe_key", "event_key", "tenant_id"} <= set(b)
        seen.append(b)
        return httpx.Response(202)
    env["router"].on("POST", "/internal/v1/notifications", post)
    gid = ids.gate_id_for(JOB, "publish_approval", 0, 1)
    r = run(notify.notify_gate_opened, {"job_id": JOB, "tenant_id": T, "gate_id": gid, "kind": "publish_approval",
                                        "channels": ["ui"], "packet_uri": "s3://x/y", "eligible": {}},
            activity_id="publish_gate.c0.n1.notify_open_publish_")
    assert r == {"delivered": True} and seen[0]["event_key"] == "gate.opened" and seen[0]["gate_id"] == gid
    env["router"].routes.clear()
    env["router"].on("POST", "/internal/v1/notifications", lambda req: httpx.Response(403))
    assert run(notify.notify_job_event, {"job_id": JOB, "tenant_id": T, "kind": "gate_held"},
               activity_id="x.c0.n1.notify_held")["delivered"] is False


def test_memory_snapshot_is_pinned_and_empty() -> None:
    """memory-service unreachable locally: the pinned empty snapshot (memory never blocks a job)."""
    from replicant_worker.activities import memory
    a = run(memory.memory_create_snapshot, {"job_id": JOB, "tenant_id": T, "mode": "create"})
    b = run(memory.memory_create_snapshot, {"job_id": JOB, "tenant_id": T, "mode": "revocation_filter",
                                          "base_snapshot_id": a["memory_snapshot_id"]})
    assert a["memory_snapshot_id"] == b["memory_snapshot_id"] == ids.derived("msnap", JOB) and a["entries"] == 0


def test_catalog_covers_every_replicant_worker_activity() -> None:
    from replicant_worker.activities import catalog_impl
    from replicant_worker.activities.options import CATALOG
    names = {getattr(f, "__temporal_activity_definition").name for fs in catalog_impl.ACTIVITIES_BY_QUEUE.values() for f in fs}
    want = {n for n, d in CATALOG.items() if d.klass.task_queue in catalog_impl.QUEUES}
    assert names == want
    v0_path = {"registry.load_bundle_core", "ops.read_kill_switches", "router.classify", "graph.compose", "memory.create_snapshot",
               "context.build_briefing", "agents.build_run_plan", "budget.allocate_run", "context.stage_inputs",
               "agents.stage_inputs", "runner.start_run", "runner.get_run", "runner.read_result", "cost.record_run",
               "qa.stage_contract_stubs", "qa.classify", "risk.classify", "verifier.check", "gates.resolve_routing",
               "gates.build_packet", "gates.write_approval_record", "identity.check_decision", "notify.gate_opened",
               "notify.gate_resolved", "artifacts.expire_session_state", "memory.record_outcome", "memory.write_episode"}
    assert v0_path <= set(catalog_impl.implemented())


# ------------------------------------------------------------------------------------------------ CONFIG: bundle rules
def test_risk_and_verifier_read_bundle_rules(env) -> None:
    key = f"tenants/{T}/jobs/{JOB}/runs/run_dddddddddddddddddddddddddd/diff_bundle/changes.tar.zst"
    env["s3"].objs[(ART, key)] = diff_bundle({"internal/ledger/ledger.go": "fixed", "db/schema.sql": "x"})
    refs = {"latest_diff_bundle_uri": f"s3://{ART}/{key}"}
    b = bundle._local("rb_cfgtestaaaaaaaaaaaaaaaaaaaa")
    pol = {"apiVersion": "replicant.example.com/v1", "kind": "Policy", "metadata": {"name": "risk-rules-cx"},
           "spec": {"policyType": "risk_rules", "large_change_files": 0, "sensitive_paths": [],
                    "forbidden_paths": ["db/**"]}}
    b.docs["policy/risk-rules-cx@1"], b.digests["policy/risk-rules-cx@1"] = pol, "sha256:" + "a" * 64
    bundle._cache[b.release_bundle_id] = b
    a = {"job_id": JOB, "tenant_id": T, "stage": "diff", "refs": refs, "targets": [], "workflow_type": "bugfix"}
    assert run(core.risk_classify, a)["level"] == "low"                              # no bundle id: the defaults
    r = run(core.risk_classify, {**a, "release_bundle_id": b.release_bundle_id})
    assert r["level"] == "medium" and "size.files_over_0" in r["rules_fired"]
    pol["spec"]["sensitive_paths"] = ["db/**"]
    assert run(core.risk_classify, {**a, "release_bundle_id": b.release_bundle_id})["level"] == "high"
    v = run(core.verifier_check, {"job_id": JOB, "tenant_id": T, "refs": refs, "checks": None, "paths_scope": [],
                                  "release_bundle_id": b.release_bundle_id})
    assert not v["passed"] and v["findings"] == 1


def test_graph_through_bundle_mapping_and_fallback_gap(monkeypatch) -> None:
    b = bundle._local("rb_cfgtestbbbbbbbbbbbbbbbbbbbb")
    ref, doc = b._by("graph", "bugfix")
    tuned = copy.deepcopy(doc)
    tuned["metadata"]["name"] = "bugfix-c0123456789"
    b.docs["graph/bugfix-c0123456789@1"], b.digests["graph/bugfix-c0123456789@1"] = tuned, "sha256:" + "b" * 64
    assert b.graph_for("bugfix")[0]["name"] == "bugfix"                               # no mapping: GRAPH_FILES
    b.graph_refs = {"bugfix": "graph/bugfix-c0123456789@1"}
    assert b.graph_for("bugfix")[0]["name"] == "bugfix-c0123456789"
    assert b.risk_rules() == bundle.RISK_DEFAULTS
    monkeypatch.setenv("REPLICANT_WORKER_BUILD_ID", "b20261024-000000000000")
    monkeypatch.delenv("REPLICANT_BUNDLE_FALLBACK_ANY", raising=False)
    base_rb = local_bundle.bundle_registry_objects("b20261024-000000000000")[0]
    assert bundle._fallback_allowed(base_rb) and not bundle._fallback_allowed("rb_someotherbundleaaaaaaaaaa")
