"""ONBOARD-01 §2-§3, §10: environment inference, the fleet classifier and the impact score (pure functions)."""
from __future__ import annotations

import pytest

from control_plane_api import inventory as I

CI = ".github/workflows/ci.yml#jobs.test"


def prof(ecos=(), ci=(), files=(), mcp=(), hooks=()):
    return {"ecosystems": list(ecos), "ci_commands": list(ci), "files": [{"path": p} for p in files],
            "mcp_servers": list(mcp), "claude": {"hooks": list(hooks)}}


def eco(e, v=None, pm=None, src="go.mod", **kw):
    return {"ecosystem": e, "version": v, "package_manager": pm, "source": src, **kw}


def cmd(stage, c, src=CI):
    return {"stage": stage, "command": c, "source": src}


def test_python_uv_from_ci_wins():
    env = I.infer_environment(prof(
        ecos=[eco("python", ">=3.11", "uv", "pyproject.toml"), eco("python", "3.13", "uv", ".python-version"),
              eco("python", "3.12", "uv", CI), eco("node", None, "npm", "package.json", scripts=["lint"])],
        ci=[cmd("install", "uv sync --frozen"), cmd("test", "uv run pytest -q"), cmd("lint", "uv run ruff check ."),
            cmd("lint", "npx prettier --check .", ".github/workflows/web.yml#jobs.fmt")],
        files=["pyproject.toml", "uv.lock", "package.json"]))
    assert env["environment"] == "python-service" and env["version"] == "3.12" and env["supported"]
    assert env["commands"] == {"install": "uv sync --frozen", "build": None, "test": "uv run pytest -q",
                               "lint": "uv run ruff check ."}
    assert env["source"] == {"install": CI, "test": CI, "lint": CI}


def test_python_defaults_and_version_files():
    env = I.infer_environment(prof(ecos=[eco("python", ">=3.12", "uv", "pyproject.toml"),
                                         eco("python", "3.13.1", "uv", ".python-version")]))
    assert env["version"] == "3.13" and env["commands"] == {"install": "uv sync --frozen", "build": None,
                                                            "test": "uv run pytest -q", "lint": None}
    assert env["source"] == {"install": "default:uv", "test": "default:uv"}
    env = I.infer_environment(prof(ecos=[eco("python", None, "pip", "requirements.txt")], files=["requirements.txt"]))
    assert env["commands"]["install"] == "pip install -r requirements.txt"
    assert env["commands"]["test"] == "python -m pytest -q" and env["version"] == "3.12"


def test_devcontainer_outranks_version_files():
    env = I.infer_environment(prof(ecos=[eco("python", "3.12", "uv", ".python-version"),
                                         eco("python", "3.13", None, ".devcontainer/devcontainer.json")]))
    assert env["version"] == "3.13"


def test_go_defaults_and_newer_go_unsupported():
    env = I.infer_environment(prof(ecos=[eco("go", "1.22.3", "go")]))
    assert env["environment"] == "go-service" and env["version"] == "1.22" and env["supported"]
    assert env["commands"] == {"install": "go mod download", "build": "go build ./...",
                               "test": "go test -count=1 ./...", "lint": None}
    env = I.infer_environment(prof(ecos=[eco("go", "1.30", "go")]))
    assert not env["supported"] and "newer than the toolchain" in env["unsupported_reason"]


def test_node_pm_scripts_and_engines():
    env = I.infer_environment(prof(ecos=[eco("node", ">=20", "pnpm", "package.json", scripts=["build", "test"]),
                                         eco("node", "22.11.0", "pnpm", ".nvmrc")]))
    assert env["environment"] == "node-service" and env["version"] == "22"
    assert env["commands"] == {"install": "pnpm install --frozen-lockfile", "build": "pnpm run build",
                               "test": "pnpm test", "lint": None}
    env = I.infer_environment(prof(ecos=[eco("node", None, "npm", "package.json", scripts=["lint"])]))
    assert env["commands"] == {"install": "npm ci", "build": None, "test": None, "lint": "npm run lint"}
    assert not I.infer_environment(prof(ecos=[eco("node", "16", "npm", ".nvmrc")]))["supported"]


def test_ci_test_command_decides_the_ecosystem():
    env = I.infer_environment(prof(ecos=[eco("go", "1.26", "go"), eco("node", None, "npm", "package.json")],
                                   ci=[cmd("test", "cd web && npm test"), cmd("build", "go build ./...")]))
    assert env["environment"] == "node-service" and env["commands"]["test"] == "cd web && npm test"
    assert env["commands"]["build"] is None or "go build" not in (env["commands"]["build"] or "")


