"""Shared helpers for the E2E drivers (tests/e2e/e2e*.py), the chaos cases (tests/chaos) and the web live E2E
(web/control-plane-web/e2e/live_gate.py): Gitea issues, job polling, gate approval, PR merge, projection check and
the sample-repo reset. Installed into the workspace venv by the replicant-e2e package (`uv run --package
replicant-e2e ...`), so drivers import it directly. Public APIs only; never prints a secret.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[3]
SECRETS = ROOT / "dev" / ".secrets" / "secrets"
API = os.environ.get("REPLICANT_API_URL", "http://127.0.0.1:8401")
GITEA = os.environ.get("REPLICANT_GITEA_URL", "http://127.0.0.1:3300")
ORG, REPO, TENANT = "acme-payments", "ledger-api", "payments"
REQUESTER = os.environ.get("E2E_REQUESTER", "alice")
APPROVER = os.environ.get("E2E_APPROVER", "bob")
READER = os.environ.get("E2E_READER", "grace")
ACK_TIMEOUT_S = 60
JOB_TIMEOUT_S = float(os.environ.get("E2E_JOB_TIMEOUT_S", "1800"))
JOB_RE = re.compile(r"\bjob_[0-9a-hjkmnp-tv-z]{26}\b")
TERMINAL = {"succeeded", "failed", "cancelled", "rejected", "expired", "taken_over"}




TAG = "e2e"  # log prefix; drivers set replicant_e2e.TAG


def log(msg: str) -> None:
    print(f"[{TAG} {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def secret(name: str) -> str:
    return (SECRETS / name).read_text().strip()


def gitea(sudo: str | None = None) -> httpx.Client:
    h = {"Sudo": sudo} if sudo else {}
    return httpx.Client(base_url=f"{GITEA}/api/v1", auth=("gitea-admin", secret("gitea_admin_password")), headers=h,
                        timeout=30)


def api(user: str) -> httpx.Client:
    return httpx.Client(base_url=API, headers={"X-Replicant-Dev-User": user}, timeout=30)


def ok(r: httpx.Response, what: str) -> httpx.Response:
    if r.status_code >= 300:
        raise AssertionError(f"{what}: HTTP {r.status_code} {r.text[:400]}")
    return r


def issue_text() -> tuple[str, str]:
    raw = (ROOT / "dev" / "sample-repos" / REPO / "ISSUES" / "001.md").read_text()
    _, fm, body = raw.split("---", 2)
    return yaml.safe_load(fm)["title"], body.strip()


def open_issue(run_tag: str) -> tuple[int, float]:
    title, body = issue_text()
    with gitea() as g:
        labels = ok(g.get(f"/repos/{ORG}/{REPO}/labels", params={"limit": 50}), "labels").json()
        label_id = next(lb["id"] for lb in labels if lb["name"] == "replicant")
    with gitea(sudo=REQUESTER) as g:
        t0 = time.monotonic()
        r = ok(g.post(f"/repos/{ORG}/{REPO}/issues",
                      json={"title": title, "body": f"{body}\n\n<!-- {TAG} run {run_tag} -->", "labels": [label_id]}),
               "create issue")
    n = r.json()["number"]
    log(f"opened issue #{n} as {REQUESTER} with label replicant")
    return n, t0


def wait_ack(number: int, t0: float) -> str:
    with gitea() as g:
        while time.monotonic() - t0 < ACK_TIMEOUT_S:
            comments = ok(g.get(f"/repos/{ORG}/{REPO}/issues/{number}/comments"), "comments").json()
            for c in comments:
                if c["user"]["login"] == "replicant-bot":
                    m = JOB_RE.search(c["body"])
                    assert m, f"ack comment has no job_id: {c['body'][:200]}"
                    log(f"ack after {time.monotonic() - t0:.1f}s: {m.group(0)}")
                    return m.group(0)
            time.sleep(1)
    raise AssertionError(f"no ack comment by replicant-bot within {ACK_TIMEOUT_S}s")


def get_job(c: httpx.Client, job_id: str) -> dict:
    r = c.get(f"/v1/jobs/{job_id}")
    if r.status_code == 404:
        return {}
    return ok(r, "GET job").json()


def snapshot(job: dict) -> dict:
    return job.get("snapshot") or {}


def wait_until(c: httpx.Client, job_id: str, pred, what: str, deadline: float) -> dict:  # noqa: ANN001
    last = None
    while time.monotonic() < deadline:
        job = get_job(c, job_id)
        s = snapshot(job)
        cur = (s.get("status"), s.get("phase"), tuple((r.get("node_key"), r.get("phase")) for r in s.get("runs", [])))
        if cur != last:
            log(f"job status={cur[0]} phase={cur[1]} runs={list(cur[2])}")
            last = cur
        if s and pred(s):
            return job
        if s.get("status") in TERMINAL:
            raise AssertionError(f"job reached {s.get('status')} while waiting for {what}: terminal={s.get('terminal')}")
        time.sleep(3)
    raise AssertionError(f"timeout waiting for {what}")


def open_publish_gate(s: dict) -> dict | None:
    for g in s.get("gates", []):
        if g.get("kind") == "publish_approval" and g.get("state") == "open":
            return g
    return None


def approve(gate: dict) -> None:
    body = {"decision": "approve", "presented_digest": gate["presented_digest"], "comment": f"{TAG}: approved"}
    with api(APPROVER) as c:
        r = ok(c.post(f"/v1/gates/{gate['gate_id']}/decisions", json=body,
                      headers={"Idempotency-Key": f'"{uuid.uuid4().hex}"'}), "decide gate")
    res = r.json() if r.content else {}
    log(f"gate {gate['gate_id']} approve -> HTTP {r.status_code} {res.get('outcome') or res.get('state') or ''}")
    if r.status_code == 200:
        assert res.get("outcome", "accepted") in ("accepted", "duplicate"), res


def wait_pr(job_id: str, deadline: float) -> dict:
    with gitea() as g:
        while time.monotonic() < deadline:
            prs = ok(g.get(f"/repos/{ORG}/{REPO}/pulls", params={"state": "open", "limit": 50}), "pulls").json()
            for pr in prs:
                if job_id in (pr.get("head") or {}).get("ref", "") or job_id in (pr.get("body") or ""):
                    return pr
            time.sleep(3)
    raise AssertionError("no PR for the job")


def merge(pr: dict) -> None:
    with gitea(sudo=REQUESTER) as g:
        r = g.post(f"/repos/{ORG}/{REPO}/pulls/{pr['number']}/merge",
                   json={"Do": "merge", "merge_message_field": f"Merge #{pr['number']} ({TAG})"})
        if r.status_code >= 300:  # fall back to the site admin (no branch protection in v0)
            with gitea() as ga:
                r = ok(ga.post(f"/repos/{ORG}/{REPO}/pulls/{pr['number']}/merge", json={"Do": "merge"}), "merge PR")
    log(f"merged PR #{pr['number']}")


COMPARED = ("job_id", "tenant_id", "workflow_type", "status", "phase", "release_bundle_id", "worker_deployment_version",
            "cycle")


def temporal_snapshot(job_id: str) -> dict:
    """Query `job.snapshot` (C-MSG) on the ReplicantJob through the Temporal CLI inside the temporal container."""
    name = subprocess.run(["podman", "ps", "-q", "--filter", "label=io.podman.compose.project=replicant",
                           "--filter", "label=com.docker.compose.service=temporal"], capture_output=True, text=True)
    cid = name.stdout.split()[0]
    r = subprocess.run(["podman", "exec", cid, "temporal", "workflow", "query", "--address", "127.0.0.1:7233",
                        "-n", "replicant-local", "-w", job_id, "--type", "job.snapshot", "-o", "json"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"temporal query failed: {r.stderr[-300:]}"
    out = json.loads(r.stdout)
    res = out.get("queryResult", out)
    return res[0] if isinstance(res, list) else res


def _norm(s: dict) -> dict:
    d = {k: s.get(k) for k in COMPARED}
    d["runs"] = sorted((r.get("run_id"), r.get("node_key"), r.get("phase")) for r in s.get("runs", []))
    d["gates"] = sorted((g.get("gate_id"), g.get("kind"), g.get("state")) for g in s.get("gates", []))
    d["pull_request"] = {k: (s.get("pull_request") or {}).get(k) for k in ("number", "state")}
    d["terminal_outcome"] = (s.get("terminal") or {}).get("outcome")
    return d


def assert_projection_matches(reader: httpx.Client, job_id: str, timeout_s: float = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        want = temporal_snapshot(job_id)
        got = snapshot(get_job(reader, job_id))
        if got.get("seq") == want.get("seq") and _norm(got) == _norm(want):
            log(f"projection == job.snapshot at seq {want.get('seq')}")
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"projection differs from job.snapshot: seq {got.get('seq')} vs {want.get('seq')}\n"
                                 f"projection={_norm(got)}\nsnapshot={_norm(want)}")
        time.sleep(2)


def gateway_mode() -> str:
    env = ROOT / "dev" / "compose" / ".env"
    for line in env.read_text().splitlines() if env.is_file() else []:
        if line.startswith("REPLICANT_GATEWAY_MODE="):
            return line.split("=", 1)[1].strip()
    return "script"


def wait_for(pred, what: str, timeout: float = 90, every: float = 2):  # noqa: ANN001, ANN201
    """Poll `pred` until it returns something truthy (returned) or the deadline passes (AssertionError)."""
    deadline = time.monotonic() + timeout
    while True:
        r = pred()
        if r:
            return r
        if time.monotonic() >= deadline:
            raise AssertionError(f"timeout after {timeout:.0f}s: {what}")
        time.sleep(every)


def never(pred, what: str, duration: float, every: float = 2) -> None:  # noqa: ANN001
    """Negative deadline poll: fail as soon as `pred` holds; pass when `duration` elapses without it holding."""
    deadline = time.monotonic() + duration
    while True:
        r = pred()
        if r:
            raise AssertionError(f"{what}: {r!r}"[:400])
        if time.monotonic() >= deadline:
            return
        time.sleep(every)


def open_jobs() -> list[dict]:
    with api(READER) as c:
        r = ok(c.get("/v1/jobs", params={"status": "queued,running,waiting_human,waiting_external,paused",
                                         "limit": 50}), "jobs")
    return r.json()["items"]


def reset_repo() -> str:
    """Force main of acme-payments/ledger-api back to its initial commit (the planted bug), so each case is a real
    bugfix job on a stack that already merged a fix. Credentials go through GIT_CONFIG_* env, never argv."""
    auth = base64.b64encode(f"gitea-admin:{secret('gitea_admin_password')}".encode()).decode()
    env = {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
           "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}", "GIT_TERMINAL_PROMPT": "0"}
    url = f"{GITEA}/acme-payments/ledger-api.git"
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "clone", "-q", url, d], env=env, check=True, capture_output=True)
        root = subprocess.run(["git", "-C", d, "rev-list", "--max-parents=0", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.split()[0]
        subprocess.run(["git", "-C", d, "push", "-q", "--force", "origin", f"{root}:refs/heads/main"], env=env,
                       check=True, capture_output=True)
    return root


# ---------------------------------------------------------------------------------------------- memory admin (carol)
MEMORY_ADMIN = os.environ.get("E2E_MEMORY_ADMIN", "carol")


def memory_admin(method: str, path: str, body: dict | None = None, if_match: int | None = None,
                 expect: int | tuple[int, ...] = 200) -> dict:
    """/v1/admin/memory/<path> as the memory admin, with an Idempotency-Key (and If-Match for state changes)."""
    import uuid
    h = {"Idempotency-Key": f"{TAG}-{uuid.uuid4().hex}"}
    if if_match is not None:
        h["If-Match"] = str(if_match)
    with api(MEMORY_ADMIN) as c:
        r = c.request(method, f"/v1/admin/memory/{path}", json=body, headers=h)
    want = expect if isinstance(expect, tuple) else (expect,)
    assert r.status_code in want, (method, path, r.status_code, r.text[:400])
    return r.json() if r.content else {}


def memory_candidates(job_id: str) -> list[dict]:
    return memory_admin("GET", f"candidates?tenant_id=payments&job_id={job_id}&limit=200")["items"]


def memory_entry(entry_id: str) -> dict:
    return memory_admin("GET", f"entries/{entry_id}")


def cleanup_memory(job_ids: list[str], reason: str) -> list[str]:
    """Purge the memory entries (candidates and approved ones) that came only from these test jobs, so E2E runs leave
    no test data in the admin queue. An entry that also has evidence from other jobs is left alone (logged)."""
    jobs = {j for j in job_ids if j}
    found: dict[str, dict] = {}
    for j in sorted(jobs):
        try:
            for it in memory_admin("GET", f"entries?tenant_id=payments&job_id={j}&limit=500")["items"]:
                if it.get("state") != "purged":
                    found[it["entry_id"]] = it
        except AssertionError as ex:
            log(f"memory cleanup: cannot list entries of {j}: {ex}")
    purge, kept = [], []
    for eid in found:
        prov_jobs = {p.get("job_id") for p in memory_entry(eid).get("provenance") or []}
        (purge if prov_jobs and prov_jobs <= jobs else kept).append(eid)
    if purge:
        memory_admin("POST", "purges", {"entry_ids": purge, "reason_code": "curator",
                                        "reason_text": f"{reason}: test artifact (E2E fixture) cleanup"})
    log(f"memory cleanup: purged {len(purge)} test entries" + (f", kept {kept} (evidence from other jobs)" if kept else ""))
    return purge


def ensure_claude_md(tag: str) -> None:
    """The sample repo ships CLAUDE.md, but reset_repo() goes back to the initial commit, which has none on older
    stacks: make sure main has it (an ordinary commit by the admin)."""
    text = (ROOT / "dev" / "sample-repos" / REPO / "CLAUDE.md").read_text()
    with gitea() as g:
        r = g.get(f"/repos/{ORG}/{REPO}/contents/CLAUDE.md", params={"ref": "main"})
        body = {"content": base64.b64encode(text.encode()).decode(), "message": f"Add CLAUDE.md ({tag})",
                "branch": "main"}
        if r.status_code == 200:
            if base64.b64decode(r.json()["content"]).decode() == text:
                return
            ok(g.put(f"/repos/{ORG}/{REPO}/contents/CLAUDE.md", json={**body, "sha": r.json()["sha"]}), "update CLAUDE.md")
        else:
            ok(g.post(f"/repos/{ORG}/{REPO}/contents/CLAUDE.md", json=body), "create CLAUDE.md")
    log("main has CLAUDE.md")
