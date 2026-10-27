"""Unit tests for the read/control endpoints added for control-plane-web (no database)."""
import datetime as dt
from types import SimpleNamespace

from control_plane_api.app import create_app, iter_routes
from control_plane_api.authz import Principal
from control_plane_api.config import Settings
from control_plane_api.routes_gates import ref_allowed, split_s3_uri
from control_plane_api.routes_web import capabilities, fold_kill_switches, me_doc


def _p(**kw):
    return Principal(principal_id="usr_" + "0" * 26, kind="user", login=kw.pop("login", "grace"), **kw)


def test_me_capabilities_and_tenants():
    grace = _p(groups=("replicant-operators",), platform_roles=("platform_admin", "operator"),
               tenant_roles={"*": ("approver",)})
    caps = capabilities(grace)
    assert {"admin:platform", "temporal:ui", "release:read", "ops:kill-switch"} <= set(caps)
    assert "memory:admin" not in caps
    d = me_doc(grace, ["local-dev", "payments"])
    assert d["display_name"] == "Grace"
    assert d["tenants"] == [{"tenant_id": "local-dev", "roles": ["approver"]},
                            {"tenant_id": "payments", "roles": ["approver"]}]
    carol = _p(login="carol", groups=("replicant-admins",), platform_roles=("operator",))
    assert "memory:admin" in capabilities(carol)          # D-25: group membership, not a role
    alice = _p(login="alice", groups=("payments-engineers",), tenant_roles={"payments": ("engineer",)})
    assert capabilities(alice) == []
    assert me_doc(alice, ["local-dev", "payments"])["tenants"] == [{"tenant_id": "payments", "roles": ["engineer"]}]


def test_fold_kill_switches():
    t = dt.datetime(2026, 10, 25, tzinfo=dt.UTC)
    row = dict(scope="tenant", scope_key="payments", tenant_id="payments", reason_code="drill", note=None,
               notice_id="ntc_" + "1" * 26, issued_by="usr_x", issued_at=t, mirror_generation=3,
               signal_batch_job_id=None, intake_state=None)
    rows = [SimpleNamespace(**row, mode="starts", engaged=True),
            SimpleNamespace(**{**row, "intake_state": "closed"}, mode="intake", engaged=True)]
    (ks,) = fold_kill_switches(rows)
    assert ks["starts"] == "paused" and ks["intake"] == "closed" and ks["runs"] == "open"
    assert ks["scope_value"] == "payments" and ks["mirror_generation"] == 3


def test_packet_ref_scope():
    s = Settings()
    g = SimpleNamespace(tenant_id="payments", job_id="job_x")
    ok = "s3://local-replicant-local-artifacts/tenants/payments/jobs/job_x/runs/r/diff_bundle/changes.tar.zst"
    assert split_s3_uri(ok)[0] == "local-replicant-local-artifacts"
    assert ref_allowed(s, g, ok) is not None
    assert ref_allowed(s, g, ok.replace("job_x", "job_y")) is None          # another job
    assert ref_allowed(s, g, ok.replace("artifacts/tenants", "audit/tenants")) is None
    assert ref_allowed(s, g, "s3://local-replicant-local-artifacts/tenants/payments/jobs/job_x/../job_y/a") is None
    assert ref_allowed(s, g, "https://example.com/x") is None


def test_new_routes_registered_with_permissions():
    app = create_app(Settings(background_tasks=False))      # also runs check_route_permissions
    paths = {(getattr(r, "path", ""), m) for r in iter_routes(list(app.routes)) for m in getattr(r, "methods", ())}
    for p in [("/v1/me", "GET"), ("/v1/tenants", "GET"), ("/v1/jobs/{job_id}/runs", "GET"),
              ("/v1/jobs/{job_id}/artifacts", "GET"), ("/v1/jobs/{job_id}:cancel", "POST"),
              ("/v1/jobs/{job_id}:pause", "POST"), ("/v1/jobs/{job_id}:resume", "POST"),
              ("/v1/jobs/{job_id}/steering", "POST"), ("/v1/releases/bundles", "GET"),
              ("/v1/releases/workers", "GET"), ("/v1/admin/kill-switches", "GET"),
              ("/v1/gates/{gate_id}/packet/refs/{ref_name}", "GET")]:
        assert p in paths, p


# ------------------------------------------------------------------------------------------- WEB-UIPREF (no DB)
def test_ui_preferences_effective_and_if_match():
    import pytest

    from control_plane_api import contracts
    from control_plane_api.problems import ProblemError
    from control_plane_api.routes_ui_prefs import effective, parse_if_match
    assert effective(None) == {"job_view": "simple"}                     # everyone starts on Simple
    assert effective({"job_view": "detailed"}) == {"job_view": "detailed"}
    assert effective({"job_view": "graph", "old_key": 1}) == {"job_view": "simple"}   # invalid/unknown fall back
    assert parse_if_match(None) is None and parse_if_match("") is None
    assert parse_if_match('"3"') == 3 and parse_if_match('W/"4"') == 4 and parse_if_match("0") == 0
    with pytest.raises(ProblemError):
        parse_if_match('"abc"')
    assert not contracts.errors("ui-preferences", {"job_view": "simple", "version": 2})
    assert contracts.errors("ui-preferences", {"job_view": "fancy"})
    assert contracts.errors("ui-preferences", {"job_view": "simple", "theme": "dark"})   # closed schema


def test_ui_preferences_routes_registered():
    app = create_app(Settings(background_tasks=False))
    paths = {(getattr(r, "path", ""), m) for r in iter_routes(list(app.routes)) for m in getattr(r, "methods", ())}
    assert ("/v1/me/ui-preferences", "GET") in paths and ("/v1/me/ui-preferences", "PUT") in paths