def test_unsupported_python_and_pm_and_nothing():
    assert I.infer_environment(prof(ecos=[eco("python", "3.9", "uv", ".python-version")]))["unsupported_reason"] \
        .startswith("python 3.9")
    env = I.infer_environment(prof(ecos=[eco("python", "3.12", "poetry", "pyproject.toml")]))
    assert not env["supported"] and "poetry" in env["unsupported_reason"]
    env = I.infer_environment(prof())
    assert env["environment"] is None and not env["supported"]


@pytest.mark.parametrize("v,spec,ok", [((3, 12), ">=3.11", True), ((3, 13), ">=3.11,<3.13", False),
                                       ((3, 12), "~=3.12", True), ((3, 13), "==3.12.*", False),
                                       ((3, 12), "^3.11", True), ((22, 0), ">= 20", True), ((3, 12), "3.12", True),
                                       ((3, 12), "garbage", False)])
def test_satisfies(v, spec, ok):
    assert I._satisfies(v, spec) is ok


def mcp(**kw):
    s = {"name": "x", "transport": "stdio", "command": "npx", "args": [], "url": None, "env_keys": [], "header_keys": []}
    s.update(kw)
    return s


def test_mcp_classes():
    assert I.classify_mcp(mcp(identity="stdio:npx:@acme/schema-mcp@1.2.0"))[0] == "auto_allow"
    assert I.classify_mcp(mcp(command="uvx", identity="stdio:uvx:pg-mcp==0.4.1", env_keys=["LOG_LEVEL"]))[0] == \
        "auto_allow"
    assert I.classify_mcp(mcp(identity="stdio:npx:@acme/schema-mcp@^1.2.0"))[0] == "review"
    assert I.classify_mcp(mcp(identity="stdio:npx:@acme/schema-mcp"))[0] == "review"
    assert I.classify_mcp(mcp(command="python", identity="stdio:python:tools.mcp"))[0] == "review"
    assert I.classify_mcp(mcp(identity="stdio:npx:@modelcontextprotocol/server-memory@1.0.0"))[0] == "map"
    assert I.classify_mcp(mcp(identity="stdio:npx:x@1.0.0", env_keys=["GITHUB_TOKEN"]))[0] == "needs_credentials"
    assert I.classify_mcp(mcp(transport="http", url="https://jira.acme.internal", identity="url:https://jira.acme.internal",
                              header_keys=["Authorization"]))[0] == "needs_credentials"
    assert I.classify_mcp(mcp(transport="http", url="https://docs.acme.internal",
                              identity="url:https://docs.acme.internal"))[0] == "needs_egress"
    assert I.classify_mcp(mcp(identity="stdio:npx:x@1.0.0", env_keys=["API_BASE_URL"]))[0] == "needs_egress"


def test_hook_classes():
    h = {"event": "PostToolUse", "command_hash": "sha256:ab"}
    assert I.classify_hook({**h, "command": "uv run ruff format $FILE"}, "python-service")[0] == "ok"
    assert I.classify_hook({**h, "command": "jq -r .tool_input.file_path | xargs uv run ruff format"},
                           "python-service")[0] == "ok"
    assert I.classify_hook({**h, "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/fmt.sh"}, "go-service")[0] == "ok"
    assert I.classify_hook({**h, "command": "curl -s https://hooks.acme.internal/x"}, "go-service")[0] == "at_risk"
    cls, why = I.classify_hook({**h, "command": "ruff format ."}, "python-service")
    assert cls == "at_risk" and "ruff" in why
    assert I.classify_hook({**h, "command": "npx prettier --write ."}, "python-service")[0] == "at_risk"
    assert I.classify_hook({**h, "command": "npx prettier --write ."}, "node-service")[0] == "ok"
    assert I.classify_hook({**h, "command": "FOO=1 gofmt -w ."}, "go-service")[0] == "ok"
    assert I.hook_identity(h) == "hook:PostToolUse:sha256:ab"


