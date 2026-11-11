"""E2E-08 (ONBOARD, design §8c): repo onboarding across a Go, a Python and a Node repo.

    uv run --package replicant-e2e python tests/e2e/e2e08_onboarding.py
    (needs the v0 stack with the onboarding build: env-python-service and env-node-service images, gitea-bootstrap with
     billing-py and web-console, `replicant dev seed` incl. 09-projects-rbac, scripted gateway)

Projects: prj_ledger (ledger-api, Go) and prj_payments-web (billing-py, Python/uv; web-console, Node/pnpm).
  1. Profiles: a fleet rescan (carol, replicant-admins) gives every sample repo a profile whose inferred environment
     and commands match its CI workflow: go-service, python-service (uv run pytest -q, uv run ruff check .),
     node-service (pnpm install --frozen-lockfile, pnpm test).
  2. Readiness: the probes (label replicant-readiness, no LLM) finish; billing-py is `ready`; web-console is `blocked`
     with cause missing_tool (vips: the node image deliberately has no libvips); ledger-api is logged (its main carries
     the planted failing test, so the probe verdict is fail). A job for web-console is refused with 409 repo_not_ready.
  3. Repo config: a bugfix job on billing-py (issue ISSUES/001.md) runs with the repo's configuration: its run
     observations reach the fleet queue (/admin/fleet) ranked: the PostToolUse hook loaded and the credentialed jira
     MCP server skipped (not_allowed).
  4. Opting out: with `repo_config: ignore` on prj_payments-web the next job's observations carry no repo hook.
  5. Tiers: models.workflows.bugfix = thorough on the project: the next job's implementer run selects the Opus model
     (timeline event run.model_selected).
Every job is cancelled once its evidence is in; the project config is restored at the end.
"""
from __future__ import annotations

import time
import uuid

import replicant_e2e as e
from replicant_e2e import reset_repo
from replicant_ids import dev_principal

e.TAG = "e2e08"
LEDGER, WEB = "prj_ledger", "prj_payments-web"
ADMIN, FLEET_ADMIN, PROJECT_ADMIN = "grace", "carol", "erin"
PROFILE_TIMEOUT_S = 300
PROBE_TIMEOUT_S = 900
JOB_TIMEOUT_S = 1200
OPUS = "anthropic/claude-opus-5-5"
JOBS: list[str] = []


def log(m: str) -> None:
    e.log(m)


def readiness(project: str, user: str = ADMIN) -> dict[str, dict]:
    with e.api(user) as c:
        repos = e.ok(c.get(f"/v1/projects/{project}/readiness"), f"readiness {project}").json()["repos"]
    return {r["full_name"].split("/", 1)[1]: r for r in repos}


def all_readiness() -> dict[str, dict]:
    return {**readiness(LEDGER), **readiness(WEB)}


def rescan() -> None:
    with e.api(FLEET_ADMIN) as c:
        r = c.post("/v1/admin/fleet:rescan", json={})
    assert r.status_code == 202, (r.status_code, r.text[:300])
    log(f"fleet rescan queued: {r.json().get('queued')}")


def recheck(project: str, repo_id: str) -> str:
    with e.api(PROJECT_ADMIN) as c:
        r = c.post(f"/v1/projects/{project}/repos/{repo_id}:recheck")
    assert r.status_code == 202, (r.status_code, r.text[:300])
    return r.json()["job_id"]


def config(project: str) -> dict:
    with e.api(PROJECT_ADMIN) as c:
        return e.ok(c.get(f"/v1/projects/{project}/config"), "GET config").json()


def put_config(project: str, cfg: dict, reason: str) -> None:
    with e.api(PROJECT_ADMIN) as c:
        ver = c.get(f"/v1/projects/{project}/config").json().get("version", 0)
        e.ok(c.put(f"/v1/projects/{project}/config", json={"config": cfg, "reason": f"e2e08: {reason}"},
                   headers={"If-Match": str(ver)}), f"PUT config {project}")
    log(f"{project}: {reason}")


def fleet_items(kind: str) -> list[dict]:
    with e.api(FLEET_ADMIN) as c:
        return e.ok(c.get("/v1/admin/fleet/items", params={"kind": kind, "sort": "impact", "limit": 200}),
                    "fleet items").json()["items"]


def timeline(job_id: str) -> list[dict]:
    with e.api(ADMIN) as c:
        return e.ok(c.get(f"/v1/jobs/{job_id}/timeline", params={"limit": 200}), "timeline").json()["items"]


