"""Pure unit tests (no Temporal server). Run: python tests/test_units.py  (or pytest tests/test_units.py)."""

from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import jsonschema  # noqa: E402
import yaml  # noqa: E402

from replicant_worker import (  # noqa: E402
    ids,
    nodes,  # noqa: E402,F401
    registry,
)
from replicant_worker.graphs.dsl import FglError, Guard  # noqa: E402
from replicant_worker.graphs.graphspec import (  # noqa: E402
    GraphValidationError,
    compose,
    parse_definition,
    validate,
    validate_composed,
)
from replicant_worker.resources import ORCH as SPEC  # noqa: E402  (vendored spec/01-orchestration)
from replicant_worker.workflows.state import CARRY_BUDGET_BYTES, CarryTooLarge, JobState  # noqa: E402

GRAPHS = sorted((SPEC / "graphs" / "definitions").glob("*.yaml"))
SCHEMAS = SPEC / "schemas"


def _graph(name: str):
    doc = yaml.safe_load((SPEC / "graphs" / "definitions" / f"{name}.yaml").read_text())
    return doc, parse_definition(doc, version=1, digest="sha256:" + "0" * 64)


def test_id_vectors():
    for f, want in ids.TEST_VECTORS:
        assert f() == want


def test_schemas_meta_valid_and_documents_valid():
    for p in SCHEMAS.glob("*.json"):
        jsonschema.Draft202012Validator.check_schema(json.loads(p.read_text()))
    gs = json.loads((SCHEMAS / "graph-definition.v1.json").read_text())
    env = json.loads((SPEC.parent / "00-contracts" / "schemas" / "definition-envelope.schema.json").read_text())
    for p in GRAPHS:
        doc = yaml.safe_load(p.read_text())
        jsonschema.validate(doc, env)
        jsonschema.validate(doc["spec"], gs)
    for pol, sch in (("router-rules", "policy-router-rules"), ("repair-limits", "policy-repair-limits"),
                     ("runner-polling", "policy-runner-polling")):
        doc = yaml.safe_load((SPEC / "policies" / f"{pol}.yaml").read_text())
        jsonschema.validate(doc, env)
        jsonschema.validate(doc["spec"], json.loads((SCHEMAS / f"{sch}.v1.json").read_text()))


def test_all_graphs_satisfy_invariants():
    cat = registry.catalog_entries()
    for p in GRAPHS:
        _, g = _graph(p.stem)
        validate(g, catalog=cat)


def test_modifiers_cannot_weaken_core_roles():
    cat = registry.catalog_entries()
    _, base = _graph("bugfix")
    for mods in ([{"op": "skipWhen", "target": "verify", "when": "True"}],
                 [{"op": "replace", "target": "publish_gate", "with": {"type": "route"}}],
                 [{"op": "replace", "target": "qa", "with": {"type": "route"}}],
                 [{"op": "skipWhen", "target": "risk_diff", "when": "True"}]):
        try:
            validate_composed(base, compose(base, mods, catalog=cat), catalog=cat)
            raise AssertionError(f"accepted {mods}")
        except GraphValidationError:
            pass


def test_requested_gates_only_add():
    cat = registry.catalog_entries()
    _, base = _graph("docs-low-risk")
    g = compose(base, [{"op": "requireGateWhen", "target": "publish_gate", "when": "'docs-review' in request.labels"}],
                catalog=cat)
    validate_composed(base, g, catalog=cat)
    assert "docs-review" in g.nodes["publish_gate"].when


def test_license_modifier_needs_build_with_node():
    """Path (b) before the new build exists: the registry rejects the binding (node type unknown)."""
    from tests_support import LICENSE_MODIFIER
    _, base = _graph("bugfix")
    cat = registry.catalog_entries()
    cat_n = {k: v for k, v in cat.items() if k != "license_policy_scan"}  # build N
    try:
        validate_composed(base, compose(base, [LICENSE_MODIFIER], catalog=cat_n), catalog=cat_n)
        raise AssertionError("build N must reject license_policy_scan")
    except GraphValidationError as e:
        assert any("license_policy_scan" in p for p in e.problems)
    if "license_policy_scan" in cat:  # build N+1
        g = compose(base, [LICENSE_MODIFIER], catalog=cat)
        validate_composed(base, g, catalog=cat)
        assert g.entry == "context" and "license_scan" in g.successors("plan_gate") + g.successors("qa")


def test_fgl():
    ctx = {"risk": {"level": "high"}, "nodes": {"qa": {"decision": "proceed"}}, "cycle": 0}
    assert Guard("risk_at_least(risk.level, 'high') and nodes['qa'].decision == 'proceed'")(ctx)
    assert Guard("nodes.missing.deep.value == None")(ctx)
    for bad in ("__import__('os')", "x if y else z", "risk.__class__", "open('f')", "[a for a in b]"):
        try:
            Guard(bad)
            raise AssertionError(bad)
        except FglError:
            pass


