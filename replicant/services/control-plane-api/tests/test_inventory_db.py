"""ONBOARD (design §8c) against the migrated smoke Postgres: profile -> readiness probe -> intake pinning and the 409,
observations, the fleet queue with its impact SQL, decisions and support requests. Skipped without CTL_DB_DSN."""
from __future__ import annotations

import datetime as dt
import json
import os
import secrets

import pytest
from sqlalchemy import text
from test_db_integration import DSN, env, job_request  # noqa: F401  (env is a fixture)

from control_plane_api.auth import service_principal
from control_plane_api.ids import ulid

pytestmark = pytest.mark.skipif(not DSN, reason="CTL_DB_DSN not set (start tests/smoke/env.sh)")

ERIN, ALICE, CAROL = ({"X-Replicant-Dev-User": u} for u in ("erin", "alice", "carol"))


def profile(repo_id: str, sha: str, **over) -> dict:
    p = {"repo_id": repo_id, "full_name": "acme-payments/billing-py", "default_branch": "main", "sha": sha,
         "scanned_at": "2026-11-10T12:00:00Z", "scanner_version": 1,
         "files": [{"path": "pyproject.toml", "sha256": None, "bytes": 10}, {"path": "uv.lock", "sha256": None,
                                                                            "bytes": 10}],
         "ecosystems": [{"ecosystem": "python", "version": "3.12", "package_manager": "uv", "source": ".python-version"}],
         "ci_commands": [{"stage": "test", "command": "uv run pytest -q",
                          "source": ".github/workflows/ci.yml#jobs.test"}],
         "devcontainer": None,
         "claude": {"settings_present": True, "hooks": [{"event": "PostToolUse", "matcher": "Edit",
                                                         "command": "uv run ruff format", "command_hash": "sha256:aa"}],
                    "rules": ["python-style.md"], "skills": [], "commands": [], "agents": [],
                    "claude_md": ["CLAUDE.md", "src/billing/CLAUDE.md"], "permission_rules": 0},
         "agents_md": False, "copilot_instructions": False,
         "mcp_servers": [{"name": "schema", "transport": "stdio", "command": "npx",
                          "args": ["-y", f"@acme/schema-mcp-{repo_id}"], "url": None, "env_keys": [],
                          "header_keys": [], "identity": f"stdio:npx:@acme/schema-mcp-{repo_id}"},
                         {"name": "jira", "transport": "http", "command": None, "args": [],
                          "url": "https://jira.acme.internal", "env_keys": [], "header_keys": ["Authorization"],
                          "identity": "url:https://jira.acme.internal"}],
         "pre_commit": {"present": False, "hooks": []}, "errors": []}
    p.update(over)
    return p


async def _seed_job(su, job_id: str, repo_id: str, status: str, repair: int, qa: str | None) -> None:
    now = dt.datetime.now(dt.UTC)
    await su.execute(text(
        "INSERT INTO jobs_read.jobs (job_id, tenant_id, seq, as_of, workflow_type, status, phase, requester, "
        "release_bundle_id, created_at, snapshot, repo_ids, repair_used, terminal_at) VALUES (:j, 'payments', 1, :n, "
        "'bugfix', :s, 'done', 'usr_x', 'rb_x', :n, '{}'::jsonb, ARRAY[:r], :rep, :n)"),
        {"j": job_id, "n": now, "s": status, "r": repo_id, "rep": repair})
    if qa:
        await su.execute(text(
            "INSERT INTO jobs_read.job_runs (run_id, job_id, tenant_id, seq, node_key, cycle, ordinal, role, phase, "
            "qa_verdict) VALUES (:run, :j, 'payments', 1, 'qa', 0, 1, 'qa', 'Succeeded', :v)"),
            {"run": ulid("run"), "j": job_id, "v": qa})


