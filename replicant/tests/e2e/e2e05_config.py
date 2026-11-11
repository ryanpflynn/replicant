"""E2E-05 (CONFIG-*): team configuration from the API under RBAC, with no image rebuilt or container restarted.

    uv run --package replicant-e2e python tests/e2e/e2e05_config.py
    (needs the v0 stack on the scripted gateway, `replicant dev seed` incl. 09-projects-rbac)

Records every running container's image ID and start time before the config steps and asserts they are unchanged
at the end. Steps (project prj_ledger, repo acme-payments/ledger-api):
  0. baseline config (label `replicant`, plan gate off, defaults) written by erin (tenant_admin)
  1. RBAC: dave (viewer) gets 403 on a config write; frank (project_admin of prj_ledger) gets 404 on
     prj_payments-web and may validate on prj_ledger; carol (linked, no binding) cannot trigger a job
  2. erin changes the trigger label to `replicant-ok`: an issue with the old label starts nothing, one with the new
     label starts job A (base bundle)
  3. erin turns the plan gate on while A is in flight: A never opens a plan gate; the next job B does (derived bundle)
  4. erin sets the large-change threshold to 0 and tunes the bugfix graph (max_review_rounds 1): job C runs on the
     tuned graph with risk >= medium; a YAML graph with an unknown node type is rejected by validate
  5. erin rolls back to the baseline version: job D behaves like A (base bundle, no plan gate, risk low)
Each job ends by approving the publish gate and closing its PR unmerged; the sample repo is reset before each job.
"""
from __future__ import annotations

import json
import subprocess
import time
import uuid

import replicant_e2e as e
from replicant_e2e import reset_repo

PRJ, OTHER = "prj_ledger", "prj_payments-web"
NEW_LABEL = "replicant-ok"
JOB_TIMEOUT_S = 1500
BASELINE = {"triggers": {"enabled": True, "label": "replicant", "modes": ["label"],
                         "allowed_workflow_types": ["bugfix"], "default_priority": "normal"}}
UNKNOWN_NODE_GRAPH = """apiVersion: replicant.example.com/v1
kind: GraphDefinition
metadata: {name: bugfix}
spec:
  workflowType: bugfix
  fglVersion: 1
  entry: a
  nodes:
    - {key: a, type: teleport_run}
  edges:
    - {from: a, to: "end:done"}
"""


def log(m: str) -> None:
    print(f"[e2e05 {time.strftime('%H:%M:%S')}] {m}", flush=True)


# ------------------------------------------------------------------------------------------------ no rebuild/restart
def containers() -> dict[str, tuple[str, str]]:
    ids = subprocess.run([e.ENGINE, "ps", "-q", "--filter", "label=com.docker.compose.project=replicant"],
                         capture_output=True, text=True, check=True).stdout.split()
    out = json.loads(subprocess.run([e.ENGINE, "inspect", *ids], capture_output=True, text=True, check=True).stdout)
    return {c["Name"].lstrip("/"): (c["Image"], c["State"]["StartedAt"]) for c in out
            if not c["Name"].lstrip("/").startswith("replicant-sbx-")}


def images() -> dict[str, str]:
    out = subprocess.run([e.ENGINE, "images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"], capture_output=True,
                         text=True, check=True).stdout.split("\n")
    return dict(ln.split(" ", 1) for ln in out if ln.startswith("localhost/replicant/"))


# ------------------------------------------------------------------------------------------------ helpers
def config(user: str = "erin") -> dict:
    with e.api(user) as c:
        return e.ok(c.get(f"/v1/projects/{PRJ}/config"), "GET config").json()