def cancel(job_id: str) -> None:
    with e.api(e.REQUESTER) as c:
        r = c.post(f"/v1/jobs/{job_id}:cancel", json={"reason": "e2e08: evidence collected"},
                   headers={"Idempotency-Key": f'"{uuid.uuid4().hex}"'})
    log(f"job {job_id} cancel -> HTTP {r.status_code}")


def run_until_implemented(repo: str) -> str:
    """Reset the repo, open its issue, wait until the implementer run has finished (its observations are posted after
    each run), then cancel the job."""
    log(f"{repo} reset to its initial commit {reset_repo(repo)[:12]}")
    number, t0 = e.open_issue("e2e08", repo)
    job_id = e.wait_ack(number, t0, repo)
    JOBS.append(job_id)
    deadline = time.monotonic() + JOB_TIMEOUT_S
    reader = e.api(ADMIN)

    def implemented(s: dict) -> bool:
        return any(r.get("role") in ("implementer", "repair") and r.get("phase") in ("Succeeded", "Failed")
                   for r in s.get("runs", []))
    e.wait_until(reader, job_id, implemented, "implementer run finished", deadline)
    time.sleep(5)  # the observations activity runs right after the run's result is read
    cancel(job_id)
    return job_id


# ------------------------------------------------------------------------------------------------ steps
def step_profiles() -> dict[str, dict]:
    rescan()
    want = {"ledger-api": "go-service", "billing-py": "python-service", "web-console": "node-service"}

    def profiled() -> dict | None:
        r = all_readiness()
        return r if all((r.get(n) or {}).get("profile_id") for n in want) else None
    r = e.wait_for(profiled, "a profile for every sample repo", PROFILE_TIMEOUT_S, every=5)
    for name, env_name in want.items():
        env = r[name]["environment"] or {}
        cmds = env.get("commands") or {}
        log(f"{name}: {env.get('environment')} {env.get('version')} commands={cmds} source={env.get('source')}")
        assert env.get("environment") == env_name, (name, env)
    assert "go test" in (r["ledger-api"]["environment"]["commands"].get("test") or "")
    b = r["billing-py"]["environment"]["commands"]
    assert b.get("test") == "uv run pytest -q" and b.get("lint") == "uv run ruff check .", b
    assert ".github/workflows/ci.yml" in str(r["billing-py"]["environment"]["source"].get("test")), r["billing-py"]
    w = r["web-console"]["environment"]["commands"]
    assert w.get("test") == "pnpm test" and "pnpm install --frozen-lockfile" in (w.get("install") or ""), w
    rc = r["billing-py"]["repo_config"]
    assert rc["settings_present"] and any(h["event"] == "PostToolUse" for h in rc["hooks"]), rc
    assert {"CLAUDE.md", "src/billing/CLAUDE.md"} <= set(rc["claude_md"]) and "python-style.md" in rc["rules"], rc
    assert {s["name"] for s in rc["mcp_servers"]} == {"schema", "jira"}, rc["mcp_servers"]
    return r


