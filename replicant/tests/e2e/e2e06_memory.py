"""E2E-06 (MEM): Replicant memory on the live v0 stack, scripted gateway.

    uv run --package replicant-e2e python tests/e2e/e2e06_memory.py
    (needs the v0 stack with memory-service, REPLICANT_GATEWAY_MODE=script, `replicant dev seed` incl. 09-projects-rbac)

Steps (tenant payments, repo acme-payments/ledger-api):
  0. RBAC: erin (tenant admin) and grace (platform admin) get 403 on /v1/admin/memory/** with an audit id; carol
     (replicant-admins) gets 200; dave (viewer) can read a job's "Memory used" but not curate.
  1. carol authors a repo rule; job A's first agent run receives it in inputs/memory-injection.json (manifest
     memory_entries + rendered block). carol disables it while A runs: a later run of A no longer has it, and A's
     memory_snapshot_id never changes.
  2. bob's request_changes comment on A's publish gate becomes a candidate (provenance gate_comment, job A); a
     PR-specific reject comment does not. carol approves the candidate; job B's runs receive it.
  4. frank (project admin) starts a repo scan of ledger-api: the scan job ends no_change_needed. Of the scripted
     scanner's four proposals, the two CLAUDE.md restates are dropped as documented (documented_in CLAUDE.md), the
     one citing a missing file is dropped as unverified, and only the verified inferred convention (pattern seen in
     >= 70% of sampled files) lands in the queue (origin import, provenance pointing to the scan job) (MEM-D20).
  5. Quarantine dry run by approver; purge of the step-1 rule: blast radius lists job A, the entry is a tombstone,
     job A's memory view shows it removed, and the export no longer contains it. Analytics answer.
  6. Cleanup: carol purges every entry this run's jobs created, so the admin queue keeps no test data.
  7. Evidence trust (MEM-D20..D13), synthetic PR-comment sources through the worker's memory client: one reviewer
     repeating one comment in three jobs is one independent source; the PR-derived candidate is held (approve 409)
     until its job's PR merges with the fix; a closed-unmerged PR auto-rejects its candidate.
"""
from __future__ import annotations

import json
import subprocess
import time
import uuid

import replicant_e2e as e
from replicant_e2e import reset_repo

e.TAG = "e2e06"
REPO = "git.replicant.localhost/acme-payments/ledger-api"
JOB_TIMEOUT_S = 1800
MARK = uuid.uuid4().hex[:8]
RULE_PREFIX = "In ledger-api, keep money in int64 minor units and add a table-driven test case for every fix"
AUTHORED: list[str] = []  # admin-authored entries of this run (no job provenance: purged by ID at the end)
log = e.log


def mem(user: str, method: str, path: str, body: dict | None = None, if_match: int | None = None,
        expect: int | tuple[int, ...] = 200) -> dict:
    h = {"Idempotency-Key": f"e2e06-{uuid.uuid4().hex}"}
    if if_match is not None:
        h["If-Match"] = str(if_match)
    with e.api(user) as c:
        r = c.request(method, f"/v1/admin/memory/{path}", json=body, headers=h)
    want = expect if isinstance(expect, tuple) else (expect,)
    assert r.status_code in want, (user, method, path, r.status_code, r.text[:400])
    return r.json() if r.content else {}


def entry(eid: str) -> dict:
    return mem("carol", "GET", f"entries/{eid}")


def act(eid: str, verb: str, body: dict | None = None) -> dict:
    return mem("carol", "POST", f"entries/{eid}:{verb}", body or {"reason": "e2e06"}, if_match=entry(eid)["state_seq"])


