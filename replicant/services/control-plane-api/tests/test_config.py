"""CONFIG unit tests (no database): role resolver and project scoping, config merge/changes/schema, derived-bundle
compilation (gate rules, graphs, validation, digest reuse)."""
import time

from control_plane_api import derived, projects as P
from control_plane_api.authz import AuthorizeRequest, Evaluator, Principal, PrincipalLookup
from control_plane_api.rbac import Binding, RoleResolver, Snapshot, project_permissions


def _resolver() -> RoleResolver:
    r = RoleResolver(None)
    r.snap = Snapshot(loaded=True, version=3, loaded_at=time.monotonic(), bindings=[
        Binding("payments-engineers", "tenant", "engineer", "payments"),
        Binding("payments-viewers", "tenant", "viewer", "payments"),
        Binding("ledger-admins", "project", "project_admin", "payments", "prj_ledger"),
        Binding("ops", "platform", "approver"),
    ], groups_of={"usr_a": frozenset({"payments-engineers"}), "usr_d": frozenset({"payments-viewers"}),
                  "usr_f": frozenset({"ledger-admins"}), "usr_g": frozenset({"ops"})},
        project_of_repo={"1": "prj_ledger", "2": "prj_web"},
        tenant_of_project={"prj_ledger": "payments", "prj_web": "payments"})
    return r


def _ev() -> Evaluator:
    d = PrincipalLookup()
    for pid, login in (("usr_a", "alice"), ("usr_d", "dave"), ("usr_f", "frank"), ("usr_g", "grace"),
                       ("usr_x", "carol")):
        # static roles must be replaced by the DB bindings (dave is statically an engineer here)
        d.add(Principal(pid, "user", login, tenant_roles={"payments": ("engineer",)} if login == "dave" else {}))
    return Evaluator(d, _resolver())


def allowed(ev: Evaluator, pid: str, action: str, tenant: str = "payments", **kw) -> bool:
    return ev.authorize(AuthorizeRequest(pid, action, tenant, **kw)).allowed


def test_db_roles_replace_static_roles_and_project_scope():
    ev = _ev()
    assert allowed(ev, "usr_a", "job:create", repo_ids=("1",))
    assert not allowed(ev, "usr_d", "job:create", repo_ids=("1",))           # DB viewer, static engineer ignored
    assert allowed(ev, "usr_d", "job:read")
    # project_admin: only its project, never the tenant or another project
    assert allowed(ev, "usr_f", "tenant:policy:edit", project_id="prj_ledger")
    assert allowed(ev, "usr_f", "job:create", repo_ids=("1",))
    assert not allowed(ev, "usr_f", "tenant:policy:edit", project_id="prj_web")
    assert not allowed(ev, "usr_f", "job:create", repo_ids=("2",))
    assert not allowed(ev, "usr_f", "job:create", repo_ids=("1", "2"))
    assert not allowed(ev, "usr_f", "tenant:policy:edit")                      # tenant scope
    assert not allowed(ev, "usr_f", "job:create", tenant="other", repo_ids=("1",))
    # platform-scope binding of a tenant role applies to every tenant
    assert allowed(ev, "usr_g", "gate:decide", tenant="anything")
    assert not allowed(ev, "usr_x", "job:create", repo_ids=("1",))              # linked, no binding
    f = _resolver().effective(Principal("usr_f", "user", "frank"))
    assert f.tenants_with("job:read") == {"payments"} and f.roles_source == "db"
    assert "tenant:policy:edit" in project_permissions(f, "prj_ledger", "payments")
    assert "tenant:policy:edit" not in project_permissions(f, "prj_web", "payments")


def test_static_roles_until_bindings_exist():
    r = RoleResolver(None)
    p = Principal("usr_a", "user", "alice", tenant_roles={"payments": ("engineer",)})
    assert r.effective(p) is p


def test_merge_changes_and_permissions():
    base = {"triggers": {"label": "replicant"}}
    new = {"triggers": {"label": "go"}, "gates": {"plan": "always"},
           "workflows": {"bugfix": {"params": {"max_review_rounds": 1}}}}
    a, b = P.merge(base), P.merge(new)
    assert a["triggers"]["modes"] == P.DEFAULTS["triggers"]["modes"] and P.is_default_pinned(a)
    assert not P.is_default_pinned(b)
    ch = {c["path"]: c["applies"] for c in P.changes(a, b)}
    assert ch == {"triggers.label": "live", "gates.plan": "new_jobs", "workflows.bugfix.params": "new_jobs"}
    assert P.permissions_needed(P.changes(a, b)) == {"tenant:ops:edit", "tenant:policy:edit"}
    # repo override on top of the project
    eff = P.merge({"risk": {"large_change_files": 5}}, {"risk": {"sensitive_paths": ["db/**"]}})
    assert eff["risk"] == {"large_change_files": 5, "sensitive_paths": ["db/**"], "forbidden_paths": []}
    assert not P.schema_errors(new)
    assert P.schema_errors({"gates": {"publish": "off"}})                      # the publish gate cannot be off
    assert P.schema_errors({"workflows": {"nope": {}}})


