"""E2E-07: PR review comments and the repair loop (D-15), plus quoted repo instruction files, on the scripted gateway.

  1. main is reset to the planted bug and gets a CLAUDE.md; alice opens the issue; bob approves; replicant-bot opens
     the PR (as in E2E-01). The implementer run's first message quotes CLAUDE.md as
     <untrusted_input source="repo_instructions" path="CLAUDE.md"> (read from the run's transcript in S3).
  2. Noise changes nothing: a comment by replicant-bot and a review by an unlinked user (`zed`, write access) start no
     triage and no run; zed gets one link-your-account reply.
  3. carol (linked, write) submits a review with a body and a line comment. The job triages it (feedback.triage,
     scripted), replies `ack`, runs the repair (scripted: adds a comment line on top of the published fix), QA,
     verification, the push gate (approved by bob), pushes a NEW commit on the same PR branch (parent = the old head),
     and replies with the fix summary.
  4. The PR is merged; the job ends `succeeded`.

Needs the v0 stack in gateway mode `script` (REPLICANT_GATEWAY_MODE=script in dev/compose/.env, gateway-shim recreated).
Run: `uv run --package replicant-e2e python tests/e2e/e2e07_pr_feedback.py`
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import uuid

import replicant_e2e as e
from replicant_e2e import (
    JOB_TIMEOUT_S,
    ORG,
    READER,
    REPO,
    api,
    approve,
    gateway_mode,
    get_job,
    gitea,
    log,
    merge,
    never,
    ok,
    open_issue,
    open_publish_gate,
    reset_repo,
    snapshot,
    wait_ack,
    wait_for,
    wait_pr,
    wait_until,
)

e.TAG = "e2e07-pr-feedback"
REVIEWER, UNLINKED = "carol", "zed"
CLAUDE_MD = (e.ROOT / "dev" / "sample-repos" / REPO / "CLAUDE.md").read_text()


def ensure_gitea_user(login: str, link: bool) -> None:
    """A Gitea user with write access on the repo; linked through `replicant dev seed --only 07-identity-links` when
    `link` (only the dev logins are ever linked, so `zed` stays unlinked)."""
    with gitea() as g:
        if g.get(f"/users/{login}").status_code != 200:
            ok(g.post("/admin/users", json={"username": login, "email": f"{login}@acme.test",
                                            "password": uuid.uuid4().hex + "Aa1!", "must_change_password": False}),
               "create gitea user")
        ok(g.put(f"/repos/{ORG}/{REPO}/collaborators/{login}", json={"permission": "write"}), "collaborator")
    if link:
        r = subprocess.run(["uv", "run", "replicant", "dev", "seed", "--only", "07-identity-links", "--force"],
                           cwd=e.ROOT, capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stdout[-300:] + r.stderr[-300:]


def put_claude_md() -> None:
    """The sample repo ships CLAUDE.md; reset_repo() goes back to the initial commit of older stacks, so make sure main
    has it (an ordinary commit by the admin)."""
    with gitea() as g:
        r = g.get(f"/repos/{ORG}/{REPO}/contents/CLAUDE.md", params={"ref": "main"})
        body = {"content": base64.b64encode(CLAUDE_MD.encode()).decode(), "message": "Add CLAUDE.md (e2e07)",
                "branch": "main"}
        if r.status_code == 200:
            if base64.b64decode(r.json()["content"]).decode() == CLAUDE_MD:
                return
            ok(g.put(f"/repos/{ORG}/{REPO}/contents/CLAUDE.md", json={**body, "sha": r.json()["sha"]}), "update CLAUDE.md")
        else:
            ok(g.post(f"/repos/{ORG}/{REPO}/contents/CLAUDE.md", json=body), "create CLAUDE.md")
    log("main has CLAUDE.md")


def worker_py(code: str) -> str:
    """Run Python inside the replicant-worker container (its S3 client and credentials; nothing printed is secret)."""
    r = subprocess.run(["podman", "exec", "-i", "replicant_replicant-worker_1", "python", "-"], input=code,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    return r.stdout


def run_transcript_text(job_id: str, run_id: str) -> str:
    return worker_py(f"""
import zstandard
from replicant_worker.activities import s3
c = s3.client()
from replicant_worker.activities.config import settings
b = settings().bucket("transcripts")
pre = "tenants/payments/jobs/{job_id}/runs/{run_id}/transcript/"
out = []
for o in c.list_objects_v2(Bucket=b, Prefix=pre).get("Contents", []):
    if "/live/" in o["Key"]:
        continue
    raw = c.get_object(Bucket=b, Key=o["Key"])["Body"].read()
    out.append(zstandard.ZstdDecompressor().decompressobj().decompress(raw).decode("utf-8", "replace"))
