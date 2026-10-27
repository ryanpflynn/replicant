"""C-IDS 2.0.0 reference implementation (spec/00-contracts/ids-and-naming.md).

Encoding (§1), the identifier catalog's derived IDs (§2), Temporal naming (§4), trace and span IDs
(§5 rules T-1 and T-5) and the normative validation regexes (§12). Pure stdlib; no I/O.
"""
from __future__ import annotations

from replicant_ids.encoding import ALPHABET, b32_130, derived, ulid, UlidGenerator, ulid_timestamp_ms
from replicant_ids.ids import (
    agentrun_name,
    artifact_index_id,
    child_workflow_id,
    decision_id,
    gate_id,
    idempotency_key,
    logical_activity_id,
    new_id,
    notification_id,
    release_bundle_id,
    jcs,
    run_id,
    run_body,
    system_workflow_id,
    takeover_id,
    worker_deployment_version,
)
from replicant_ids.regex import PATTERNS, is_valid, validate
from replicant_ids.temporal import (
    DEPLOYMENT_NAMES,
    MESSAGE_NAMES,
    SEARCH_ATTRIBUTES,
    TASK_QUEUES,
    WORKFLOW_TYPES,
    namespace,
)
from replicant_ids.trace import job_span_id, job_trace_id, run_span_id

CONTRACT_VERSION = "2.0.0"

__all__ = [
    "ALPHABET", "CONTRACT_VERSION", "DEPLOYMENT_NAMES", "MESSAGE_NAMES", "PATTERNS", "SEARCH_ATTRIBUTES",
    "TASK_QUEUES", "UlidGenerator", "WORKFLOW_TYPES", "agentrun_name", "artifact_index_id", "b32_130",
    "child_workflow_id", "decision_id", "derived", "gate_id", "idempotency_key", "is_valid", "jcs",
    "job_span_id", "job_trace_id", "logical_activity_id", "namespace", "new_id", "notification_id",
    "release_bundle_id", "run_body", "run_id", "run_span_id", "system_workflow_id", "takeover_id", "ulid",
    "ulid_timestamp_ms", "validate", "worker_deployment_version",
]