def test_gate_rules_compile():
    base = {"gates": {"plan_approval": {"enabled": False, "always_for": [], "risk_at_least": "critical",
                                        "channels": ["ui"], "requester_may_decide": False},
                      "publish_approval": {"always_for": ["bugfix"], "risk_at_least": "high",
                                           "channels": ["ui", "cli", "slack"], "requester_may_decide": True}}}
    g = derived.compile_gate_rules(base, {"plan": "always", "publish": "always", "requester_may_decide": False})
    plan, pub = g["gates"]["plan_approval"], g["gates"]["publish_approval"]
    assert plan["enabled"] and "bugfix" in plan["always_for"] and plan["channels"] == ["ui", "cli", "slack"]
    assert pub["always_for"] == derived.PUBLISH_ALWAYS_FOR and not pub["requester_may_decide"]
    g = derived.compile_gate_rules(base, {"plan": "risk_medium", "publish": "risk_medium"})
    assert g["gates"]["plan_approval"]["risk_at_least"] == "medium" and g["gates"]["publish_approval"]["always_for"] == []
    assert base["gates"]["plan_approval"]["enabled"] is False                    # input untouched


GRAPH = {"apiVersion": "replicant.example.com/v1", "kind": "GraphDefinition", "metadata": {"name": "bugfix"},
         "spec": {"workflowType": "bugfix", "params": {"max_review_rounds": 2}, "entry": "a", "nodes": [
             {"key": "a", "type": "review_run", "maxVisits": 3, "config": {"agent": "x"}},
             {"key": "g", "type": "human_gate", "config": {"gateKind": "publish_approval"}},
             {"key": "p", "type": "publisher"}],
             "edges": [{"from": "a", "to": "g"}, {"from": "g", "switch": [{"when": "x", "to": "p"}],
                                                  "default": "end:rejected"}, {"from": "p", "to": "end:done"}]}}
CATALOG = {"review_run": {"type": "object", "required": ["agent"]}, "human_gate": {}, "publisher": {}}


def test_graph_tune_and_validate():
    doc = derived.tune_graph(GRAPH, {"params": {"max_review_rounds": 1}, "max_visits": {"p": 2}})
    assert doc["spec"]["params"]["max_review_rounds"] == 1 and doc["spec"]["nodes"][0]["maxVisits"] == 1
    assert doc["spec"]["nodes"][2]["maxVisits"] == 2 and GRAPH["spec"]["nodes"][0]["maxVisits"] == 3
    assert derived.validate_graph(doc, "bugfix", CATALOG) == []
    bad = derived.tune_graph(GRAPH, {"graph_yaml": "kind: GraphDefinition\nspec:\n  workflowType: bugfix\n  entry: a\n"
                                                   "  nodes:\n    - {key: a, type: teleport_run}\n  edges:\n"
                                                   "    - {from: a, to: nowhere}\n"})
    msgs = " ".join(e["message"] for e in derived.validate_graph(bad, "bugfix", CATALOG))
    assert "unknown node type 'teleport_run'" in msgs and "nowhere" in msgs
    no_gate = {**GRAPH, "spec": {**GRAPH["spec"], "nodes": [GRAPH["spec"]["nodes"][2]], "entry": "p",
                                 "edges": [{"from": "p", "to": "end:done"}]}}
    assert any("publish_approval" in e["message"] for e in derived.validate_graph(no_gate, "bugfix", CATALOG))
    cfg_err = {**GRAPH, "spec": {**GRAPH["spec"], "nodes": [{"key": "a", "type": "review_run"}, *GRAPH["spec"]["nodes"][1:]]}}
    assert any("agent" in e["message"] for e in derived.validate_graph(cfg_err, "bugfix", CATALOG))


def test_derived_digest_is_stable_and_config_specific():
    a = P.pinned_part(P.merge({"gates": {"plan": "always"}}))
    b = P.pinned_part(P.merge({"gates": {"plan": "always"}, "triggers": {"label": "x"}}))   # live parts do not count
    assert derived.config_digest("rb_1", a) == derived.config_digest("rb_1", b)
    assert derived.config_digest("rb_1", a) != derived.config_digest("rb_2", a)
    assert derived.config_digest("rb_1", a) != derived.config_digest("rb_1", P.pinned_part(P.merge({})))
