"""Manual demo helper for the Replicant v0 MVP. You drive the job from the web UI; this only does the Gitea-side steps.

Prerequisite (from the monorepo root): `uv run replicant dev up --profile v0` and `uv run replicant dev seed`.

    uv run --package replicant-e2e python tests/e2e/demo.py start           # reset ledger-api to the bug, open an issue
    uv run --package replicant-e2e python tests/e2e/demo.py merge <job_id>  # merge the job's PR (acts as alice)
"""

from __future__ import annotations

import sys
import time
import uuid

import replicant_e2e as e
from replicant_e2e import reset_repo

UI = "http://127.0.0.1:8400"


def start() -> None:
    root = reset_repo()
    print(f"ledger-api main reset to the planted bug ({root[:12]})")
    number, t0 = e.open_issue(f"demo-{uuid.uuid4().hex[:6]}")
    print(f"issue:  {e.GITEA}/{e.ORG}/{e.REPO}/issues/{number}")
    job_id = e.wait_ack(number, t0)
    print(f"job:    {UI}/jobs/{job_id}")
    print(f"approve when it reaches the publish gate: {UI}/approvals  (signed in as grace or bob)")
    print(f"then:   uv run --package replicant-e2e python tests/e2e/demo.py merge {job_id}")


def merge(job_id: str) -> None:
    pr = e.wait_pr(job_id, time.monotonic() + 600)
    print(f"PR:     {pr['html_url']}")
    e.merge(pr)
    print(f"merged; the job finishes as succeeded within a few seconds: {UI}/jobs/{job_id}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "start":
        start()
    elif len(sys.argv) == 3 and sys.argv[1] == "merge":
        merge(sys.argv[2])
    else:
        sys.exit(__doc__)
