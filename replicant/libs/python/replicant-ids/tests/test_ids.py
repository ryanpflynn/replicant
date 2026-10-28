import json
import pathlib
import re

import pytest

import replicant_ids as f

V = json.loads((pathlib.Path(__file__).parent / "vectors.json").read_text())
JOB = "job_01m5p7x7r0bt69j0h0fbxepw90"


@pytest.mark.parametrize("v", V["derived"])
def test_derived_vectors(v):
    assert f.derived(v["prefix"], *v["fields"]) == v["out"]


def test_trace_vectors():
    for v in V["job_trace_id"]:
        assert f.job_trace_id(v["in"]) == v["out"]
    for v in V["job_span_id"]:
        assert f.job_span_id(v["in"]) == v["out"]
    for v in V["run_span_id"]:
        assert f.run_span_id(v["in"]) == v["out"]


def test_run_id_composition_matches_vector():
    key = f.idempotency_key(JOB, f.logical_activity_id("implementer", 0, 1, "run"))
    assert key == "ik1:job_01m5p7x7r0bt69j0h0fbxepw90:implementer.c0.n1.run"
    rid = f.run_id("payments", key)
    assert rid == "run_gj3h3ep3phjfs9e23v768zrca5"
    assert f.agentrun_name(rid) == "ar-gj3h3ep3phjfs9e23v768zrca5"


def test_gate_id_vector():
    assert f.gate_id(JOB, "plan_approval", 0, 1) == "gate_r1a2avb51zgbsjy0x4ghyktt2e"


def test_child_workflow_idempotency_key():
    wf = f.child_workflow_id(JOB, "qa_stage", 1)
    assert f.idempotency_key(wf, "qa.c0.n1.run") == f"ik1:{JOB}:qa_stage:1:qa.c0.n1.run"


def test_ulid_monotonic_sorted_and_valid():
    ids = [f.new_id("job") for _ in range(2000)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert all(f.is_valid("job_id", i) for i in ids)
    gen = f.UlidGenerator()
    a, b = gen.body(1_700_000_000_000), gen.body(1_700_000_000_000)
    assert b > a and f.ulid_timestamp_ms(a) == 1_700_000_000_000


def test_alphabet_excludes_ilou():
    assert not set("ilou") & set(f.ALPHABET) and len(f.ALPHABET) == 32


def test_regexes_reject_uppercase_and_bad_tenants():
    assert not f.is_valid("job_id", JOB.upper())
    assert f.is_valid("tenant_id", "payments") and f.is_valid("tenant_id", "local-dev")
    for bad in ("ab", "Payments", "replicant-x", "sbx-a", "kube1", "aws-x", "a" * 33, "pay--ments"):
        assert not f.is_valid("tenant_id", bad), bad
    with pytest.raises(ValueError):
        f.validate("run_id", "run_xyz")


def test_temporal_names():
    assert f.namespace("local") == "replicant-local"
    assert len(f.SEARCH_ATTRIBUTES) == 14
    counts = {}
    for t in f.SEARCH_ATTRIBUTES.values():
        counts[t] = counts.get(t, 0) + 1
    assert counts == {"Keyword": 8, "KeywordList": 2, "Int": 2, "Datetime": 1, "Bool": 1}
    assert "replicant-runner-calls" in f.TASK_QUEUES
    assert f.worker_deployment_version("replicant-worker", "b20261024-3f9a1c2e7d4b") == "replicant-worker:b20261024-3f9a1c2e7d4b"


def test_release_bundle_id_content_addressed():
    a = f.release_bundle_id({"b": 1, "a": [True, None, "x"]})
    b = f.release_bundle_id({"a": [True, None, "x"], "b": 1.0})
    assert a == b and re.match(r"^rb_[0-9a-hjkmnp-tv-z]{26}$", a[0]) and a[1].startswith("sha256:")
    assert f.jcs({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()


def test_system_workflow_id():
    assert f.system_workflow_id("projection-reconciler") == "sys:projection-reconciler"
    assert f.system_workflow_id("projection-rebuild", "01m5") == "sys:projection-rebuild:01m5"


def test_dev_principal():
    # the local stack's seeded identity.users rows (replicant seed) and the principals the dev auth and
    # github-integration attribute to these logins
    assert f.dev_principal("alice") == "usr_gz14a1cx2p2w6aqq5ejag3aq5j" == f.derived("usr", "replicant-dev-user", "alice")
    assert f.dev_principal("grace") == "usr_9d6n2hnc1qn2a3zkmddqp8c3jg"
