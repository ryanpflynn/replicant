"""Replay recorded production-shaped histories against the current ReplicantJob (determinism guard).

tests/replay/histories/ holds gzipped `WorkflowHistory.to_json()` exports of completed (and continued-as-new) runs from
the local stack: the e2e01, e2e05, e2e06 and e2e07 jobs, including PR-loop jobs with a repair cycle. File names are
`<scenario>__<workflow_id>__<HHMMSS>__<run_id>.json.gz`. A refactor of the workflow code must replay every one of them
without nondeterminism; a history recorded before an intentional, patched change keeps replaying through its
`workflow.patched` branch.
"""
from __future__ import annotations

import asyncio
import gzip
import pathlib

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from replicant_worker.cli import PASSTHROUGH
from replicant_worker.workflows.maintenance import MaintenanceBatch, MaintenanceShard
from replicant_worker.workflows.replicant_job import ReplicantJob

HISTORIES = sorted((pathlib.Path(__file__).parent / "replay" / "histories").glob("*.json.gz"))
RUNNER = SandboxedWorkflowRunner(restrictions=SandboxRestrictions.default.with_passthrough_modules(*PASSTHROUGH))


def test_histories_present() -> None:
    assert len(HISTORIES) >= 20, HISTORIES


@pytest.mark.parametrize("path", HISTORIES, ids=[p.name.split("__")[0] + ":" + p.name.split("__")[3][:8] for p in HISTORIES])
def test_replay(path: pathlib.Path) -> None:
    workflow_id = path.name.split("__")[1]
    hist = WorkflowHistory.from_json(workflow_id, gzip.decompress(path.read_bytes()).decode())

    async def go() -> None:
        r = Replayer(workflows=[ReplicantJob, MaintenanceBatch, MaintenanceShard], workflow_runner=RUNNER)
        res = await r.replay_workflow(hist, raise_on_replay_failure=False)
        assert res.replay_failure is None, f"{path.name}: {res.replay_failure!r}"[:2000]

    asyncio.run(go())