def test_catalog_items_of_a_profile():
    p = prof(ecos=[eco("python", "3.12", "uv", ".python-version")],
             mcp=[mcp(name="schema", identity="stdio:npx:@acme/schema-mcp@1.2.0"),
                  mcp(name="jira", transport="http", url="https://jira", identity="url:https://jira",
                      header_keys=["Authorization"])],
             hooks=[{"event": "Stop", "command": "make check", "command_hash": "sha256:1"}])
    env = I.infer_environment(p)
    items = {(i["kind"], i["identity"]): i for i in I.catalog_items(p, env)}
    assert items[("mcp_server", "stdio:npx:@acme/schema-mcp@1.2.0")]["default_action"] == "allow"
    assert items[("mcp_server", "url:https://jira")]["class"] == "needs_credentials"
    assert items[("hook", "hook:Stop:sha256:1")]["class"] == "ok"
    assert items[("toolchain", "toolchain:python:3.12:uv")]["class"] == "supported"
    bad = I.infer_environment(prof(ecos=[eco("python", "3.9", "uv", ".python-version")]))
    assert I.toolchain_item(bad)["identity"] == "toolchain:python:3.9:uv" and I.toolchain_item(bad)["class"] == \
        "unsupported"
    assert I.probe_item("node-service", "missing_tool", "libvips")["identity"] == "probe:node-service:missing_tool:libvips"
    assert I.probe_item(None, "weird", "a b")["identity"] == "probe:none:build_failure:a_b"
    assert I.probe_item("python-service", "lint_failure", "ruff")["class"] == "lint_failure"


def test_effective_action():
    assert I.effective_action("skip", None) == "skip" and I.effective_action("skip", "approve") == "allow"
    assert I.effective_action("allow", "deny") == "skip" and I.effective_action("skip", "map", "docs") == "map:docs"


def test_env_digest_tracks_commands_only():
    a = {"environment": "python-service", "version": "3.12", "commands": {"test": "uv run pytest -q"}, "source": {"x": 1}}
    assert I.env_digest(a) == I.env_digest({**a, "source": {}})
    assert I.env_digest(a) != I.env_digest({**a, "commands": {"test": "pytest"}})


def test_impact_ratios_and_score():
    agg = {"jobs_with": 10, "jobs_without": 30, "w_failed": 5, "wo_failed": 3, "w_rejected": 1, "wo_rejected": 3,
           "w_repair_cycles": 10, "wo_repair_cycles": 15, "w_qa_jobs": 10, "w_qa_first_pass": 3, "wo_qa_jobs": 30,
           "wo_qa_first_pass": 24}
    out = I.impact(agg)
    d = out["delta"]
    assert out["jobs_with"] == 10 and out["jobs_without"] == 30
    assert d["failed"] > 1 and d["qa_first_pass"] < 1 and d["repair_cycles"] > 1 and 0.9 < d["rejected"] < 1.3
    assert out["score"] > 0
    same = I.impact({"jobs_with": 10, "jobs_without": 10, "w_failed": 2, "wo_failed": 2, "w_qa_jobs": 10,
                     "wo_qa_jobs": 10, "w_qa_first_pass": 5, "wo_qa_first_pass": 5})
    assert same["delta"]["failed"] == 1 and same["score"] == 0
    none = I.impact({"jobs_with": 4, "jobs_without": 0, "w_failed": 2})
    assert none["delta"] == {"qa_first_pass": None, "rejected": None, "repair_cycles": None, "failed": None}
    assert none["score"] == 2.0
    assert I.impact({}, "probe_failure", repos=3)["score"] == 3


# ------------------------------------------------------------------------------------------------ project config
LEDGER_DIGESTS = [  # test_unit.LEGACY_DIGESTS: the local stack's prj_ledger configs must keep their derived bundle
    ({"gates": {"plan": "always"}}, "sha256:0e9e7a07a64ba719f81d2daf6eb05e8c95121afb277c0ed5359aa4219955c91f"),
    ({"risk": {"large_change_files": 0}, "workflows": {"bugfix": {"params": {"max_review_rounds": 1}}}},
     "sha256:2a2dce0189cf2561bca903564d10688fcdb77a38de72f71dbaad24ce16c4fa4c"),
    ({}, "sha256:603c4c504ad5e9d73630e826106301dfa2a4f96b3fca1a977a1641f1d88d68bc"),
]