async def test_onboarding_flow(env):  # noqa: F811
    c, fake, seed_h, _shim, app = env
    from control_plane_api import routes_inventory as RI
    gi_tok, wk_tok = secrets.token_urlsafe(12), secrets.token_urlsafe(12)
    for tok, pid in ((gi_tok, "svc_github-integration"), (wk_tok, "svc_replicant-worker")):
        p = service_principal(pid)
        app.state.auth._internal[tok] = p
        app.state.auth.directory.add(p)
    gi, wk = {"Authorization": f"Bearer {gi_tok}"}, {"Authorization": f"Bearer {wk_tok}"}
    pid, repo = f"prj_o{secrets.token_hex(4)}", str(800000 + secrets.randbelow(99999))
    r = await c.post("/v1/tenants/payments/projects", headers=seed_h, json={
        "project_id": pid, "name": "Onboard", "repos": [{"repo_id": repo, "full_name": "acme-payments/billing-py"}]})
    assert r.status_code == 201, r.text

    # 1. profile -> classification + environment; readiness pending
    sha = secrets.token_hex(20)
    assert (await c.put(f"/internal/v1/repos/{repo}/profile", headers=wk, json=profile(repo, sha))).status_code == 403
    r = await c.put(f"/internal/v1/repos/{repo}/profile", headers=gi, json=profile(repo, sha))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile_id"].startswith("rp_") and body["environment"]["environment"] == "python-service"
    assert body["environment"]["commands"]["test"] == "uv run pytest -q"
    cls = {i["identity"]: i["class"] for i in body["classified"]}
    assert cls["url:https://jira.acme.internal"] == "needs_credentials" and cls["hook:PostToolUse:sha256:aa"] == "ok"
    assert cls["toolchain:python:3.12:uv"] == "supported"
    r2 = await c.put(f"/internal/v1/repos/{repo}/profile", headers=gi, json=profile(repo, sha))   # idempotent per sha
    assert r2.json()["profile_id"] == body["profile_id"]
    assert (await c.put("/internal/v1/repos/99999999/profile", headers=gi, json=profile("99999999", sha))) \
        .status_code == 404

    # 2. the launcher submits the probe through intake: qa_only, labelled, pinned to the sha, with the profile
    n_starts = len(fake.starts)
    assert await RI.launch_pending(app.state) >= 1
    probe = next(s for s in fake.starts[n_starts:] if s["job_request"]["targets"][0]["repo"]["repo_id"] == repo)
    pjr = probe["job_request"]
    assert pjr["constraints"]["labels"] == ["replicant-readiness"] and pjr["workflow_type"] == "qa_only"
    assert pjr["targets"][0]["base_sha"] == sha and pjr["repo_profile"]["environment"] == "python-service"
    assert pjr["repo_profile"]["repo_config"] == "use" and pjr["repo_profile"]["mcp_auto_allow"] == []
    probe_job = probe["job_id"]

    # 3. a normal job pins the profile; a blocked probe makes intake refuse with 409 unless overridden
    jr = job_request("alice")
    jr["targets"][0]["repo"]["repo_id"] = repo
    jr["repo_profile"] = {"profile_id": "rp_" + "z" * 26}             # a submitted profile is never trusted
    r = await c.post("/v1/jobs", headers=ALICE, json=jr)
    assert r.status_code == 201, r.text
    pinned = fake.starts[-1]["job_request"]["repo_profile"]
    assert pinned["profile_id"] == body["profile_id"] and pinned["readiness"] == "probing" and pinned["sha"] == sha
    r = await c.put(f"/internal/v1/repos/{repo}/readiness", headers=wk, json={
        "job_id": probe_job, "profile_id": body["profile_id"], "status": "blocked",
        "causes": [{"stage": "install", "cause": "missing_tool", "detail": "libvips"}]})
    assert r.status_code == 200 and r.json()["status"] == "blocked", r.text
    r = await c.post("/v1/jobs", headers=ALICE, json=job_request("alice", targets=jr["targets"]))
    assert r.status_code == 409 and r.json()["code"] == "repo_not_ready", r.text
    r = await c.post(f"/v1/projects/{pid}/repos/{repo}:recheck", headers=ALICE)
    assert r.status_code == 403
    r = await c.post(f"/v1/projects/{pid}/repos/{repo}:recheck", headers=ERIN)   # the probe itself is let through
    assert r.status_code == 202 and r.json()["job_id"].startswith("job_"), r.text
    recheck_job = r.json()["job_id"]
    cfg = (await c.get(f"/v1/projects/{pid}/config", headers=ERIN)).json()
    r = await c.put(f"/v1/projects/{pid}/config", headers={**ERIN, "If-Match": str(cfg["version"])},
                    json={"config": {"triggers": {"readiness_override": True}, "repo_config": "ignore"},
                          "reason": "known issue"})
    assert r.status_code == 200, r.text
    r = await c.post("/v1/jobs", headers=ALICE, json=job_request("alice", targets=jr["targets"]))
    assert r.status_code == 201, r.text
    assert fake.starts[-1]["job_request"]["repo_profile"]["repo_config"] == "ignore"

    # 4. readiness view: blocked, the probe, warnings with item ids
    r = await c.get(f"/v1/projects/{pid}/readiness", headers=ALICE)
    assert r.status_code == 200, r.text
    rd = next(x for x in r.json()["repos"] if x["repo_id"] == repo)
    assert rd["status"] == "probing" and rd["last_probe"]["job_id"] == recheck_job   # the re-check is running
    assert rd["environment"]["environment"] == "python-service" and rd["repo_config_mode"] == "ignore"
    assert rd["repo_config"]["claude_md"] == ["CLAUDE.md", "src/billing/CLAUDE.md"]
    warn = {w["identity"]: w for w in rd["warnings"]}
    assert "url:https://jira.acme.internal" in warn and warn["url:https://jira.acme.internal"]["item_id"].startswith("inv_")
    assert any(i.startswith("probe:python-service:missing_tool:libvips") for i in warn)
    schema_item = warn[f"stdio:npx:@acme/schema-mcp-{repo}"]["item_id"]            # unpinned: review -> skipped

    # 5. support request (project admins), admin decision (audited) -> the server is allowed on the next job
    r = await c.post(f"/v1/projects/{pid}/fleet-items/{schema_item}:request", headers=ALICE, json={"reason": "x"})
    assert r.status_code == 403
    r = await c.post(f"/v1/projects/{pid}/fleet-items/{schema_item}:request", headers=ERIN,
                     json={"reason": "we need the schema tool"})
    assert r.status_code == 201 and r.json()["request_id"].startswith("srq_"), r.text
    r = await c.post(f"/v1/admin/fleet/items/{schema_item}:decide", headers=ERIN,
                     json={"decision": "approve", "reason": "contained"})
    assert r.status_code == 403
    r = await c.post(f"/v1/admin/fleet/items/{schema_item}:decide", headers=CAROL,
                     json={"decision": "approve", "reason": "contained"})
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["effective"] == "allow" and item["decision"] == "approve" and len(item["requests"]) == 1
    assert item["repos"][0]["repo_id"] == repo and item["decisions"][0]["reason"] == "contained"
    r = await c.post("/v1/jobs", headers=ALICE, json=job_request("alice", targets=jr["targets"]))
    assert fake.starts[-1]["job_request"]["repo_profile"]["mcp_auto_allow"] == [
        {"name": "schema", "command": "npx", "args": ["-y", f"@acme/schema-mcp-{repo}"]}]

    # 6. observations + impact: jobs with the hook fail more than comparable jobs without it
    su_dsn = os.environ.get("CTL_SU_DSN")
    if not su_dsn:
        pytest.skip("CTL_SU_DSN not set (seeding jobs_read)")
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(su_dsn)
    with_jobs, without_jobs = [ulid("job") for _ in range(3)], [ulid("job") for _ in range(3)]
    async with eng.begin() as su:
        for j in with_jobs:
            await su.execute(text("INSERT INTO intake.job_requests (job_id, tenant_id, request_id, dedupe_key, channel, "
                                  "requester, submitted_by, workflow_type, workflow_type_started, request, "
                                  "release_bundle_id, assignment_reason, release_cohort, assignment_version, received_at, "
                                  "start_status) SELECT :j, tenant_id, :rq, :k, channel, requester, submitted_by, "
                                  "workflow_type, workflow_type_started, request, release_bundle_id, assignment_reason, "
                                  "release_cohort, assignment_version, received_at, 'started' FROM intake.job_requests WHERE "
                                  "job_id = :src"), {"j": j, "rq": ulid("jrq"), "k": f"t:{j}", "src": probe_job})
        for j in with_jobs:
            await _seed_job(su, j, repo, "failed", 2, "fail")
        for j in without_jobs:
            await _seed_job(su, j, repo, "succeeded", 0, "pass")
    await eng.dispose()
    hook = "hook:PostToolUse:sha256:aa"
    for j in with_jobs:
        r = await c.post("/internal/v1/inventory/observations", headers=wk, json={
            "job_id": j, "run_id": ulid("run"), "repo_id": repo, "workflow_type": "bugfix", "role": "implementer",
            "items": [{"kind": "hook", "identity": hook, "status": "failed", "cause": "missing_binary"},
                      {"kind": "mcp_server", "identity": "url:https://new.example", "status": "skipped",
                       "cause": "not_allowed"}],
            "tier": "standard", "model": "anthropic/claude-sonnet-5-5", "settings_hash": "sha256:x"})
        assert r.status_code == 202 and r.json()["accepted"] == 2, r.text
    r = await c.post("/internal/v1/inventory/observations", headers=wk, json={
        "job_id": ulid("job"), "repo_id": repo, "items": []})
    assert r.status_code == 404
    r = await c.get("/v1/admin/fleet/items?kind=hook&sort=impact&limit=200", headers=CAROL)
    assert r.status_code == 200, r.text
    h = next(i for i in r.json()["items"] if i["identity"] == hook)
    imp = h["impact"]
    assert imp["jobs_with"] == 3 and imp["jobs_without"] == 3 and imp["score"] > 0
    assert imp["delta"]["failed"] > 1 and imp["delta"]["qa_first_pass"] < 1 and imp["delta"]["repair_cycles"] > 1
    assert imp["delta"]["rejected"] == 1
    r = await c.get(f"/v1/admin/fleet/items/{h['id']}", headers=CAROL)
    assert r.status_code == 200 and {j["job_id"] for j in r.json()["recent_jobs"]} == set(with_jobs)
    new = await c.get("/v1/admin/fleet/items?kind=mcp_server&class=review&limit=200", headers=CAROL)
    assert any(i["identity"] == "url:https://new.example" for i in new.json()["items"])
    assert json.dumps(new.json())       # serialisable