def put_config(cfg: dict, reason: str, user: str = "erin", project: str = PRJ, expect: int = 200) -> dict:
    with e.api(user) as c:
        cur = c.get(f"/v1/projects/{project}/config")
        ver = cur.json().get("version", 0) if cur.status_code == 200 else 0
        r = c.put(f"/v1/projects/{project}/config", json={"config": cfg, "reason": reason},
                  headers={"If-Match": str(ver)})
    assert r.status_code == expect, (user, project, r.status_code, r.text[:300])
    if expect == 200:
        doc = r.json()
        log(f"{user}: config v{doc['written_version']} ({reason}); changes "
            f"{[(ch['path'], ch['applies']) for ch in doc['changes']]}")
        return doc
    log(f"{user}: config write on {project} -> {r.status_code} {r.json().get('code')}")
    return {}


def ensure_label(name: str) -> int:
    with e.gitea() as g:
        labels = e.ok(g.get(f"/repos/{e.ORG}/{e.REPO}/labels", params={"limit": 50}), "labels").json()
        hit = next((lb for lb in labels if lb["name"] == name), None)
        if hit:
            return hit["id"]
        return e.ok(g.post(f"/repos/{e.ORG}/{e.REPO}/labels", json={"name": name, "color": "#00aa55"}),
                    "create label").json()["id"]


def open_issue(label: str, user: str = "alice") -> tuple[int, float]:
    title, body = e.issue_text()
    lid = ensure_label(label)
    with e.gitea(sudo=user) as g:
        t0 = time.monotonic()
        r = e.ok(g.post(f"/repos/{e.ORG}/{e.REPO}/issues", json={
            "title": title, "body": f"{body}\n\n<!-- e2e05 {uuid.uuid4().hex[:8]} -->", "labels": [lid]}), "issue")
    n = r.json()["number"]
    log(f"issue #{n} by {user} with label {label}")
    return n, t0


def bot_comments(number: int) -> list[str]:
    with e.gitea() as g:
        cs = e.ok(g.get(f"/repos/{e.ORG}/{e.REPO}/issues/{number}/comments"), "comments").json()
    return [c["body"] for c in cs if c["user"]["login"] == "replicant-bot"]


def expect_no_job(number: int, wait_s: float = 20) -> None:
    time.sleep(wait_s)
    found = [b for b in bot_comments(number) if e.JOB_RE.search(b)]
    assert not found, f"issue #{number} started a job: {found[0][:120]}"


def ensure_gitea_user(login: str) -> None:
    with e.gitea() as g:
        if g.get(f"/users/{login}").status_code == 200:
            return
        e.ok(g.post("/admin/users", json={"username": login, "email": f"{login}@acme.test",
                                          "password": uuid.uuid4().hex + "Aa1!", "must_change_password": False}),
             "create gitea user")
        e.ok(g.put(f"/repos/{e.ORG}/{e.REPO}/collaborators/{login}", json={"permission": "write"}), "collaborator")
    r = subprocess.run(["uv", "run", "replicant", "dev", "seed", "--only", "07-identity-links", "--force"],
                       cwd=e.ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-300:] + r.stderr[-300:]
    log(f"gitea user {login} created and linked")


def gate_open(kind: str):  # noqa: ANN202
    return lambda s: next((g for g in s.get("gates", []) if g.get("kind") == kind and g.get("state") == "open"), None)


def decide(gate: dict) -> None:
    body = {"decision": "approve", "presented_digest": gate["presented_digest"], "comment": "e2e05"}
    with e.api("bob") as c:
        r = c.post(f"/v1/gates/{gate['gate_id']}/decisions", json=body,
                   headers={"Idempotency-Key": f'"{uuid.uuid4().hex}"'})
    assert r.status_code in (200, 202), (r.status_code, r.text[:300])
    log(f"bob approved {gate['kind']} {gate['gate_id']}")