def test_carry_budget_enforced():
    s = JobState(job_id="job_" + "0" * 26, tenant_id="payments", release_bundle_id="rb_" + "0" * 26,
                 worker_build_id="b20261024-000000000001", requester="usr_" + "0" * 26, priority="normal",
                 title="t", targets=[], request=None, request_summary={})
    assert len(json.dumps(s.carry())) < 8_000  # an empty job's carry is small
    s.working_memory = {"blob": "x" * CARRY_BUDGET_BYTES}
    try:
        s.carry()
        raise AssertionError("oversized carry accepted")
    except CarryTooLarge:
        pass


def test_nodecatalog_export_valid():
    os.environ.setdefault("REPLICANT_WORKER_BUILD_ID", "b20261024-3f9a1c2e7d4b")
    from replicant_worker.cli import nodecatalog
    doc = nodecatalog()
    env = json.loads((SPEC.parent / "00-contracts" / "schemas" / "definition-envelope.schema.json").read_text())
    jsonschema.validate(doc, env)
    jsonschema.validate(doc["spec"], json.loads((SCHEMAS / "node-catalog.v1.json").read_text()))


def test_batch_child_activity_ids_unique():
    """R1-011: child indices spread over the cycle field; IDs stay inside the C-IDS grammar and unique."""
    import re

    from replicant_worker.workflows.maintenance import child_activity_id
    pat = re.compile(r"^[a-z][a-z0-9_]{0,40}\.c[0-9]{1,3}\.n[0-9]{1,3}\.[a-z][a-z0-9_]{0,30}$")  # C-RUNNER/C-IDS
    got = [child_activity_id(i) for i in range(5000)]
    assert len(set(got)) == 5000 and all(pat.match(x) for x in got)
    assert child_activity_id(0) == "batch.c0.n1.create_child"
    assert child_activity_id(998) == "batch.c0.n999.create_child" and child_activity_id(999) == "batch.c1.n1.create_child"


def test_terminal_outcomes_match_contract_pattern():
    """R1-009: every terminal outcome satisfies JobSnapshot.terminal.outcome ^[a-z][a-z_]{1,63}$."""
    import re

    from replicant_worker.workflows.state import TERMINAL_STATUS_FOR_OUTCOME
    bad = [o for o in TERMINAL_STATUS_FOR_OUTCOME if not re.match(r"^[a-z][a-z_]{1,63}$", o)]
    assert not bad, bad


def test_codec_test_vectors():
    """XD-FIX-R1-044: codec/testvectors.json decrypts with the stated DEK, nonce and AAD to the inner Payload,
    and re-encrypts byte-for-byte (WS-02's Go encoder consumes the same file)."""
    import base64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from replicant_worker.codec import aesgcm_encrypt_with_dek
    doc = json.loads((SPEC / "codec" / "testvectors.json").read_text())
    assert len(doc["vectors"]) == 3
    for v in doc["vectors"]:
        dek, nonce = bytes.fromhex(v["dek_hex"]), bytes.fromhex(v["nonce_hex"])
        data = base64.b64decode(v["expected"]["data_b64"])
        assert data[:12] == nonce
        inner = AESGCM(dek).decrypt(nonce, data[12:], v["tenant_id"].encode())
        assert inner == base64.b64decode(v["inner_payload_b64"])
        assert aesgcm_encrypt_with_dek(dek, nonce, v["tenant_id"], inner) == data


def test_steering_backlog_input_matches_ws04():
    """XD-W2-04-08: the workflow's steering.stage_backlog input validates against WS-04's schema, messages[] in
    message_ids[] order, and WS-04's result pattern accepts the stub's keys."""
    import asyncio

    from support.stubs import FakeWorld

    from replicant_worker.workflows.replicant_job import steering_backlog_input
    from replicant_worker.workflows.state import JobState
    s = JobState(job_id="job_" + "a" * 26, tenant_id="payments", release_bundle_id="rb_" + "b" * 26, worker_build_id="b",
                 requester="usr_" + "c" * 26, priority="normal", title="t", targets=[], request=None, request_summary={})
    s.segment, s.resume_at = 2, "implement"
    s.steering_queue = [{"message_id": f"msg_{str(i) * 26}", "text": f"hint {i}", "author": "usr_" + "c" * 26,
                         "sent_at": "2026-10-24T10:00:00Z"} for i in range(1, 7)]
    arg = steering_backlog_input(s)
    sch = json.loads((SPEC.parent / "04-memory-and-context" / "schemas" / "steering-stage-backlog.schema.json").read_text())
    jsonschema.Draft202012Validator(sch).validate(arg)
    assert [m["message_id"] for m in arg["messages"]] == arg["message_ids"]
    w = FakeWorld()
    fn = next(f for f in w.activities(None)["replicant-jobs"] if f.__name__ == "steering_stage_backlog")
    keys = asyncio.run(fn(arg))["keys"]
    jsonschema.validate({"keys": keys}, {**sch["$defs"]["result"], "$defs": sch["$defs"]})
    bad = {**arg, "messages": list(reversed(arg["messages"]))}
    try:
        asyncio.run(fn(bad))
        raise AssertionError("mismatch must fail")
    except Exception as e:  # noqa: BLE001
        assert getattr(e, "type", "") == "steering_backlog_mismatch", e


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL", name, repr(e)[:300])
    sys.exit(failed)
