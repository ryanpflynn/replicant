"""Unit tests (no database, no Temporal)."""
import json

import pytest

from control_plane_api import contracts, registry
from control_plane_api.auth import parse_token_map
from control_plane_api.authz import AuthorizeRequest, Evaluator, Principal, PrincipalLookup
from control_plane_api.grants import load_jwks
from control_plane_api.ids import dev_user_principal, derived, release_bundle_id
from control_plane_api.routes_gates import _precheck
from control_plane_api.problems import ProblemError


def test_cids_vectors_and_dev_principal():
    assert derived("gate", "job_01m5p7x7r0bt69j0h0fbxepw90", "plan_approval", "c0", "n1") == \
        "gate_r1a2avb51zgbsjy0x4ghyktt2e"
    p = dev_user_principal("grace")
    assert p.startswith("usr_") and len(p) == 30 and p == dev_user_principal("grace") != dev_user_principal("alice")


def test_token_map_formats():
    assert parse_token_map("tok1 svc_a\n# c\ntok2 svc_b\n") == {"tok1": "svc_a", "tok2": "svc_b"}
    assert parse_token_map('"tokA": svc_replicant-gateway-shim\ntokB: svc_github-integration\n') == \
        {"tokA": "svc_replicant-gateway-shim", "tokB": "svc_github-integration"}


def _dir():
    d = PrincipalLookup()
    d.add(Principal("usr_a", "user", "alice", tenant_roles={"payments": ("engineer",)}))
    d.add(Principal("usr_g", "user", "grace", platform_roles=("platform_admin", "operator"),
                    tenant_roles={"*": ("approver",)}))
    d.add(Principal("svc_x", "service", "x", permissions=frozenset({"service:x"})))
    return d


def test_evaluator_roles():
    ev = Evaluator(_dir())
    assert ev.authorize(AuthorizeRequest("usr_a", "job:create", "payments")).allowed
    assert not ev.authorize(AuthorizeRequest("usr_a", "job:create", "other")).allowed
    assert not ev.authorize(AuthorizeRequest("usr_a", "gate:decide", "payments")).allowed
    assert ev.authorize(AuthorizeRequest("usr_g", "gate:decide", "anything")).allowed
    assert ev.authorize(AuthorizeRequest("usr_g", "admin:platform")).allowed
    assert not ev.authorize(AuthorizeRequest("usr_a", "admin:platform")).allowed
    assert ev.authorize(AuthorizeRequest("svc_x", "service:x")).allowed
    assert ev.authorize(AuthorizeRequest("nobody", "job:read", "payments")).reason == "principal_deprovisioned"
    assert _dir().get("usr_a").tenants_with("job:read") == {"payments"}
    assert _dir().get("usr_g").tenants_with("job:read") is None


def test_job_request_example_validates():
    ex = contracts.schema("job-request")["examples"][0]
    assert contracts.errors("job-request", ex) == []
    bad = dict(ex, priority="asap")
    assert contracts.errors("job-request", bad)


def test_dev_bundle_is_schema_valid_and_content_addressed():
    spec = registry.default_dev_spec("b20261024-000000000000", "sha256:" + "0" * 64)
    doc = registry.bundle_document(spec, "svc_dev-seeder")
    rb1, d1 = release_bundle_id(doc["spec"])
    rb2, d2 = release_bundle_id(registry.default_dev_spec("b20261024-000000000000", "sha256:" + "0" * 64))
    assert rb1 == rb2 and d1 == d2 and rb1.startswith("rb_")
    rb3, _ = release_bundle_id(registry.default_dev_spec("b20261024-111111111111", "sha256:" + "0" * 64))
    assert rb3 != rb1
    with pytest.raises(registry.BundleInvalid):
        registry.bundle_document({"worker_deployment": {}}, "svc_dev-seeder")


def test_ramp_bucket():
    assert 0 <= registry.bucket("job_01m5p7x7r0bt69j0h0fbxepw90") < 10000
    r = {"tenants_excluded": [], "tenants_included": ["early"], "percent": "0", "bucket_lo": 0}
    assert registry._in_ramp(r, "early", "job_x")
    assert not registry._in_ramp(dict(r, tenants_excluded=["early"]), "early", "job_x")
    full = {"tenants_excluded": [], "tenants_included": [], "percent": "100.00", "bucket_lo": 0}
    assert registry._in_ramp(full, "t", "job_01m5p7x7r0bt69j0h0fbxepw90")


def test_gate_decide_request_schema():
    req = {"schema": "replicant.msg.gate-decide/v1", "decision_id": derived("dec", "ui", "01m5p7x7r0bt69j0h0fbxepw90"),
           "gate_id": "gate_r1a2avb51zgbsjy0x4ghyktt2e", "decision": "approve", "actor": {"principal_id": "usr_" + "a" * 26},
           "channel": "ui", "presented_digest": "sha256:" + "a" * 64, "decided_at": "2026-10-24T08:00:00Z"}
    assert contracts.errors("workflow-messages", req, "GateDecideRequest") == []
    assert contracts.errors("workflow-messages", dict(req, decision="answer"), "GateDecideRequest")  # needs answer_text


class G:
    state = "open"
    kind = "plan_approval"
    presented_digest = "sha256:" + "a" * 64
    channels = ["ui", "cli"]
    requester_may_decide = False
    requester = "usr_" + "r" * 26
    winning_decision = None


def test_precheck_rules():
    approver = Principal("usr_" + "b" * 26, "user")
    _precheck(approver, G, "approve", G.presented_digest, "ui")
    cases = [
        (approver, "approve", "sha256:" + "b" * 64, "ui", "stale_approval_view"),
        (approver, "approve", G.presented_digest, "slack", "channel_not_allowed"),
        (approver, "rerun_qa", G.presented_digest, "ui", "decision_not_offered"),
        (approver, "answer", G.presented_digest, "ui", "decision_not_allowed"),
        (Principal(G.requester, "user"), "approve", G.presented_digest, "ui", "self_approval_forbidden"),
        (Principal("svc_x", "service"), "approve", G.presented_digest, "ui", "actor_not_human"),
    ]
    for p, dec, dig, ch, code in cases:
        with pytest.raises(ProblemError) as e:
            _precheck(p, G, dec, dig, ch)
        assert e.value.code == code


def test_jwks_strips_private_members(tmp_path):
    from control_plane_api.config import Settings
    f = tmp_path / "jwks.json"
    f.write_text(json.dumps({"keys": [{"kty": "EC", "crv": "P-256", "kid": "rg-local-1", "x": "x", "y": "y", "d": "SECRET"}]}))
    out = load_jwks(Settings(run_grant_jwks_file=str(f)))
    assert out == {"keys": [{"kty": "EC", "crv": "P-256", "kid": "rg-local-1", "x": "x", "y": "y"}]}
