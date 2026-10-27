"""Task queue names (C-IDS section 4). Fixed in code; no environment variable selects them (R1-267)."""
Q_JOBS = "replicant-jobs"
Q_RUNNER = "replicant-runner-calls"
Q_LLM = "replicant-llm-calls"
Q_PUBLISHER = "replicant-publisher"
Q_PROJECTIONS = "replicant-projections"
Q_EVAL = "replicant-eval"
Q_OPS = "replicant-ops"

# deployment name -> task queues its workers poll
DEPLOYMENT_QUEUES = {
    "replicant-worker": (Q_JOBS, Q_RUNNER, Q_LLM),
    "publisher-worker": (Q_PUBLISHER,),
    "replicant-projector": (Q_PROJECTIONS,),
}
