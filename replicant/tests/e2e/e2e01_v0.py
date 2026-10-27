"""E2E-01-v0 driver (spec/11-local-dev/test-strategy.md §6, v0 settings from plan §1.6 X0-2).

Bugfix on acme-payments/ledger-api from a Gitea issue labelled `replicant`, with the v0 gate and repair settings
(no plan gate, no repair cycle, gateway `script`):
  1. open the issue (ISSUES/001.md) as alice with label `replicant`
  2. ack comment by replicant-bot within 60 s (carries the job_id)
  3. poll GET /v1/jobs/{job_id} until the publish gate is open (implementer, QA, reviewer runs Succeeded)
  4. approve through the API (POST /v1/gates/{gate_id}/decisions) as an approver
  5. a PR is opened by replicant-bot; merge it through the Gitea API
  6. the job ends `succeeded`, and the projection (GET /v1/jobs/{id}) equals the workflow's `job.snapshot` Query
Public APIs only (control-plane-api, Gitea). Humans are played with control-plane-api's local dev principals
(`X-Replicant-Dev-User`, CONTROL-02) and Gitea's admin `Sudo` header. Never prints a secret.

Run: `uv run --package replicant-e2e pytest -s tests/e2e/e2e01_v0.py`  or  `uv run python tests/e2e/e2e01_v0.py`
Env: REPLICANT_API_URL (http://127.0.0.1:8401), REPLICANT_GITEA_URL (http://127.0.0.1:3300),
     E2E_REQUESTER (alice), E2E_APPROVER (bob), E2E_READER (grace), E2E_JOB_TIMEOUT_S (1800).
"""
from __future__ import annotations

import os
import re
import sys
import time
import uuid
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[2]
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


def log(msg: str) -> None:
    print(f"[e2e01-v0 {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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
                      json={"title": title, "body": f"{body}\n\n<!-- e2e01-v0 run {run_tag} -->", "labels": [label_id]}),
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
    body = {"decision": "approve", "presented_digest": gate["presented_digest"], "comment": "e2e01-v0: approved"}
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
                   json={"Do": "merge", "merge_message_field": f"Merge #{pr['number']} (e2e01-v0)"})
        if r.status_code >= 300:  # fall back to the site admin (no branch protection in v0)
            with gitea() as ga:
                r = ok(ga.post(f"/repos/{ORG}/{REPO}/pulls/{pr['number']}/merge", json={"Do": "merge"}), "merge PR")
    log(f"merged PR #{pr['number']}")


COMPARED = ("job_id", "tenant_id", "workflow_type", "status", "phase", "release_bundle_id", "worker_deployment_version",
            "cycle")


def temporal_snapshot(job_id: str) -> dict:
    """Query `job.snapshot` (C-MSG) on the ReplicantJob through the Temporal CLI inside the temporal container."""
    import json
    import subprocess
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


def _ts(v: str | None):  # noqa: ANN202
    from datetime import datetime
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


def report(reader: httpx.Client, job_id: str, s: dict, pr: dict, t0: float) -> None:
    log(f"evidence: job {job_id}  PR {pr.get('html_url')}  bundle {s.get('release_bundle_id')}  "
        f"worker {s.get('worker_deployment_version')}  total {time.monotonic() - t0:.0f}s")
    for r in s.get("runs", []):
        a, b = _ts(r.get("started_at")), _ts(r.get("finished_at"))
        dur = f"{(b - a).total_seconds():.0f}s" if a and b else "?"
        log(f"  run {r.get('node_key'):10s} {r.get('role'):11s} {r.get('phase'):9s} {dur:>6s}  {r.get('run_id')}")
    tl = reader.get(f"/v1/jobs/{job_id}/timeline", params={"limit": 200})
    if tl.status_code == 200:
        items = tl.json().get("items", [])
        first = items[0].get("at") if items else None
        for ev in items:
            log(f"  timeline {ev.get('at')} {ev.get('kind') or ev.get('type')} {str(ev.get('summary') or '')[:80]}")
        if first:
            log(f"  first visible progress at {first}")


def gateway_mode() -> str:
    env = ROOT / "dev" / "compose" / ".env"
    for line in env.read_text().splitlines() if env.is_file() else []:
        if line.startswith("REPLICANT_GATEWAY_MODE="):
            return line.split("=", 1)[1].strip()
    return "script"


