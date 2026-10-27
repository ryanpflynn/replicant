"""Shared Temporal plumbing for replicant services and workers.

Public API (announced in replicant/docs/v0/INTERFACES-LOG.md):

    from replicant_temporal import TemporalSettings, connect
    from replicant_temporal.search_attributes import SA, job_start_attributes
    from replicant_temporal.versioning import (BUILD_ID_RE, build_id_from_env, deployment_config,
                                             set_current_version, describe_version_queues)
    from replicant_temporal.interceptors import ReplicantContextInterceptor, ReplicantContext, current_context
    from replicant_temporal.health import worker_healthcheck
    from replicant_temporal.queues import Q_JOBS, Q_RUNNER, Q_LLM, Q_PUBLISHER, Q_PROJECTIONS, Q_EVAL, Q_OPS
"""
from replicant_temporal.client import TemporalSettings, connect  # noqa: F401

__all__ = ["TemporalSettings", "connect"]
