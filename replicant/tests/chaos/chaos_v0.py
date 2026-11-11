"""X0-7 chaos cases on the live v0 stack (spec/11-local-dev/test-strategy.md §7), each on a full E2E-01-v0 job.

  CH-WRK-01  kill replicant-worker while the implementer run is Running, start it again
  CH-RUN-01  kill runner-local while a run is Running, start it again
  CH-PG-01   pause postgres-replicant for 120 s while the implementer runs, then unpause

Every case must end with: the job `succeeded`; exactly one AgentRun per logical run (Runner API list by job, and
one pod group per run_id in the engine); no lost decision (the publish gate approval is recorded once, gate
`approved`); and a converged projection (GET /v1/jobs/{id} == the workflow's `job.snapshot`).

Run: uv run --package replicant-e2e python tests/chaos/chaos_v0.py [CH-WRK-01 CH-RUN-01 CH-PG-01]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

import replicant_e2e as e
from replicant_e2e import reset_repo

e.TAG = "chaos-v0"

RUNNER = "http://127.0.0.1:8480"


def engine(*args: str) -> str:
    r = subprocess.run([e.ENGINE, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{e.ENGINE} {' '.join(args)}: {r.stderr.strip()[-300:]}")
    return r.stdout


container = e.container


def wait_healthy(service: str, timeout_s: float = 300) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        st = json.loads(engine("inspect", container(service)))[0]["State"]
        h = (st.get("Health") or st.get("Healthcheck") or {}).get("Status", "")
        if st.get("Running") and h in ("healthy", ""):
            return
        time.sleep(3)
    raise AssertionError(f"{service} not healthy after restart")


def runner_runs(job_id: str) -> list[dict]:
    tok = e.secret("runner_worker_token")
    out, page = [], ""
    while True:
        r = httpx.get(f"{RUNNER}/v1/runs", params={"job_id": job_id, "page_size": 50, **({"page_token": page} if page else {})},
                      headers={"Authorization": f"Bearer {tok}"}, timeout=30)
        e.ok(r, "runner list")
        d = r.json()
        out += d.get("runs", [])
        page = d.get("next_page_token") or ""
        if not page:
            return out


def kill_and_restart(service: str, how: str = "kill") -> str:
    t0 = time.monotonic()
    engine("kill", "--signal", "KILL", container(service))
    time.sleep(5)
    engine("start", container(service))
    wait_healthy(service)
    return f"{service} killed and back healthy in {time.monotonic() - t0:.0f}s"


def pause_pg(seconds: int) -> str:
    engine("pause", container("postgres-replicant"))
    time.sleep(seconds)
    engine("unpause", container("postgres-replicant"))
    wait_healthy("postgres-replicant")
    return f"postgres-replicant paused {seconds}s"


INJECT = {
    "CH-WRK-01": ("implement", lambda: kill_and_restart("replicant-worker")),
    "CH-RUN-01": ("implement", lambda: kill_and_restart("runner-local")),
    "CH-PG-01": ("implement", lambda: pause_pg(120)),
}


def run_case(case: str) -> dict:
    node, inject = INJECT[case]
    e.log(f"=== {case}")
    e.log(f"main reset to the initial commit {reset_repo()[:12]}")
    deadline = time.monotonic() + 2400
    number, t0 = e.open_issue(f"{case}-{uuid.uuid4().hex[:6]}")
    job_id = e.wait_ack(number, t0)
    reader = e.api(e.READER)
    e.wait_until(reader, job_id, lambda s: any(r.get("node_key") == node and r.get("phase") == "Running"
                                               for r in s.get("runs", [])), f"{node} Running", deadline)
    time.sleep(3)
    what = inject()
    e.log(f"{case}: injected: {what}")
    job = e.wait_until(reader, job_id, lambda s: e.open_publish_gate(s) is not None, "publish gate", deadline)
    gate = e.open_publish_gate(e.snapshot(job))
    e.approve(gate)
    pr = e.wait_pr(job_id, deadline)
    assert pr["user"]["login"] == "replicant-bot"
    e.merge(pr)
    job = e.wait_until(reader, job_id, lambda s: s.get("status") == "succeeded", "job succeeded", deadline)
    s = e.snapshot(job)
    # one AgentRun per logical run
    runs = runner_runs(job_id)
    by_lai: dict[str, list[str]] = {}
    for r in runs:
        by_lai.setdefault(r["idempotency_key"].rsplit(":", 1)[-1], []).append(r["run_id"])
    dup = {k: v for k, v in by_lai.items() if len(v) != 1}
    assert not dup, f"more than one AgentRun per logical run: {dup}"
    assert len(runs) == len(s.get("runs", [])), f"runner has {len(runs)} runs, projection {len(s.get('runs', []))}"
    for r in runs:
        pods = [n for n in engine("ps", "-a", "--filter", f"label=replicant.example.com/run-id={r['run_id']}",
                                  "--format", "{{.Names}}").split() if n.endswith("-pause")]
        assert len(pods) <= 1, f"run {r['run_id']} has {len(pods)} pod groups"
    # no lost decision
    g = next(x for x in s["gates"] if x["gate_id"] == gate["gate_id"])
    assert g["state"] == "approved", g
    decisions = (g.get("decision_log") or [])
    assert len([d for d in decisions if d.get("outcome", "accepted") == "accepted"]) <= 1, decisions
    e.assert_projection_matches(reader, job_id, timeout_s=180)
    ev = {"case": case, "job_id": job_id, "injected": what, "runs": {r["idempotency_key"].rsplit(":", 1)[-1]: r["run_id"] for r in runs},
          "gate": g["state"], "pr": pr["number"], "total_s": round(time.monotonic() - t0)}
    e.log(f"PASS {case}: {ev}")
    return ev


def main(cases: list[str]) -> int:
    results, failed = [], []
    for c in cases or list(INJECT):
        try:
            results.append(run_case(c))
        except Exception as ex:  # noqa: BLE001
            e.log(f"FAIL {c}: {ex}")
            failed.append(c)
    (Path(__file__).parent / "results-v0.json").write_text(json.dumps({"passed": results, "failed": failed}, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