def run_job(label: str, *, expect_plan_gate: bool) -> dict:
    """Reset the repo, open an issue, drive the job to its PR (approving the gates), close the PR unmerged."""
    log(f"repo reset to the planted bug {reset_repo()[:12]}")
    number, t0 = open_issue(label)
    job_id = e.wait_ack(number, t0)
    deadline = time.monotonic() + JOB_TIMEOUT_S
    reader = e.api("grace")
    if expect_plan_gate:
        job = e.wait_until(reader, job_id, lambda s: gate_open("plan_approval")(s) is not None, "plan gate", deadline)
        decide(gate_open("plan_approval")(e.snapshot(job)))
    job = e.wait_until(reader, job_id, lambda s: gate_open("publish_approval")(s) is not None, "publish gate", deadline)
    snap = e.snapshot(job)
    decide(gate_open("publish_approval")(snap))
    pr = e.wait_pr(job_id, deadline)
    with e.gitea() as g:
        e.ok(g.patch(f"/repos/{e.ORG}/{e.REPO}/pulls/{pr['number']}", json={"state": "closed"}), "close PR")
    log(f"job {job_id}: PR #{pr['number']} closed unmerged")
    s = e.snapshot(e.get_job(reader, job_id))
    kinds = sorted({g.get("kind") for g in s.get("gates", [])})
    rb = s.get("release_bundle_id")
    with e.api("grace") as c:
        graph = c.get(f"/v1/jobs/{job_id}/graph").json()
    log(f"job {job_id}: bundle {rb} gates {kinds} risk {snap.get('risk', {}).get('level')} "
        f"graph {(graph.get('definition') or {}).get('ref')}")
    return {"job_id": job_id, "rb": rb, "gates": kinds, "risk": (snap.get("risk") or {}).get("level"),
            "graph": graph}


def default_bundle() -> str:
    with e.api("grace") as c:
        items = e.ok(c.get("/v1/releases/bundles"), "bundles").json()["items"]
    return next(b["release_bundle_id"] for b in items if b["assignment"]["role"] == "default")


