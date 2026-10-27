"""§4 Temporal naming: namespaces, task queues, search attributes, workflow types, message names."""
from __future__ import annotations

ENVS = ("local", "dev", "staging", "prod")


def namespace(env: str) -> str:
    if env not in ENVS:
        raise ValueError(f"unknown env {env!r}")
    return f"replicant-{env}"


TASK_QUEUES: dict[str, str] = {
    "replicant-jobs": "replicant-worker",
    "replicant-runner-calls": "replicant-worker",
    "replicant-publisher": "publisher-worker",
    "replicant-projections": "replicant-projector",
    "replicant-eval": "eval-worker",
    "replicant-retention": "retention-sweeper",
    "replicant-ops": "ops-worker",
}
"""Task queue -> deployment that polls it (§4 table; retention/ops hosts from the local stack)."""

DEPLOYMENT_NAMES = ("replicant-worker", "publisher-worker", "replicant-projector", "eval-worker")

# name -> Temporal indexed value type, exactly C-IDS §4.1 (8 Keyword, 2 KeywordList, 2 Int, 1 Datetime, 1 Bool).
SEARCH_ATTRIBUTES: dict[str, str] = {
    "ReplicantJobId": "Keyword",
    "ReplicantTenantId": "Keyword",
    "ReplicantWorkflowType": "Keyword",
    "ReplicantStatus": "Keyword",
    "ReplicantPhase": "Keyword",
    "ReplicantReleaseBundleId": "Keyword",
    "ReplicantParentJobId": "Keyword",
    "ReplicantRiskLevel": "Keyword",
    "ReplicantRepos": "KeywordList",
    "ReplicantOpenGateIds": "KeywordList",
    "ReplicantRepairCycle": "Int",
    "ReplicantProjectionSeq": "Int",
    "ReplicantWaitingSince": "Datetime",
    "ReplicantAwaitingHuman": "Bool",
}

WORKFLOW_TYPES = (
    "ReplicantJob", "MaintenanceBatch", "MaintenanceShard", "EvalSuite", "EvalCase", "ProjectionReconciler",
    "RetentionSweeper", "ReleasePipeline", "EvalComparisonRun", "SentinelEval", "JudgeDriftCheck", "GoldenMiner",
    "GoldenHarvester", "ImprovementProposer", "CliUpgradeWatcher", "OnlineMetrics", "ProjectionRebuild",
    "ScheduledIntake", "DataOperation", "LeakPurge", "ComponentSuite",
)

MESSAGE_NAMES = (
    "gate.decide", "job.steer", "job.cancel", "job.pause", "job.resume", "job.takeover", "job.offer_bundle_migration",
    "run.status_changed", "pr.event", "release.notice", "identity.principal_changed", "job.snapshot",
    "job.open_gates", "job.working_memory_refs", "batch.snapshot", "batch.cancel", "release.signoff",
    "release.abort", "release.hold", "release.snapshot", "batch.child_progress", "ops.kill_switch", "ops.retry_node",
)