LOKI = os.environ.get("REPLICANT_LOKI_URL", "http://127.0.0.1:3100")
TEMPO = os.environ.get("REPLICANT_TEMPO_URL", "http://127.0.0.1:3200")
GRAFANA = os.environ.get("REPLICANT_GRAFANA_URL", "http://127.0.0.1:3001")


def observability_up(wait_s: float = 0) -> bool:
    deadline = time.monotonic() + wait_s
    while True:
        try:
            if httpx.get(f"{LOKI}/ready", timeout=3).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(3)


def loki_lines(job_id: str, org: str) -> list[dict]:
    """Lines of `{env="local"} | job_id="<job_id>"` in one Loki tenant, with their labels + structured metadata."""
    now = time.time_ns()
    r = ok(httpx.get(f"{LOKI}/loki/api/v1/query_range", headers={"X-Scope-OrgID": org}, timeout=30, params={
        "query": f'{{env="local"}} | job_id="{job_id}"', "start": now - 3 * 3600 * 10**9, "end": now,
        "limit": 5000, "direction": "forward"}), f"loki {org}")
    out = []
    for st in r.json()["data"]["result"]:
        for _ts, line, *meta in st["values"]:
            labels = dict(st["stream"])
            for m in meta:  # categorized labels, when the server returns them
                if isinstance(m, dict):
                    labels.update(m.get("structuredMetadata") or {})
            out.append({"labels": labels, "line": line})
    return out


SANDBOX_SERVICES = ("replicant-sandbox", "replicant-sandbox-uploader", "replicant-sandbox-agentd")


def _sandbox_coverage(sbx: list[dict], run_ids: list[str]) -> dict[str, set[str]]:
    cov: dict[str, set[str]] = {r: set() for r in run_ids}
    for ln in sbx:
        rid = ln["labels"].get("run_id")
        if rid in cov:
            cov[rid].add(ln["labels"].get("service", "?"))
    return cov


def assert_x0_8(job_id: str, timeout_s: float = 180, run_ids: list[str] | None = None) -> dict:
    """plan §1.6 X0-8: from the job_id alone, Loki returns lines and Tempo returns the trace, all with one trace ID;
    the Grafana `replicant-job` dashboard loads."""
    from replicant_ids import job_trace_id
    tid = job_trace_id(job_id)
    deadline = time.monotonic() + timeout_s
    while True:
        plat, sbx = loki_lines(job_id, "platform"), loki_lines(job_id, "sandbox")
        tr = httpx.get(f"{TEMPO}/api/traces/{tid}", timeout=30)
        spans = []
        if tr.status_code == 200:
            for b in tr.json().get("batches", tr.json().get("resourceSpans", [])):
                svc = next((a["value"].get("stringValue") for a in b.get("resource", {}).get("attributes", [])
                            if a["key"] == "service.name"), "?")
                for ss in b.get("scopeSpans", b.get("instrumentationLibrarySpans", [])):
                    for sp in ss.get("spans", []):
                        spans.append((svc, sp["name"], sp.get("traceId")))
        services = {s for s, _, _ in spans}
        cov = _sandbox_coverage(sbx, run_ids or [])
        sbx_run_spans = sum(1 for _, n, _ in spans if n == "sandbox.run")
        ready = plat and sbx and spans and {"control-plane-api", "replicant-worker", "runner-local",
                                            "replicant-gateway-shim", "replicant-entrypoint", "replicant-agentd"} <= services \
            and all(set(SANDBOX_SERVICES) <= v for v in cov.values()) and sbx_run_spans >= len(run_ids or [])
        if ready or time.monotonic() > deadline:
            break
        time.sleep(5)
    assert plat, f"Loki tenant platform has no line with job_id={job_id}"
    assert sbx, f"Loki tenant sandbox has no line with job_id={job_id}"
    lt = {ln["labels"].get("trace_id") for ln in plat + sbx}
    assert lt == {tid}, f"Loki lines carry trace ids {lt}, want {{{tid}}}"
    assert spans, f"Tempo has no trace {tid}"
    import base64
    def _hex(t: str) -> str:  # Tempo JSON may return base64 ids
        return t if len(t) == 32 else base64.b64decode(t).hex()
    assert {_hex(t) for _, _, t in spans} == {tid}, "Tempo spans outside the job trace"
    names = sorted({n for _, n, _ in spans})
    assert "job" in names, f"no root span `job` in {names}"
    # M3: every run's agent, uploader and agentd lines are in Loki (per-run flush), and every run exported its
    # sandbox.run span (T-5b span ID) plus an agentd span.
    for rid, svcs in cov.items():
        missing = set(SANDBOX_SERVICES) - svcs
        assert not missing, f"run {rid}: no sandbox lines from {sorted(missing)} (have {sorted(svcs)})"
    assert sbx_run_spans >= len(run_ids or []), f"{sbx_run_spans} sandbox.run spans for {len(run_ids or [])} runs"
    assert "agentd" in names, "no agentd span"
    g = ok(httpx.get(f"{GRAFANA}/api/dashboards/uid/replicant-job", timeout=30), "grafana replicant-job dashboard")
    dash = g.json()["dashboard"]
    assert dash["uid"] == "replicant-job" and any(p.get("title") == "Trace" for p in dash.get("panels", []))
    page = ok(httpx.get(f"{GRAFANA}/d/replicant-job", params={"var-job_id": job_id, "var-trace_id": tid}, timeout=30),
              "grafana dashboard page")
    svc_lines = {}
    for ln in plat + sbx:
        k = ln["labels"].get("service", "?")
        svc_lines[k] = svc_lines.get(k, 0) + 1
    ev = {"trace_id": tid, "loki_platform_lines": len(plat), "loki_sandbox_lines": len(sbx),
          "sandbox_services_per_run": {r: sorted(v) for r, v in cov.items()}, "sandbox_run_spans": sbx_run_spans,
          "loki_lines_by_service": svc_lines, "tempo_spans": len(spans), "tempo_services": sorted(services),
          "tempo_span_names": names[:40], "grafana": f"{page.status_code} /d/replicant-job"}
    log(f"X0-8: {ev}")
    return ev


