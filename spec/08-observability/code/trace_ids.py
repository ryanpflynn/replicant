"""Deterministic trace and span identifiers (WS-08). Reference implementation for the shared replicant-telemetry
package (Python) and its Go twin. Rules T-1 and T-5 are normative in C-IDS 1.10.0 section 5.

Run the self-test:  python3 trace_ids.py
"""
from __future__ import annotations

import hashlib
import re

JOB_ID = re.compile(r"^job_[0-9a-hjkmnp-tv-z]{26}$")
RUN_ID = re.compile(r"^run_[0-9a-hjkmnp-tv-z]{26}$")


def _hex(prefix: str, value: str, n: int) -> str:
    out = hashlib.sha256((prefix + value).encode("utf-8")).hexdigest()[:n]
    if out == "0" * n:  # W3C: all-zero IDs are invalid; probability 2^-64 (span) or 2^-128 (trace)
        raise ValueError("all-zero identifier")
    return out


def job_trace_id(job_id: str) -> str:
    """C-IDS Rule T-1: one trace per job."""
    if not JOB_ID.match(job_id):
        raise ValueError(job_id)
    return _hex("replicant.job.trace.v1:", job_id, 32)


def job_root_span_id(job_id: str) -> str:
    """Rule T-5a (C-IDS 1.10.0 section 5, CCR-08-01): span ID of the root span `job` emitted by control-plane-api at intake.
    Any component holding job_id can parent late work (PR events weeks later, sweeper, audit export) under it."""
    if not JOB_ID.match(job_id):
        raise ValueError(job_id)
    return _hex("replicant.job.span.v1:", job_id, 16)


def run_span_id(run_id: str) -> str:
    """Rule T-5b (C-IDS 1.10.0 section 5, CCR-08-01): span ID of span `sandbox.run`, created by the entrypoint for the whole agent
    lifetime. The gateway shim parents requests that arrive without traceparent under it (X-Replicant-Run-Id)."""
    if not RUN_ID.match(run_id):
        raise ValueError(run_id)
    return _hex("replicant.run.span.v1:", run_id, 16)


def traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"


if __name__ == "__main__":
    job = "job_01m5p7x7r0bt69j0h0fbxepw90"
    run = "run_gj3h3ep3phjfs9e23v768zrca5"
    assert job_trace_id(job) == "fc0575170c8597ce9805552723c08d6e", "C-IDS test vector"
    print("job_trace_id    ", job_trace_id(job))
    print("job_root_span_id", job_root_span_id(job))
    print("run_span_id     ", run_span_id(run))
    print("traceparent     ", traceparent(job_trace_id(job), run_span_id(run)))