@pytest.mark.parametrize("cfg,want", LEDGER_DIGESTS)
def test_derived_digest_stable_with_new_sections(cfg, want):
    from control_plane_api import projects as P
    from control_plane_api.derived import config_digest
    for extra in ({}, {"repo_config": "ignore"}, {"triggers": {"readiness_override": True}},
                  {"models": {"default_tier": "standard", "workflows": {}}}):
        eff = P.merge({**cfg, **extra})
        assert config_digest("rb_232k9k8qj2km565gcrapktkgkf", P.pinned_part(eff)) == want, extra
    tiered = P.merge({**cfg, "models": {"workflows": {"bugfix": "thorough"}}})
    assert config_digest("rb_232k9k8qj2km565gcrapktkgkf", P.pinned_part(tiered)) != want
    assert not P.is_default_pinned(tiered)


def test_new_sections_merge_validate_and_label():
    from control_plane_api import projects as P
    assert P.DEFAULTS["repo_config"] == "use" and P.DEFAULTS["triggers"]["readiness_override"] is False
    assert P.schema_errors({"repo_config": "ignore", "models": {"default_tier": "thorough",
                                                                "workflows": {"bugfix": "standard"}},
                            "triggers": {"readiness_override": True}}) == []
    assert P.schema_errors({"repo_config": "maybe"}) and P.schema_errors({"models": {"workflows": {"nope": "x"}}})
    assert P.schema_errors({"models": {"tier": "x"}})
    eff = P.merge({"repo_config": "ignore", "models": {"workflows": {"bugfix": "thorough"}}},
                  {"models": {"workflows": {"docs": "standard"}}})
    assert eff["repo_config"] == "ignore"
    assert eff["models"] == {"default_tier": "standard", "workflows": {"bugfix": "thorough", "docs": "standard"}}
    assert P.is_default_pinned(P.merge({"repo_config": "ignore", "triggers": {"readiness_override": True}}))
    ch = {c["path"]: c["applies"] for c in P.changes(P.merge({}), P.merge(
        {"repo_config": "ignore", "models": {"default_tier": "thorough"}, "triggers": {"readiness_override": True}}))}
    assert ch == {"repo_config": "new_jobs", "models.default_tier": "new_jobs", "triggers.readiness_override": "live"}


def test_model_tier_policy():
    from control_plane_api import derived
    spec = derived.model_tier_selection_spec({"default_tier": "standard", "workflows": {"bugfix": "thorough"}})
    assert spec == {"policyType": "model_tier_selection", "default_tier": "standard",
                    "workflows": {"bugfix": "thorough"},
                    "escalation": {"repair_cycle_at_least": 2, "risk_at_least": "high", "after_failed_attempt": True}}
    tiers = {"policyType": "model_tiers", "tiers": {"standard": {}, "thorough": {}}}
    assert derived.model_tier_errors({"default_tier": "standard", "workflows": {"bugfix": "thorough"}}, tiers) == []
    errs = derived.model_tier_errors({"default_tier": "turbo", "workflows": {"docs": "cheap"}}, tiers)
    assert [e["path"] for e in errs] == ["/models/default_tier", "/models/workflows/docs"]
    assert derived.model_tier_errors({"default_tier": "turbo"}, None) == []    # no catalogue in the base bundle


def test_readiness_request_validates_with_repo_profile():
    from control_plane_api import contracts
    from control_plane_api.routes_inventory import readiness_request
    repo = {"full_name": "acme-payments/billing-py", "host": "git.replicant.localhost:3300", "provider": "gitea"}
    auto = readiness_request("payments", repo, "12", "main", "a" * 40, "sha256:" + "b" * 64)
    assert contracts.errors("job-request", auto) == []
    assert auto["requester"] == {"kind": "service", "principal_id": "svc_replicant-readiness",
                                 "on_behalf_of": "svc_replicant-readiness"}
    assert auto["constraints"]["labels"] == ["replicant-readiness"] and auto["workflow_type"] == "qa_only"
    assert auto["targets"][0]["base_sha"] == "a" * 40
    again = readiness_request("payments", repo, "12", "main", "a" * 40, "sha256:" + "b" * 64)
    assert again["dedupe_key"] == auto["dedupe_key"]                    # retries of an automatic probe dedupe
    user = readiness_request("payments", repo, "12", "main", "a" * 40, "sha256:" + "b" * 64, "usr_" + "a" * 26)
    assert user["requester"]["kind"] == "user" and user["dedupe_key"] != auto["dedupe_key"]
    # a fleet admin's re-check runs as the probe principal but must still start a new probe
    admin = readiness_request("payments", repo, "12", "main", "a" * 40, "sha256:" + "b" * 64, fresh=True)
    assert admin["requester"]["kind"] == "service" and admin["dedupe_key"] != auto["dedupe_key"]
    user["repo_profile"] = {"profile_id": "rp_" + "c" * 26, "sha": "a" * 40, "environment": "python-service",
                            "commands": {"install": "uv sync --frozen", "build": None, "test": "uv run pytest -q",
                                         "lint": None},
                            "repo_config": "use", "config_base_sha": "a" * 40,
                            "mcp_auto_allow": [{"name": "schema", "command": "npx",
                                                "args": ["-y", "@acme/schema-mcp@1.2.0"]}],
                            "readiness": "ready"}
    assert contracts.errors("job-request", user) == []
    user["repo_profile"]["environment"] = "rust-service"
    assert contracts.errors("job-request", user)