def run() -> str:
    run_tag = uuid.uuid4().hex[:8]
    deadline = time.monotonic() + JOB_TIMEOUT_S
    number, t0 = open_issue(run_tag)
    job_id = wait_ack(number, t0)
    reader = api(READER)
    job = wait_until(reader, job_id, lambda s: open_publish_gate(s) is not None, "publish gate", deadline)
    s = snapshot(job)
    assert s["tenant_id"] == TENANT and s["workflow_type"] == "bugfix", (s["tenant_id"], s["workflow_type"])
    assert s.get("release_bundle_id", "").startswith("rb_"), "job not pinned to a bundle (X0-6)"
    assert s.get("worker_deployment_version"), "job has no worker_deployment_version (X0-6)"
    roles = {r["role"]: r["phase"] for r in s.get("runs", [])}
    for role in ("implementer", "qa", "reviewer"):
        assert roles.get(role) == "Succeeded", f"{role} run not Succeeded before the gate: {roles}"
    assert not any(g.get("kind") == "plan_approval" for g in s.get("gates", [])), "v0 has no plan gate"
    approve(open_publish_gate(s))
    pr = wait_pr(job_id, deadline)
    assert pr["user"]["login"] == "replicant-bot", f"PR opened by {pr['user']['login']}, want replicant-bot"
    log(f"PR #{pr['number']} by replicant-bot: {pr['title']}")
    with gitea() as g:
        files = ok(g.get(f"/repos/{ORG}/{REPO}/pulls/{pr['number']}/files"), "pr files").json()
    if gateway_mode() == "script":  # the canned implementer applies the known one-line fix
        assert any(f["filename"] == "internal/ledger/ledger.go" for f in files), [f["filename"] for f in files]
    else:  # passthrough: real Claude writes its own fix; QA (install/unit/build) already passed on it
        assert files, "PR has no changed files"
        log(f"PR files: {[f['filename'] for f in files]}")
    merge(pr)
    job = wait_until(reader, job_id, lambda s: s.get("status") == "succeeded", "job succeeded", deadline)
    s = snapshot(job)
    assert (s.get("pull_request") or {}).get("state") == "merged", s.get("pull_request")
    assert s.get("cycle", 0) == 0, "v0 runs no repair cycle"
    assert_projection_matches(reader, job_id)
    if observability_up(wait_s=90 if os.environ.get("E2E_REQUIRE_X08") == "1" else 5):
        assert_x0_8(job_id, run_ids=[r["run_id"] for r in s.get("runs", [])])
    elif os.environ.get("E2E_REQUIRE_X08") == "1":
        raise AssertionError("observability profile not up (Loki not ready) but E2E_REQUIRE_X08=1")
    report(reader, job_id, s, pr, t0)
    log(f"PASS: {job_id} succeeded (issue #{number}, PR #{pr['number']})")
    return job_id


def test_e2e01_v0() -> None:
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        log(f"FAIL: {e}")
        sys.exit(1)
