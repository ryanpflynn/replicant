"""QA routing (OWNED BY WS-05; vendored verbatim from spec/05-qa/stage-design.md section 6, fix round wave 2:
AdmissionDenied (R1-122) and qa_only fail_job (R1-131)).

Pure and replay-safe. WS-01's qa_run node feeds it the classified QA result (activity qa.classify)
and the per-cycle counters; graph edges route on the returned decision (XD-05-03).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Decision = Literal["proceed", "fix_loop", "retry_qa", "operator_and_gate", "gate_with_failure", "fail_job"]


@dataclass(frozen=True)
class QaRoutingInput:
    verdict: Literal["pass", "pass_with_warnings", "fail", "inconclusive", "not_applicable"]
    reason_code: str
    run_failure_reason: str | None   # AgentRun failure reason when the run itself failed
    run_failure_retryable: bool
    fix_iterations_used: int         # QA-driven implementer re-runs already done in this cycle
    fix_iterations_max: int          # repair_limits.qa.fix_iterations
    env_retries_used: int            # QA re-runs for environment reasons in this cycle
    on_fail_after_fixes: Literal["gate_with_failure", "fail_job"]
    workflow_type: str
    oscillation: bool                # a test fixed in an earlier iteration fails again
    env_signature_transient: bool    # first environment signature is retry-worthy


def route_after_qa(i: QaRoutingInput) -> tuple[Decision, str]:
    """Pure function (replay-safe). Returns (decision, risk_floor)."""
    if i.run_failure_reason == "EnvironmentSetupFailed":          # C-CRD 2.1.0; report and run_result exist
        transient = i.reason_code == "environment_failure" and i.env_retries_used < 1 and i.env_signature_transient
        return ("retry_qa", "low") if transient else ("operator_and_gate", "high")
    if i.run_failure_reason == "AdmissionDenied":                # 422 at createRun or controller-side VAP denial (R1-122)
        return "operator_and_gate", "high"                       # classify synthesized verdict inconclusive/unsupported_runtime
    if i.run_failure_reason is not None:
        if i.run_failure_reason in {"WorkspaceDiskExceeded", "OOMKilled"}:
            return "operator_and_gate", "high"
        if i.run_failure_reason == "SecurityViolation":       # CCR-09-04: never retried
            return "fail_job", "critical"
        if i.run_failure_retryable and i.env_retries_used < 1:
            return "retry_qa", "low"
        return "operator_and_gate", "high"
    if i.verdict == "not_applicable":
        return "proceed", "low"
    if i.verdict == "inconclusive":
        if i.env_retries_used < 1 and i.reason_code == "environment_failure":
            return "retry_qa", "low"
        if i.workflow_type == "qa_only":
            return "fail_job", "high"                            # graph ends at end:qa_inconclusive by verdict
        return "operator_and_gate", "high"
    if i.verdict == "fail":
        if i.workflow_type == "qa_only":
            return "fail_job", "high"                            # nothing to publish: end:qa_failed
        if i.fix_iterations_used < i.fix_iterations_max and not i.oscillation:
            return "fix_loop", "medium"
        return i.on_fail_after_fixes, "critical"
    if i.verdict == "pass_with_warnings":
        return "proceed", "medium"
    return "proceed", "low"