def test_profile_body_checks():
    from control_plane_api.routes_inventory import _check_profile
    ok = {"repo_id": "12", "full_name": "a/b", "sha": "a" * 40, "files": [], "claude": {}}
    assert _check_profile("12", ok) == []
    assert _check_profile("13", ok) == ["repo_id must match the path"]
    assert _check_profile("12", {**ok, "sha": "main"}) == ["sha must be a commit id"]
    assert _check_profile("12", {**ok, "files": "x"}) == ["files must be a list"]


@pytest.fixture
def admin_client(monkeypatch):  # noqa: ANN001, ANN201
    import contextlib

    from fastapi.testclient import TestClient

    from control_plane_api import routes_inventory
    from control_plane_api.app import create_app
    from control_plane_api.config import Settings
    app = create_app(Settings(background_tasks=False))
    audited: list[dict] = []

    @contextlib.asynccontextmanager
    async def tx(**kw):  # noqa: ANN003, ANN202
        yield None

    async def emit(conn, db, ev):  # noqa: ANN001, ANN202
        audited.append(ev)
        return "aud_" + "1" * 26

    async def refresh(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    async def has_relation(name):  # noqa: ANN001, ANN202
        return False
    monkeypatch.setattr(app.state.db, "tx", tx)
    monkeypatch.setattr(app.state.db, "has_relation", has_relation)
    monkeypatch.setattr(app.state.auth.rbac, "refresh", refresh)
    monkeypatch.setattr(routes_inventory.audit, "emit", emit)
    return TestClient(app), audited


def test_fleet_admin_gate(admin_client):  # noqa: ANN001
    c, audited = admin_client
    r = c.get("/v1/admin/fleet/items", headers={"X-Replicant-Dev-User": "erin"})
    assert r.status_code == 403 and r.json()["code"] == "not_fleet_admin" and r.json()["audit_id"].startswith("aud_")
    assert audited[-1]["event_type"] == "inventory.admin_denied" and audited[-1]["outcome"] == "denied"
    r = c.post("/v1/admin/fleet/items/inv_" + "a" * 26 + ":decide", headers={"X-Replicant-Dev-User": "erin"},
               json={"decision": "approve", "reason": "x"})
    assert r.status_code == 403
    r = c.get("/v1/admin/fleet/items?kind=hook&sort=impact", headers={"X-Replicant-Dev-User": "carol"})
    assert r.status_code == 200 and r.json() == {"items": [], "next": None}
    assert c.get("/v1/admin/fleet/items?kind=nope", headers={"X-Replicant-Dev-User": "carol"}).status_code == 422
    r = c.post("/v1/admin/fleet/items/inv_" + "a" * 26 + ":approve", headers={"X-Replicant-Dev-User": "carol"},
               json={"decision": "approve", "reason": "x"})
    assert r.status_code == 404


def test_fleet_admin_capability():
    from control_plane_api.authz import Principal
    from control_plane_api.routes_web import capabilities
    admin = Principal("usr_" + "a" * 26, "user", "carol", groups=("replicant-admins",))
    assert {"memory:admin", "fleet:admin"} <= set(capabilities(admin))
    assert "fleet:admin" not in capabilities(Principal("usr_" + "b" * 26, "user", "erin", groups=("x",)))


def test_internal_routes_need_their_service(admin_client):  # noqa: ANN001
    c, _ = admin_client
    assert c.put("/internal/v1/repos/1/profile", json={}, headers={"X-Replicant-Dev-User": "carol"}).status_code == 403
    assert c.post("/internal/v1/inventory/observations", json={}).status_code == 401
