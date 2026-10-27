"""Creates (and afterwards finishes) a real publish gate on the running v0 stack for the UI's live E2E.

  make    reset acme-payments/ledger-api main to the planted bug, open a Gitea issue labelled `replicant` as alice,
          wait for the job's publish gate, print {"job_id", "gate_id"} as JSON on the last line
  start   like make, but print {"job_id"} as soon as the job is acknowledged (for the cancel test)
  finish  JOB_ID: after the UI approved the gate, merge the replicant PR and wait for `succeeded`

Reuses tests/e2e/e2e01_v0.py and tests/chaos/chaos_v0.py (public APIs only; never prints a secret).
Refuses to reset the repo while another job is still open, so it does not disturb other users of the stack.
Run from replicant/:  uv run --package replicant-e2e python web/control-plane-web/e2e/live_gate.py make
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests" / "e2e"))
sys.path.insert(0, str(ROOT / "tests" / "chaos"))
import e2e01_v0 as e  # noqa: E402
from chaos_v0 import reset_repo  # noqa: E402


def open_jobs() -> list[dict]:
    with e.api(e.READER) as c:
        r = e.ok(c.get("/v1/jobs", params={"status": "queued,running,waiting_human,waiting_external,paused", "limit": 50}), "jobs")
    return r.json()["items"]


def make(wait_gate: bool = True) -> None:
    busy = open_jobs()
    deadline = time.monotonic() + 600
    while busy and time.monotonic() < deadline:
        e.log(f"waiting for {len(busy)} open job(s) to finish before resetting the repo: {[j['job_id'] for j in busy]}")
        time.sleep(15)
        busy = open_jobs()
    if busy:
        raise SystemExit("other jobs are still open; not resetting the sample repo")
    e.log(f"main reset to the initial commit {reset_repo()[:12]}")
    number, t0 = e.open_issue(f"web-{uuid.uuid4().hex[:6]}")
    job_id = e.wait_ack(number, t0)
    if not wait_gate:
        print(json.dumps({"job_id": job_id, "issue": number}), flush=True)
        return
    with e.api(e.READER) as c:
        job = e.wait_until(c, job_id, lambda s: e.open_publish_gate(s) is not None, "publish gate", time.monotonic() + 900)
    gate = e.open_publish_gate(e.snapshot(job))
    print(json.dumps({"job_id": job_id, "gate_id": gate["gate_id"], "issue": number}), flush=True)


def finish(job_id: str) -> None:
    deadline = time.monotonic() + 900
    pr = e.wait_pr(job_id, deadline)
    e.merge(pr)
    with e.api(e.READER) as c:
        e.wait_until(c, job_id, lambda s: s.get("status") == "succeeded", "job succeeded", deadline)
    e.log(f"{job_id} succeeded after merging PR #{pr['number']}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["make"]:
        make()
    elif sys.argv[1:2] == ["start"]:
        make(wait_gate=False)
    elif sys.argv[1:2] == ["finish"] and len(sys.argv) == 3:
        finish(sys.argv[2])
    else:
        raise SystemExit(__doc__)