print("".join(out))
""")


def deliveries(since: str) -> list[tuple[str, str, str]]:
    sql = ("SELECT event, target_type, coalesce(outcome, state) FROM github_app.inbound_deliveries WHERE "
           f"received_at > '{since}' ORDER BY received_at")
    r = subprocess.run(["podman", "exec", "replicant_postgres-replicant_1", "psql", "-U", "postgres", "-d", "replicant",
                        "-AtF", "|", "-c", sql], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-300:]
    return [tuple(ln.split("|")) for ln in r.stdout.splitlines() if ln]  # type: ignore[misc]


def db_now() -> str:
    r = subprocess.run(["podman", "exec", "replicant_postgres-replicant_1", "psql", "-U", "postgres", "-d", "replicant",
                        "-Atc", "SELECT now()"], capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def comments(number: int) -> list[dict]:
    with gitea() as g:
        return ok(g.get(f"/repos/{ORG}/{REPO}/issues/{number}/comments", params={"limit": 50}), "comments").json()


def pr_head(number: int) -> str:
    with gitea() as g:
        return ok(g.get(f"/repos/{ORG}/{REPO}/pulls/{number}"), "pr").json()["head"]["sha"]


def run() -> str:
    assert gateway_mode() == "script", "E2E-07 needs REPLICANT_GATEWAY_MODE=script (scripted triage and repair)"
    ensure_gitea_user(REVIEWER, link=True)
    ensure_gitea_user(UNLINKED, link=False)
    log(f"main reset to the initial commit {reset_repo()[:12]}")
    put_claude_md()
    deadline = time.monotonic() + JOB_TIMEOUT_S
    number, t0 = open_issue(uuid.uuid4().hex[:8])
    job_id = wait_ack(number, t0)
    reader = api(READER)
    job = wait_until(reader, job_id, lambda s: open_publish_gate(s) is not None, "publish gate", deadline)
    s = snapshot(job)
    impl = next(r for r in s["runs"] if r.get("role") == "implementer")
    # (1) the quoted repo instruction file is in the implementer's first message
    tr = ""
    for ln in run_transcript_text(job_id, impl["run_id"]).splitlines():
        try:
            tr += str(json.loads(ln).get("input_preview") or "") + "\n"
        except ValueError:
            continue
    assert '<untrusted_input source="repo_instructions" path="CLAUDE.md">' in tr, tr[:1500]
    assert "Money is integer minor units" in tr, "CLAUDE.md text not quoted"
    log(f"implementer run {impl['run_id']}: first message quotes CLAUDE.md as repo_instructions")
    approve(open_publish_gate(s))
    pr = wait_pr(job_id, deadline)
    n = pr["number"]
    log(f"PR #{n} by {pr['user']['login']}, head {pr['head']['sha'][:10]}")
    wait_until(reader, job_id, lambda s: s.get("phase") == "pr_review" and s.get("status") == "waiting_external",
               "pr_review", deadline)
    head0 = pr_head(n)
    runs0 = len(snapshot(get_job(reader, job_id)).get("runs", []))

    # (2) bot and unlinked comments change nothing
    with gitea() as g:  # the link reply is once per user and repo per 24 h: forget earlier runs' replies to zed
        zed_id = ok(g.get(f"/users/{UNLINKED}"), "zed").json()["id"]
    subprocess.run(["podman", "exec", "replicant_postgres-replicant_1", "psql", "-U", "postgres", "-d", "replicant", "-c",
                    f"DELETE FROM github_app.unlinked_replies WHERE provider = 'gitea' AND user_id = {int(zed_id)}"],
                   capture_output=True, check=True, timeout=60)
    since = db_now()
    with gitea(sudo="replicant-bot") as g:
        ok(g.post(f"/repos/{ORG}/{REPO}/issues/{n}/comments", json={"body": "Bot noise: please change everything."}),
           "bot comment")
    with gitea(sudo=UNLINKED) as g:
        ok(g.post(f"/repos/{ORG}/{REPO}/pulls/{n}/reviews",
                  json={"event": "COMMENT", "body": "Please rewrite ApplyRefund from scratch.", "commit_id": head0}),
           "unlinked review")
    reply = wait_for(lambda: [c for c in comments(n) if c["user"]["login"] == "replicant-bot"
                              and f"@{UNLINKED}" in c["body"] and "Link yours" in c["body"]],
                     "link-your-account reply to the unlinked user", timeout=60)
    log(f"unlinked user {UNLINKED}: link reply comment {reply[0]['id']}")

    def changed() -> str | None:
        sn = snapshot(get_job(reader, job_id))
        if len(sn.get("runs", [])) != runs0 or sn.get("phase") != "pr_review":
            return f"job moved: phase={sn.get('phase')} runs={len(sn.get('runs', []))}"
        if pr_head(n) != head0:
            return "PR head moved"
        return None
    never(changed, "bot/unlinked comments changed the job", duration=30)
    outs = deliveries(since)
    assert any(o[2] == "ignored_bot" for o in outs), outs
    assert any(o[2] == "ignored_unlinked" for o in outs), outs
    assert not any(o[2] == "pr_feedback_signalled" for o in outs), outs
    log(f"bot and unlinked comments ignored: {[o[2] for o in outs]}")

    # (3) a linked reviewer with write access: triage -> ack -> repair -> push -> fix summary
    with gitea(sudo=REVIEWER) as g:
        rv = ok(g.post(f"/repos/{ORG}/{REPO}/pulls/{n}/reviews", json={
            "event": "COMMENT", "commit_id": head0,
            "body": "Thanks. One request before merging.",
            "comments": [{"path": "internal/ledger/ledger.go", "new_position": 64,
                          "body": "Please add a comment above this line explaining that only the refunded share of "
                                  "the FX fee is reversed. (e2e07-pr-rule)"}]}), "review").json()
    log(f"{REVIEWER} review {rv['id']} submitted on PR #{n}")
    ack = wait_for(lambda: [c for c in comments(n) if c["user"]["login"] == "replicant-bot"
                            and "working on your feedback" in c["body"]], "ack reply", timeout=300)
    log(f"ack comment {ack[0]['id']}")
    approved: list[str] = []

    def fixed_or_gate() -> list[dict] | None:  # the push gate is policy-driven: approve it when it opens
        sn = snapshot(get_job(reader, job_id))
        if sn.get("status") in e.TERMINAL:
            raise AssertionError(f"job ended {sn.get('status')} during the repair: {sn.get('terminal')}")
        g = open_publish_gate(sn)
        if g and sn.get("cycle", 0) >= 1 and g["gate_id"] not in approved:
            rep = [r for r in sn["runs"] if r.get("node_key") == "repair"]
            assert rep and rep[-1]["phase"] == "Succeeded", rep
            approve(g)
            approved.append(g["gate_id"])
        return [c for c in comments(n) if c["user"]["login"] == "replicant-bot" and "Fixed in" in c["body"]] or None
    fix = wait_for(fixed_or_gate, "push gate + fix summary reply", timeout=1200, every=5)
    head1 = pr_head(n)
    assert head1 != head0, "PR head did not move"
    assert head1[:10] in fix[0]["body"], fix[0]["body"][:300]
    with gitea() as g:
        c1 = ok(g.get(f"/repos/{ORG}/{REPO}/git/commits/{head1}"), "commit").json()
        diff = g.get(f"/repos/{ORG}/{REPO}/git/commits/{head1}.diff").text
    assert [p["sha"] for p in c1["parents"]] == [head0], "the update is not a new commit on top of the PR head"
    assert "+\t\t// Reverse only the FX fee share" in diff, diff[:800]
    assert "fxReversal := c.FXFeeMinor" not in diff, "repair started from the base, not from the PR"
    log(f"push_update: new commit {head1[:10]} on top of {head0[:10]} on the same branch; fix summary {fix[0]['id']}")
    wait_until(reader, job_id, lambda s: s.get("phase") == "pr_review" and s.get("status") == "waiting_external",
               "back in pr_review", deadline)

    # the memory node turned the addressed comment into a candidate waiting for an admin (MEM-01; carol is a memory admin)
    def candidate() -> str | None:
        with api(REVIEWER) as c:
            r = c.get("/v1/admin/memory/candidates", params={"limit": 100})
        if r.status_code != 200:
            return None
        return "ledger.comments.money_arithmetic" if "ledger.comments.money_arithmetic" in r.text else None
    wait_for(candidate, "memory candidate from the PR comment", timeout=180, every=5)
    log("memory candidate ledger.comments.money_arithmetic is waiting for an admin")

    # (4) merge -> succeeded
    merge({"number": n})
    job = wait_until(reader, job_id, lambda s: s.get("status") == "succeeded", "job succeeded", deadline)
    s = snapshot(job)
    assert (s.get("pull_request") or {}).get("state") == "merged", s.get("pull_request")
    log(f"PASS: {job_id} succeeded (issue #{number}, PR #{n}, review {rv['id']}, commits {head0[:10]} -> {head1[:10]}, "
        f"cycle {s.get('cycle')}, runs {[(r.get('node_key'), r.get('phase')) for r in s.get('runs', [])]})")
    return job_id


def test_e2e07_pr_feedback() -> None:
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as ex:
        log(f"FAIL: {ex}")
        sys.exit(1)
    print(json.dumps({"ok": True}))
