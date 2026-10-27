"""§5 rules T-1 (one trace per job) and T-5 (deterministic span IDs)."""
from __future__ import annotations

import hashlib

from replicant_ids.regex import validate


def _hex(seed: str, n: int) -> str:
    h = hashlib.sha256(seed.encode()).hexdigest()[:n]
    if set(h) == {"0"}:  # never all zeros (W3C); probability ~2^-128, kept for completeness
        raise ValueError("degenerate all-zero id")
    return h


def job_trace_id(job_id: str) -> str:
    return _hex("replicant.job.trace.v1:" + validate("job_id", job_id), 32)


def job_span_id(job_id: str) -> str:
    return _hex("replicant.job.span.v1:" + validate("job_id", job_id), 16)


def run_span_id(run_id: str) -> str:
    return _hex("replicant.run.span.v1:" + validate("run_id", run_id), 16)