def run_inputs(job_id: str) -> list[dict]:
    """Every run's inputs manifest memory_entries and memory-injection.json, read from S3 inside the worker."""
    code = f"""
import json
from replicant_worker.activities import s3
from replicant_worker.activities.config import settings
c = s3.client(); b = settings().bucket('artifacts'); out = []
pre = 'tenants/payments/jobs/{job_id}/runs/'
keys = [o['Key'] for p in c.get_paginator('list_objects_v2').paginate(Bucket=b, Prefix=pre) for o in p.get('Contents', [])]
for k in keys:
    if k.endswith('/inputs/manifest.json'):
        m = json.loads(c.get_object(Bucket=b, Key=k)['Body'].read())
        inj = k.replace('manifest.json', 'memory-injection.json')
        r = json.loads(c.get_object(Bucket=b, Key=inj)['Body'].read()).get('rendered', '') if inj in keys else ''
        fm = c.get_object(Bucket=b, Key=k.replace('manifest.json', 'first-message.md'))['Body'].read().decode() if k.replace('manifest.json', 'first-message.md') in keys else ''
        out.append({{'run_id': m['run_id'], 'role': m['role'], 'created_at': m.get('created_at'), 'memory_entries': m.get('memory_entries', []),
                    'snapshot': m.get('memory_snapshot_id'), 'rendered': r, 'first_message': fm}})
print(json.dumps(sorted(out, key=lambda x: x['created_at'] or '')))
"""
    r = subprocess.run([e.ENGINE, "exec", e.container("replicant-worker"), "python", "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def decide(gate: dict, decision: str, comment: str, user: str = "bob") -> None:
    with e.api(user) as c:
        r = c.post(f"/v1/gates/{gate['gate_id']}/decisions", json={"decision": decision, "comment": comment,
                   "presented_digest": gate["presented_digest"]}, headers={"Idempotency-Key": f'"{uuid.uuid4().hex}"'})
    assert r.status_code in (200, 202), (r.status_code, r.text[:300])
    log(f"{user}: {decision} on {gate['kind']} {gate['gate_id']}")


def gate_open(kind: str, not_id: str | None = None):  # noqa: ANN201
    return lambda s: next((g for g in s.get("gates", []) if g.get("kind") == kind and g.get("state") == "open"
                           and g.get("gate_id") != not_id), None)


def start_job() -> str:
    log(f"repo reset to the planted bug {reset_repo()[:12]}")
    n, t0 = e.open_issue(MARK)
    j = e.wait_ack(n, t0)
    JOBS.append(j)
    return j


def job_memory(job_id: str, user: str = "dave") -> dict:
    with e.api(user) as c:
        return e.ok(c.get(f"/v1/jobs/{job_id}/memory"), "job memory").json()


def candidates(**q: str) -> list[dict]:
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    return mem("carol", "GET", f"candidates?tenant_id=payments&{qs}")["items"]


def main() -> None:
    assert e.gateway_mode() == "script", "E2E-06 runs on the scripted gateway (REPLICANT_GATEWAY_MODE=script)"
    # ---- 0. RBAC
    for u in ("erin", "grace"):
        with e.api(u) as c:
            r = c.get("/v1/admin/memory/candidates")
        assert r.status_code == 403 and r.json()["code"] == "not_memory_admin" and r.json().get("audit_id"), r.text
        log(f"{u}: 403 not_memory_admin, audited as {r.json()['audit_id']}")
    mem("carol", "GET", "candidates")
    log("carol (replicant-admins): 200")
    # leftovers of aborted earlier runs (the fixtures' gate-comment and repo-scan statements) would absorb this run's
    # candidates as evidence; real entries of the same source kinds are left alone
    fixture = fixture_statements()
    old = sorted({i for k in ("gate_comment", "import") for i in mem("carol", "POST", "quarantines", {
        "selector": {"tenant_id": "payments", "source_kind": k}, "dry_run": True})["entries"]
        if (entry(i).get("statement") or "") in fixture})
    old += [x["entry_id"] for x in mem("carol", "GET", "entries?tenant_id=payments&limit=500")["items"]
            if x["state"] != "purged" and (x.get("statement") or "").startswith(RULE_PREFIX)]  # step-1 rules of aborted runs
    if old:
        mem("carol", "POST", "purges", {"entry_ids": old, "reason_code": "curator", "reason_text": "e2e06 cleanup"})
        log(f"purged {len(old)} entries left by earlier runs")

    # ---- 1. admin-authored rule reaches the agent; disable drops it at the next node, snapshot unchanged
    stmt = f"{RULE_PREFIX} ({MARK})."
    rule = mem("carol", "POST", "entries", {"tenant_id": "payments", "memory_type": "rule", "scope_level": "repo",
                                            "repo": REPO, "title": "Minor units and table tests", "statement": stmt},
               expect=201)
    AUTHORED.append(rule["entry_id"])
    ref = f"{rule['entry_id']}@1"
    log(f"carol authored {ref} (active)")
    reader = e.api("grace")
    job_a = start_job()
    deadline = time.monotonic() + JOB_TIMEOUT_S
    job = e.wait_until(reader, job_a, lambda s: any(r.get("node_key") == "implement" for r in s.get("runs", [])),
                       "implement run", deadline)
    snap_a = e.snapshot(job)["memory_snapshot_id"]
    first = run_inputs(job_a)[0]
    assert ref in first["memory_entries"] and stmt.split(" (")[0][:40] in first["rendered"], first
    assert '<replicant-memory format="replicant.memory-block/v1"' in first["rendered"]
    log(f"job A {job_a}: first run ({first['role']}) received {ref} in memory-injection.json; snapshot {snap_a}")
    act(rule["entry_id"], "disable", {"reason": "e2e06: wrong for this repo"})
    log("carol disabled the rule while job A runs")
    job = e.wait_until(reader, job_a, gate_open("publish_approval"), "A publish gate", deadline)
    s = e.snapshot(job)
    assert s["memory_snapshot_id"] == snap_a, (s["memory_snapshot_id"], snap_a)
    runs = run_inputs(job_a)
    later = [r for r in runs if r["role"] in ("reviewer", "qa", "implementer")][-1]
    assert ref not in later["memory_entries"] and stmt[:40] not in later["rendered"], later
    log(f"later run ({later['role']}) has no {ref}; snapshot ID unchanged ({snap_a})")
    view = job_memory(job_a)
    assert view["available"] and ref in [x["ref"] for x in view["entries"]], view
    assert "author" not in json.dumps(view["entries"])
    log("dave (viewer) sees Memory used for job A, without author identity")

    # ---- 2. gate request_changes comment -> candidate; approve -> next job
    g1 = gate_open("publish_approval")(s)
    comment = f"e2e06-gate-rule: please wrap the errors returned from ApplyRefund with fmt.Errorf and %w ({MARK})"
    decide(g1, "request_changes", comment)
    cand = e.wait_for(lambda: [x for x in candidates(job_id=job_a) if x.get("source_kind") == "gate_comment"],
                      "gate comment candidate", timeout=180)[0]
    det = entry(cand["entry_id"])
    p = det["provenance"][0]
    assert p["source_kind"] == "gate_comment" and p["job_id"] == job_a and MARK in (p["source_excerpt"] or ""), p
    assert det["scope_level"] == "path" and det["state"] == "candidate", det
    log(f"candidate {cand['entry_id']} from bob's comment: provenance gate_comment, job {job_a}, "
        f"extracted by {p['extraction_model']}")
    job = e.wait_until(reader, job_a, gate_open("publish_approval", g1["gate_id"]), "A second publish gate", deadline)
    g2 = gate_open("publish_approval", g1["gate_id"])(e.snapshot(job))
    decide(g2, "reject", f"e2e06-gate-specific: rename this variable ({MARK})")
    e.wait_for(lambda: e.snapshot(e.get_job(reader, job_a)).get("status") in e.TERMINAL, "job A terminal", timeout=180)
    time.sleep(10)
    assert not [x for x in candidates(job_id=job_a) if "rename" in (x.get("statement") or "").lower()]
    log("the PR-specific reject comment produced no candidate")
    act(cand["entry_id"], "approve", {"reason": "e2e06: good rule"})
    log(f"carol approved {cand['entry_id']}")
    job_b = start_job()
    deadline = time.monotonic() + JOB_TIMEOUT_S
    job = e.wait_until(reader, job_b, gate_open("publish_approval"), "B publish gate", deadline)
    gref = f"{cand['entry_id']}@1"
    rb = run_inputs(job_b)
    assert any(gref in r["memory_entries"] for r in rb), rb
    log(f"job B {job_b}: runs received {gref}")
    decide(gate_open("publish_approval")(e.snapshot(job)), "approve", "e2e06: approved")
    pr = e.wait_pr(job_b, deadline)
    with e.gitea() as g:
        e.ok(g.patch(f"/repos/{e.ORG}/{e.REPO}/pulls/{pr['number']}", json={"state": "closed"}), "close PR")
    log(f"job B: PR #{pr['number']} closed unmerged")

    # ---- 4. repo scan
    e.ensure_claude_md("e2e06")  # reset_repo() went back to the initial commit, which has no CLAUDE.md
    with e.api("frank") as c:
        r = c.post("/v1/projects/prj_ledger/repos/1:scan", json={})
    assert r.status_code in (200, 201, 202), r.text[:300]
    job_c = r.json()["job_id"]
    JOBS.append(job_c)
    log(f"frank started repo scan {job_c}")
    job = e.wait_for(lambda: (lambda j: j if e.snapshot(j).get("status") in e.TERMINAL else None)(e.get_job(reader, job_c)),
                     "scan job terminal", timeout=JOB_TIMEOUT_S, every=5)
    sc = e.snapshot(job)
    assert sc["status"] == "succeeded" and (sc.get("terminal") or {}).get("outcome") == "no_change_needed", sc.get("terminal")
    scans = e.wait_for(lambda: [x for x in candidates(job_id=job_c)], "scan candidates", timeout=120)
    time.sleep(5)
    scans = candidates(job_id=job_c)
    assert len(scans) == 1 and all(x["origin"] == "import" and x["source_kind"] == "import" for x in scans), scans
    assert "doc comment" in scans[0]["statement"], scans[0]["statement"]
    assert "Inferred convention (seen in" in json.dumps(entry(scans[0]["entry_id"])), "label missing"
    env = scan_envelope(job_c)
    for p in env["proposals"]:
        v = p["verification"]
        assert not v["missing"] and v["sampled"] >= 3 and v["matched"] / v["sampled"] >= 0.7, v
    dropped = {n["reason"].split(":")[0].split(" in ")[0]: n for n in env["non_candidates"]}
    documented = [n for n in env["non_candidates"] if n.get("documented_in") == ["CLAUDE.md"]]
    missing = [n for n in env["non_candidates"] if "internal/ledger/fx.go" in (n.get("verification") or {}).get("missing", [])]
    assert len(documented) == 2 and len(missing) == 1, env["non_candidates"]
    log(f"scan {job_c}: 1 verified candidate ({env['proposals'][0]['verification']['matched']} of "
        f"{env['proposals'][0]['verification']['sampled']} files) - {scans[0]['statement'][:70]}; dropped "
        f"{len(documented)} documented in CLAUDE.md and 1 citing a missing file ({sorted(dropped)})")
    quoted = any('source="repo_instructions"' in r["first_message"] for r in run_inputs(job_c))
    log(f"repo CLAUDE.md quoted in the scan's first message: {quoted} (entrypoint quoting, PR-loop owner)")

    # ---- 5. quarantine dry run, purge, export, analytics
    bob = next(x["author_principal_id"] for x in det["provenance"])
    dry = mem("carol", "POST", "quarantines", {"selector": {"tenant_id": "payments", "author_principal_id": bob},
                                                "dry_run": True, "reason": "e2e06"})
    assert cand["entry_id"] in dry["entries"] and job_b in dry["jobs"], dry
    log(f"quarantine dry run by author: {dry['count']} entries, {len(dry['jobs'])} jobs")
    pdry = mem("carol", "POST", "purges", {"entry_ids": [rule["entry_id"]], "dry_run": True})
    assert job_a in pdry["jobs"] and snap_a in pdry["snapshots"], pdry
    pg = mem("carol", "POST", "purges", {"entry_ids": [rule["entry_id"]], "reason_code": "curator", "reason_text": "e2e06"})
    det = entry(rule["entry_id"])
    assert det["state"] == "purged" and det["statement"] is None, det
    va = job_memory(job_a)
    assert next(x for x in va["entries"] if x["ref"] == ref)["purged"] is True
    ex = mem("carol", "POST", "exports", {"tenant_id": "payments"})
    assert rule["entry_id"] not in [x["entry_id"] for x in ex["entries"]] and stmt not in json.dumps(ex)
    log(f"purge {pg['purge_ids'][0]}: tombstone, job A view shows it removed, export {ex['export_id']} excludes it")
    for rep in ("summary", "entries", "feedback"):
        mem("carol", "GET", f"analytics/{rep}")
    summ = mem("carol", "GET", "analytics/summary")
    log(f"analytics: {summ['outcomes']['jobs']} job outcomes recorded, injections {summ['injections']}")
    log(f"evidence: A {job_a}  B {job_b}  scan {job_c}  rule {rule['entry_id']}  gate candidate {cand['entry_id']}")
    trust_and_holds()
    log("E2E-06 memory: PASS")


def worker_mem(path: str, body: dict) -> dict:
    """POST to memory-service through the worker's own client (its caller token; nothing printed is secret)."""
    code = f"""
import asyncio, json
from replicant_worker.activities.memory import mem
print(json.dumps(asyncio.run(mem({path!r}, json.loads({json.dumps(body)!r})))))
"""
    r = subprocess.run([e.ENGINE, "exec", e.container("replicant-worker"), "python", "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _job_id() -> str:
    import random
    return "job_" + "".join(random.choices("0123456789abcdefghjkmnpqrstvwxyz", k=26))


def trust_and_holds() -> None:
    """7. MEM-D20..D13 on the live memory-service (synthetic PR-comment sources posted with the worker's client):
    the same reviewer repeating the same comment in several jobs stays ONE independent source; a PR-derived candidate
    is held and cannot be approved; a closed-unmerged PR auto-rejects its candidate; a merged one releases it."""
    author = "usr_" + "e2e06" + "0" * 21
    stmt = f"Use integer minor units for every money amount in ledger code ({MARK})."

    def comment(job: str, n: int, text: str = "Please use integer minor units, never floats, for money.") -> dict:
        return {"memory_type": "rule", "proposed_scope": {"level": "repo", "repo": REPO}, "statement": stmt,
                "source": {"kind": "pr_review_comment", "source_key": f"gh-review-comment:{job}:{n}", "text": text,
                           "author_principal_id": author, "author_repo_permission": "write", "pr_number": 900 + n,
                           "comment_id": n, "cycle": 1, "job_id": job,
                           "url": f"http://gitea:3300/acme-payments/ledger-api/pulls/{900 + n}"}}
    jobs = [_job_id() for _ in range(4)]
    JOBS.extend(jobs)
    eid = None
    for i, j in enumerate(jobs[:3]):  # the identical comment in three jobs
        r = worker_mem("/internal/v1/candidates", {"tenant_id": "payments", "job_id": j, "proposals": [comment(j, i + 1)]})
        eid = eid or r["entries"][0]["entry_id"]
    d = entry(eid)
    assert d["evidence_count"] == 3 and d["independent_evidence_count"] == 1 and d["distinct_author_count"] == 1, d
    assert d["confidence"] == 0.5, d["confidence"]
    q = next(x for x in candidates() if x["entry_id"] == eid)
    assert q["independent_evidence_count"] == 1, q
    log(f"repeat: 3 identical comments by one author -> {eid}: evidence 3, 1 independent source (1 author), "
        f"confidence {d['confidence']}")
    assert d["activation_hold"] and d["pr_outcome"] == "awaiting_merge", d
    mem("carol", "POST", f"entries/{eid}:approve", {"reason": "e2e06"}, if_match=d["state_seq"], expect=409)
    out = worker_mem("/internal/v1/outcomes", {"tenant_id": "payments", "job_id": jobs[0], "repo": REPO,
                                              "terminal_status": "succeeded", "outcome": "pr_merged", "fix_retained": True})
    assert out["holds"] and out["holds"][0]["result"] == "released", out
    d = entry(eid)
    assert not d["activation_hold"] and d["pr_outcome"] == "merged_with_fix", d
    log(f"hold: {eid} was held awaiting_merge (approve 409) and released when {jobs[0]} merged with the fix")
    j = jobs[3]
    r = worker_mem("/internal/v1/candidates", {"tenant_id": "payments", "job_id": j, "proposals": [{
        **comment(j, 9, "Return 404 for ledgers the caller cannot see."),
        "statement": f"Return 404, not 403, for ledgers the caller cannot see ({MARK})."}]})
    closed = r["entries"][0]["entry_id"]
    worker_mem("/internal/v1/outcomes", {"tenant_id": "payments", "job_id": j, "repo": REPO,
                                        "terminal_status": "rejected", "outcome": "pr_closed_unmerged"})
    d = entry(closed)
    assert d["state"] == "rejected" and d["pr_outcome"] == "closed_unmerged", d
    assert d["transitions"][-1]["reason_code"] == "pr_closed_unmerged", d["transitions"]
    log(f"closed: {closed} auto-rejected when {j}'s PR closed unmerged (system_outcome, pr_closed_unmerged)")


def fixture_statements() -> set[str]:
    """Statements the scripted gateway produces (memory_extraction.yaml, scan.yaml): only these are test leftovers."""
    import re
    d = e.ROOT / "tests" / "fixtures" / "gateway-scripts" / "walkthrough-ledger-api"
    out: set[str] = set()
    for f in ("memory_extraction.yaml", "scan.yaml"):
        out |= {s.encode().decode("unicode_escape") for s in re.findall(r'"statement": "((?:[^"\\]|\\.)*)"', (d / f).read_text())}
    return out


def scan_envelope(job_id: str) -> dict:
    """The scan job's extracted memory-candidates envelope (verified proposals and dropped non-candidates)."""
    code = f"""
import json
from replicant_worker.activities import s3
from replicant_worker.activities.config import settings
c = s3.client(); b = settings().bucket('artifacts')
pre = 'tenants/payments/jobs/{job_id}/steps/'
keys = [o['Key'] for p in c.get_paginator('list_objects_v2').paginate(Bucket=b, Prefix=pre) for o in p.get('Contents', [])
        if o['Key'].endswith('/memory_candidates/extracted.json')]
print(json.dumps(json.loads(c.get_object(Bucket=b, Key=keys[0])['Body'].read())))
"""
    r = subprocess.run([e.ENGINE, "exec", e.container("replicant-worker"), "python", "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    return json.loads(r.stdout.strip().splitlines()[-1])


JOBS: list[str] = []


def test_e2e06_memory() -> None:
    run()


def run() -> None:
    try:
        main()
    finally:  # (6) never leave test candidates or entries in the admin queue, pass or fail
        e.cleanup_memory(JOBS, "e2e06")
        left = [i for i in AUTHORED if entry(i).get("state") != "purged"]
        if left:
            mem("carol", "POST", "purges", {"entry_ids": left, "reason_code": "curator",
                                            "reason_text": "e2e06: test artifact (E2E fixture) cleanup"})


if __name__ == "__main__":
    run()