def main() -> None:
    assert e.gateway_mode() == "script", "E2E-05 runs on the scripted gateway (REPLICANT_GATEWAY_MODE=script)"
    ensure_gitea_user("carol")
    ensure_label(NEW_LABEL)
    before_c, before_i = containers(), images()
    log(f"recorded {len(before_c)} containers and {len(before_i)} images")
    base_rb = default_bundle()

    # 0. baseline
    v0 = put_config(BASELINE, "e2e05: baseline")["written_version"]

    # 1. RBAC negatives
    put_config({**BASELINE, "gates": {"plan": "always"}}, "dave tries", user="dave", expect=403)
    put_config({"gates": {"plan": "always"}}, "frank tries the other project", user="frank", project=OTHER, expect=404)
    with e.api("frank") as c:
        r = c.post(f"/v1/projects/{PRJ}/config:validate", json={"config": BASELINE})
        assert r.status_code == 200 and r.json()["valid"], r.text[:200]
        assert c.get(f"/v1/projects/{OTHER}/config").status_code == 404
    log("frank: validates on prj_ledger, cannot see prj_payments-web")
    n, _ = open_issue("replicant", user="carol")
    time.sleep(15)
    bodies = bot_comments(n)
    assert any("missing_permission" in b for b in bodies) and not any(e.JOB_RE.search(b) for b in bodies), bodies
    log("carol (linked, no project binding): refused with missing_permission")

    # 2. trigger label (live)
    put_config({**BASELINE, "triggers": {**BASELINE["triggers"], "label": NEW_LABEL}}, "e2e05: new trigger label")
    old_issue, _ = open_issue("replicant")
    expect_no_job(old_issue)
    log("issue with the old label started nothing")
    # job A starts with the new label; plan gate goes on while A is in flight (step 3)
    log(f"repo reset {reset_repo()[:12]}")
    number, t0 = open_issue(NEW_LABEL)
    job_a = e.wait_ack(number, t0)
    cfg_plan = {**BASELINE, "triggers": {**BASELINE["triggers"], "label": NEW_LABEL}, "gates": {"plan": "always"}}
    put_config(cfg_plan, "e2e05: plan gate on")
    reader = e.api("grace")
    deadline = time.monotonic() + JOB_TIMEOUT_S
    job = e.wait_until(reader, job_a, lambda s: gate_open("publish_approval")(s) is not None, "A publish gate", deadline)
    sa = e.snapshot(job)
    assert not any(g.get("kind") == "plan_approval" for g in sa.get("gates", [])), "in-flight job A opened a plan gate"
    assert sa["release_bundle_id"] == base_rb, (sa["release_bundle_id"], base_rb)
    decide(gate_open("publish_approval")(sa))
    pr = e.wait_pr(job_a, deadline)
    with e.gitea() as g:
        e.ok(g.patch(f"/repos/{e.ORG}/{e.REPO}/pulls/{pr['number']}", json={"state": "closed"}), "close PR")
    log(f"job A {job_a}: base bundle, no plan gate, risk {(sa.get('risk') or {}).get('level')}")

    # 3. next job B opens a plan gate (derived bundle)
    b = run_job(NEW_LABEL, expect_plan_gate=True)
    assert "plan_approval" in b["gates"] and b["rb"] != base_rb, b

    # 4. large-change threshold 0 + tuned bugfix graph; an unknown node type is rejected
    with e.api("erin") as c:
        bad = c.post(f"/v1/projects/{PRJ}/config:validate",
                     json={"config": {**BASELINE, "workflows": {"bugfix": {"graph_yaml": UNKNOWN_NODE_GRAPH}}}}).json()
    assert not bad["valid"] and any("teleport_run" in x["message"] for x in bad["errors"]), bad["errors"]
    log(f"validate rejected the unknown node type: {bad['errors'][0]['message']}")
    tuned = {**cfg_plan, "gates": {"plan": "off"}, "risk": {"large_change_files": 0},
             "workflows": {"bugfix": {"params": {"max_review_rounds": 1}}}}
    with e.api("erin") as c:
        pv = e.ok(c.post(f"/v1/projects/{PRJ}/config:validate", json={"config": tuned}), "validate").json()
    assert pv["valid"] and pv["graphs"]["bugfix"]["graph"]["nodes"], pv["errors"]
    review = next(x for x in pv["graphs"]["bugfix"]["graph"]["nodes"] if x["key"] == "review")
    assert review["max_visits"] == 1, review
    log(f"tuned bugfix graph previews as {pv['graphs']['bugfix']['definition']['ref']} (review max_visits 1)")
    put_config(tuned, "e2e05: threshold 0, one review round")
    cjob = run_job(NEW_LABEL, expect_plan_gate=False)
    assert cjob["risk"] in ("medium", "high", "critical"), cjob
    ref = (cjob["graph"].get("definition") or {}).get("ref", "")
    assert ref.startswith("graph/bugfix-c"), ref
    rv = next(x for x in cjob["graph"]["graph"]["nodes"] if x["key"] == "review")
    assert rv["max_visits"] == 1 and rv["status"] == "succeeded", rv

    # 5. rollback to the baseline version
    with e.api("erin") as c:
        rb_doc = e.ok(c.post(f"/v1/projects/{PRJ}/config:rollback",
                             json={"version": v0, "reason": "e2e05: back to baseline"}), "rollback").json()
    log(f"rolled back: v{rb_doc['written_version']} copies v{v0}")
    d = run_job("replicant", expect_plan_gate=False)
    assert d["rb"] == base_rb and "plan_approval" not in d["gates"] and d["risk"] == "low", d

    after_c, after_i = containers(), images()
    assert after_i == before_i, {k: (before_i.get(k), after_i.get(k)) for k in set(before_i) | set(after_i)
                                 if before_i.get(k) != after_i.get(k)}
    changed = {k: (before_c.get(k), after_c.get(k)) for k in set(before_c) | set(after_c)
               if before_c.get(k) != after_c.get(k)}
    assert not changed, f"containers rebuilt or restarted: {changed}"
    log(f"no image rebuilt, no container restarted ({len(after_c)} containers)")
    log(f"evidence: A {job_a}  B {b['job_id']} ({b['rb']})  C {cjob['job_id']} ({cjob['rb']})  D {d['job_id']}")
    log("E2E-05 config: PASS")


def test_e2e05_config() -> None:
    main()


if __name__ == "__main__":
    main()
