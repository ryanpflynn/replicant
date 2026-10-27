"""CONTRACT FAKE of WORKER's ReplicantJob for CONTROL's component smoke test (used only when the real replicant-worker is
not running). A versioned worker `replicant-worker:<REPLICANT_WORKER_BUILD_ID>` polling `replicant-jobs`, with a PINNED
`ReplicantJob` that keeps its start input, accepts Update `gate.decide` for gates listed in `STUB_GATES` (comma list) with
the C-MSG validator rejection types, and buffers Signal `pr.event`; it completes on pull_request_merged/closed.

    REPLICANT_TEMPORAL_ADDRESS=127.0.0.1:57233 STUB_GATES=gate_... python stub_worker.py
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker, WorkerDeploymentConfig

BUILD = os.environ.get("REPLICANT_WORKER_BUILD_ID", "b20261024-000000000000")
GATES = tuple(g for g in os.environ.get("STUB_GATES", "").split(",") if g)


@workflow.defn(name="ReplicantJob", versioning_behavior=VersioningBehavior.PINNED)
class StubReplicantJob:
    def __init__(self) -> None:
        self.input: dict[str, Any] = {}
        self.decisions: dict[str, dict[str, Any]] = {}
        self.pr_events: list[dict[str, Any]] = []
        self.done = False

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        self.input = inp
        await workflow.wait_condition(lambda: self.done, timeout=timedelta(hours=1))
        return {"pr_events": len(self.pr_events), "decisions": list(self.decisions)}

    @workflow.update(name="gate.decide")
    async def gate_decide(self, req: dict[str, Any]) -> dict[str, Any]:
        res = {"schema": "replicant.msg.gate-decide-result/v1", "outcome": "accepted", "gate_id": req["gate_id"],
               "decision_id": req["decision_id"],
               "winning_decision": {"decision_id": req["decision_id"], "decision": req["decision"],
                                    "principal_id": req["actor"]["principal_id"], "channel": req["channel"],
                                    "decided_at": req["decided_at"]}}
        self.decisions[req["decision_id"]] = res
        return res

    @gate_decide.validator
    def v_gate_decide(self, req: dict[str, Any]) -> None:
        if req.get("schema") != "replicant.msg.gate-decide/v1":
            raise ApplicationError("bad schema", type="InvalidPayload", non_retryable=True)
        if req["gate_id"] not in GATES:
            raise ApplicationError("unknown gate", type="UnknownGate", non_retryable=True)
        if req["decision_id"] in self.decisions:
            raise ApplicationError("dup", self.decisions[req["decision_id"]], type="DuplicateDecision",
                                   non_retryable=True)
        if any(d["gate_id"] == req["gate_id"] for d in self.decisions.values()):
            winner = next(d for d in self.decisions.values() if d["gate_id"] == req["gate_id"])
            raise ApplicationError("decided", winner["winning_decision"], type="GateAlreadyDecided", non_retryable=True)
        if not req["actor"]["principal_id"].startswith("usr_"):
            raise ApplicationError("svc", type="ActorNotHuman", non_retryable=True)

    @workflow.signal(name="pr.event")
    def pr_event(self, sig: dict[str, Any]) -> None:
        if any(e["delivery_id"] == sig["delivery_id"] for e in self.pr_events):
            return
        self.pr_events.append(sig)
        if sig.get("event") in ("pull_request_merged", "pull_request_closed"):
            self.done = True

    @workflow.query(name="job.snapshot")
    def snapshot(self) -> dict[str, Any]:
        return {"input": self.input, "decisions": self.decisions, "pr_events": self.pr_events}


async def main() -> None:
    from replicant_temporal import TemporalSettings, connect
    client = await connect(TemporalSettings.from_env(identity="ctl-stub-worker"))
    w = Worker(client, task_queue="replicant-jobs", workflows=[StubReplicantJob],
               deployment_config=WorkerDeploymentConfig(version=WorkerDeploymentVersion("replicant-worker", BUILD),
                                                        use_worker_versioning=True))
    print(f"stub worker replicant-worker:{BUILD} polling replicant-jobs; gates={GATES}", flush=True)
    await w.run()


if __name__ == "__main__":
    asyncio.run(main())