def step_readiness(profiles: dict[str, dict]) -> None:
    def probed() -> dict | None:
        r = all_readiness()
        done = all((r[n].get("last_probe") or {}).get("status") in ("ready", "warnings", "blocked")
                   for n in ("billing-py", "web-console"))
        return r if done else None
    try:
        r = e.wait_for(probed, "automatic readiness probes", 120, every=5)
    except AssertionError:
        log("no automatic probe result yet: re-checking billing-py and web-console")
        for name in ("billing-py", "web-console"):
            JOBS.append(recheck(WEB, profiles[name]["repo_id"]))
        r = e.wait_for(probed, "re-checked readiness probes", PROBE_TIMEOUT_S, every=10)
    for name in ("ledger-api", "billing-py", "web-console"):
        lp = r[name].get("last_probe") or {}
        log(f"{name}: status={r[name]['status']} probe={lp.get('job_id')} causes={lp.get('causes')}")
    assert r["billing-py"]["status"] == "ready", r["billing-py"]
    wc = r["web-console"]
    assert wc["status"] == "blocked", wc
    assert any(c.get("cause") == "missing_tool" and "vips" in str(c.get("detail")) for c in wc["last_probe"]["causes"]), wc
    lp = r["ledger-api"].get("last_probe") or {}
    assert not any(c.get("cause") in ("missing_tool", "network") for c in lp.get("causes") or []), lp
    # a job for the blocked repo is refused at intake
    with e.gitea() as g:
        labels = e.ok(g.get(f"/repos/{e.ORG}/web-console/labels", params={"limit": 50}), "labels").json()
        label_id = next(lb["id"] for lb in labels if lb["name"] == "replicant")
    with e.gitea(sudo=e.REQUESTER) as g:
        n = e.ok(g.post(f"/repos/{e.ORG}/web-console/issues",
                        json={"title": "e2e08: blocked repo", "body": "<!-- e2e08 -->", "labels": [label_id]}),
                 "issue").json()["number"]
    host = profiles["web-console"].get("host") or "git.replicant.localhost:3300"
    jr = {"schema_version": "replicant.job-request/v1", "request_id": f"jrq_{uuid.uuid4().hex[:26]}",
          "tenant_id": e.TENANT, "workflow_type": "bugfix", "priority": "normal",
          "dedupe_key": f"api:e2e08-{uuid.uuid4().hex}",
          "received_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
          "source": {"channel": "api", "event_type": "api.request", "event_id": uuid.uuid4().hex},
          "requester": {"kind": "user", "principal_id": dev_principal(e.REQUESTER)},
          "intent": {"title": "e2e08: blocked repo", "format": "markdown", "untrusted": True, "text": f"issue #{n}"},
          "targets": [{"repo": {"host": host, "owner": e.ORG, "name": "web-console",
                                "repo_id": profiles["web-console"]["repo_id"], "provider": "gitea"}, "base_ref": "main"}]}
    with e.api(e.REQUESTER) as c:
        r409 = c.post("/v1/jobs", json=jr)
    assert r409.status_code == 409 and "repo_not_ready" in r409.text, (r409.status_code, r409.text[:300])
    log("web-console job refused: 409 repo_not_ready")


def step_repo_config() -> None:
    job = run_until_implemented("billing-py")
    hooks = [i for i in fleet_items("hook") if "PostToolUse" in i["identity"]]
    mcp = {i["identity"]: i for i in fleet_items("mcp_server")}
    log(f"job {job}: hooks {[(i['identity'][:40], i['jobs']) for i in hooks]} mcp {sorted(mcp)}")
    assert hooks and hooks[0]["jobs"] >= 1, hooks
    jira = mcp.get("url:https://jira.acme.internal")
    assert jira and jira["class"] in ("needs_credentials", "needs_egress"), jira
    with e.api(FLEET_ADMIN) as c:
        item = e.ok(c.get(f"/v1/admin/fleet/items/{jira['id']}"), "fleet item").json()
    assert any(j["job_id"] == job for j in item.get("recent_jobs") or []), item.get("recent_jobs")
    ranked = fleet_items("mcp_server")
    assert ranked == sorted(ranked, key=lambda i: -float((i.get("impact") or {}).get("score") or 0)), "impact order"
    log("billing-py job: hook loaded, jira skipped, ranked in /admin/fleet")


def step_opt_out(base: dict) -> None:
    put_config(WEB, {**base, "repo_config": "ignore"}, "repo_config ignore")
    before = {i["identity"]: i["jobs"] for i in fleet_items("hook")}
    job = run_until_implemented("billing-py")
    after = {i["identity"]: i["jobs"] for i in fleet_items("hook")}
    grew = [k for k in after if after[k] > before.get(k, 0)]
    assert not grew, f"job {job} with repo_config ignore still observed hooks: {grew}"
    log(f"job {job}: repo_config ignore -> no repo hook observed")


def step_tiers(base: dict) -> None:
    put_config(WEB, {**base, "models": {"default_tier": "standard", "workflows": {"bugfix": "thorough"}}},
               "bugfix on the thorough tier")
    job = run_until_implemented("billing-py")
    ev = [t for t in timeline(job) if t["type"] == "run.model_selected"]
    log(f"job {job}: {[t['summary'] for t in ev]}")
    assert any(t["summary"].startswith("implementer run uses tier thorough") and OPUS in t["summary"] for t in ev), ev


def main() -> None:
    profiles = step_profiles()
    step_readiness(profiles)
    base = config(WEB).get("config") or {}
    try:
        step_repo_config()
        step_opt_out(base)
        step_tiers(base)
    finally:
        put_config(WEB, base, "restore the project configuration")
        for name in ("billing-py",):
            reset_repo(name)
    log(f"evidence: jobs {JOBS}")
    log("E2E-08 onboarding: PASS")


def test_e2e08_onboarding() -> None:
    main()


if __name__ == "__main__":
    main()
