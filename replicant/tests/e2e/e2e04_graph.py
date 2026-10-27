"""E2E-04 (GRAPH-*): job graph and run-activity read APIs on a finished scripted-backend bugfix job.

    GRAPH_JOB_ID=job_… uv run --package replicant-e2e python tests/e2e/e2e04_graph.py
    (without GRAPH_JOB_ID: the most recent succeeded job alice can read)

Checks: GET /v1/jobs/{id}/graph (C-DEF bugfix graph, the happy path succeeded, plan_gate skipped, the taken edges, the
reached terminal) and GET /v1/jobs/{id}/runs/{run}/activity for the implementer run (Read/Edit/Bash tool calls with
results, an Edit diff, gateway usage rows, no JWTs in the body), a run of another job → 404, an unknown run → 404.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e01_v0 as e  # noqa: E402

HAPPY = ["context", "plan", "risk_plan", "implement", "qa", "review", "verify", "risk_diff", "publish_gate",
         "base_check", "publish", "pr_wait"]


def main() -> None:
    with e.api("alice") as c:
        job = os.environ.get("GRAPH_JOB_ID")
        if not job:
            items = c.get("/v1/jobs", params={"status": "succeeded", "limit": 1}).json()["items"]
            assert items, "no succeeded job to check; run tests/e2e/demo.py start/merge first"
            job = items[0]["job_id"]
        e.log(f"job {job}")
        g = c.get(f"/v1/jobs/{job}/graph")
        assert g.status_code == 200, g.text
        doc = g.json()
        assert doc["definition"]["ref"].startswith("graph/bugfix@"), doc.get("definition")
        nodes = {n["key"]: n for n in doc["graph"]["nodes"]}
        for k in HAPPY:
            assert nodes[k]["status"] == "succeeded", (k, nodes[k]["status"])
        assert nodes["plan_gate"]["status"] == "skipped"
        assert nodes["triage"]["status"] == "pending" and nodes["triage"]["visits"] == 0
        assert nodes["implement"]["runs"] and nodes["implement"]["is_agent"]
        taken = {(x["from"], x["to"]) for x in doc["graph"]["edges"] if x.get("taken")}
        assert ("implement", "qa") in taken and ("publish", "pr_wait") in taken, taken
        assert doc["graph"]["terminal"] == {"outcome": "pr_merged", "node": "end:pr_merged"}
        e.log(f"graph ok: {sum(1 for n in nodes.values() if n['status'] != 'pending')} nodes visited")

        run = nodes["implement"]["runs"][-1]["run_id"]
        a = c.get(f"/v1/jobs/{job}/runs/{run}/activity")
        assert a.status_code == 200, a.text
        act = a.json()
        body = json.dumps(act)
        assert not re.search(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.", body), "a JWT leaked into the feed"
        assert "token_jti" not in body and "grant_jti" not in body
        tools = [i for i in act["items"] if i["kind"] == "tool_call"]
        assert {"Read", "Edit", "Bash"} <= {t["tool"] for t in tools}, [t["tool"] for t in tools]
        assert all("result" in t for t in tools)
        edit = next(t for t in tools if t["tool"] == "Edit")
        assert edit["path"] == "internal/ledger/ledger.go" and "+\t\tfxReversal := Prorate(" in edit["diff"]
        assert act["usage"]["totals"]["requests"] >= 1 and act["usage"]["totals"]["cost_usd"] > 0
        assert act["result"]["cli"]["exit_code"] == 0 and act["live"] is False
        e.log(f"activity ok: {len(act['items'])} items, {len(tools)} tool calls, "
              f"{act['usage']['totals']['requests']} gateway requests")

        other = c.get("/v1/jobs", params={"limit": 20}).json()["items"]
        for o in other:
            if o["job_id"] == job:
                continue
            runs = c.get(f"/v1/jobs/{o['job_id']}/runs").json()["items"]
            if runs:
                r = c.get(f"/v1/jobs/{job}/runs/{runs[0]['run_id']}/activity")
                assert r.status_code == 404, ("a run of another job must not resolve", r.status_code)
                break
        assert c.get(f"/v1/jobs/{job}/runs/run_{'0' * 26}/activity").status_code == 404
        assert c.get(f"/v1/jobs/job_{'0' * 26}/graph").status_code == 404
    e.log("E2E-04 graph/activity: PASS")


def test_e2e04_graph() -> None:
    main()


if __name__ == "__main__":
    main()
