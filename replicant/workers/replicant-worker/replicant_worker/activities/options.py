"""Activity catalog: names, task queues, timeouts, retry policies, side-effect classification.

Every activity Replicant workflows call is listed here. `side_effect=True` means the activity
changes something outside Temporal; such calls MUST pass an explicit logical activity ID
(C-IDS section 2.1, ADR-008). `tools/lint_activity_ids.py` enforces that in CI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio.common import RetryPolicy

Q_JOBS = "replicant-jobs"
Q_RUNNER = "replicant-runner-calls"
Q_LLM = "replicant-llm-calls"  # new queue (C-IDS section 4 allows replicant-<purpose>); same deployment
Q_PUBLISHER = "replicant-publisher"
Q_PROJECTIONS = "replicant-projections"

# Error types (ApplicationError.type) that must never be retried, per class.
NON_RETRYABLE_COMMON = ["InvalidInput", "DefinitionDigestMismatch", "NotInBundle", "SchemaViolation"]
RUNNER_NON_RETRYABLE = [
    "invalid_request", "missing_idempotency_key", "forbidden", "run_not_found", "run_expired",
    "idempotency_key_reuse", "admission_denied", "definition_not_resolvable", "steering_not_supported",
    "run_not_active",
    # C-RUNNER 2.1.0: the activity re-signs an expired grant and retries once internally (XD-02-14);
    # a 422 that survives a fresh grant is a real authorization failure and is not retried
    "run_grant_invalid",
]


@dataclass(frozen=True)
class ActivityClass:
    task_queue: str
    start_to_close: timedelta
    retry: RetryPolicy
    schedule_to_close: timedelta | None = None
    heartbeat: timedelta | None = None


IO_INTERNAL = ActivityClass(
    Q_JOBS, timedelta(seconds=60),
    RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=60), maximum_attempts=10,
                non_retryable_error_types=NON_RETRYABLE_COMMON),
    schedule_to_close=timedelta(minutes=30),
)
IO_SLOW = ActivityClass(  # context assembly, packet build: may read many objects
    Q_JOBS, timedelta(minutes=10),
    RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=2), maximum_attempts=6,
                non_retryable_error_types=NON_RETRYABLE_COMMON),
    schedule_to_close=timedelta(hours=1), heartbeat=timedelta(seconds=60),
)
IDENTITY = ActivityClass(  # C-MSG section 2.1: start_to_close 10 s, retry max 5.
    # R2-011: the backoff (8 + 16 + 32 + 60 s) spans about 2 minutes, longer than an Aurora failover (< 60 s).
    # If it is still exhausted, the handler returns `denied` / `identity_unverifiable`; it never fails.
    Q_JOBS, timedelta(seconds=10),
    RetryPolicy(initial_interval=timedelta(seconds=8), backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=60), maximum_attempts=5,
                non_retryable_error_types=["InvalidInput"]),
)
AUDIT_WRITE = ActivityClass(  # R2-013: approval records are awaited before Continue-As-New or close
    Q_JOBS, timedelta(seconds=60),
    RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=5), maximum_attempts=0,
                non_retryable_error_types=NON_RETRYABLE_COMMON),
    schedule_to_close=timedelta(days=7),
)
RELEASE_OVERRIDE = ActivityClass(  # Migration Handoff Protocol v1 (versioning.md 6.3): retried for 30 min,
    # then the job falls back to its source build (R2-015)
    Q_JOBS, timedelta(seconds=30),
    RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=2), maximum_attempts=0,
                non_retryable_error_types=NON_RETRYABLE_COMMON),
    schedule_to_close=timedelta(minutes=30),
)
QA_STUBS = ActivityClass(  # WS-05 stage-design.md 4.5 (one table, R1-124)
    Q_JOBS, timedelta(seconds=60),
    RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=30), maximum_attempts=10,
                non_retryable_error_types=NON_RETRYABLE_COMMON),
    schedule_to_close=timedelta(minutes=30),
)
QA_CLASSIFY = ActivityClass(  # WS-05 stage-design.md 4.5 (one table, R1-124)
    Q_JOBS, timedelta(seconds=120),
    RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=60), maximum_attempts=8,
                non_retryable_error_types=NON_RETRYABLE_COMMON + ["QaReportInvalid"]),
    schedule_to_close=timedelta(minutes=30),
)
RUNNER_START = ActivityClass(  # XD-FIX-R2-020 / XD-W2-02-04 (R1V-093): at most 60 s of retries per attempt; the
    # tenant run quota (429) is the typed non-retryable CapacityUnavailable{reason, quota_used, quota_limit,
    # retry_after_seconds} and the workflow waits with a durable timer (JobSnapshot.waiting). runner-api has no
    # queue and no 503: cluster capacity is detected in the run wait (Pending, Scheduled=False > 60 s)
    Q_RUNNER, timedelta(seconds=30),
    RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=20), maximum_attempts=0,
                non_retryable_error_types=RUNNER_NON_RETRYABLE + ["CapacityUnavailable"]),
    schedule_to_close=timedelta(seconds=60),
)
EVAL_AWAIT = ActivityClass(  # XD-FIX-R1-134: heartbeating wait for eval-worker's staged manifest
    Q_JOBS, timedelta(minutes=16),
    RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0, maximum_interval=timedelta(seconds=30),
                maximum_attempts=3, non_retryable_error_types=["InputsNotStaged"]),
    heartbeat=timedelta(seconds=60),
)
QA_INDEX = ActivityClass(  # XD-FIX-R1-118: qa.index_results on replicant-projector, fire-and-forget
    "replicant-projections", timedelta(seconds=60),
    RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=30), maximum_attempts=5,
                non_retryable_error_types=["QaReportInvalid"]),
)
RUNNER_GET = ActivityClass(  # C-RUNNER: start_to_close 10 s
    Q_RUNNER, timedelta(seconds=10),
    RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=30), maximum_attempts=0,
                non_retryable_error_types=["run_not_found", "run_expired"]),
    schedule_to_close=timedelta(minutes=10),
)
RUNNER_CANCEL = ActivityClass(
    Q_RUNNER, timedelta(seconds=30),
    RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=5), maximum_attempts=0,
                non_retryable_error_types=["run_not_found", "run_expired"]),
    schedule_to_close=timedelta(hours=24),
)
RUNNER_MESSAGE = ActivityClass(
    Q_RUNNER, timedelta(seconds=15),
    RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=10), maximum_attempts=4,
                non_retryable_error_types=RUNNER_NON_RETRYABLE),
)
LLM = ActivityClass(  # in-worker model calls through the AI gateway (router intent, triage)
    Q_LLM, timedelta(seconds=120),
    RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=2), maximum_attempts=5,
                non_retryable_error_types=["GatewayBudgetExceeded", "ContentPolicy", "InvalidInput"]),
    schedule_to_close=timedelta(minutes=20),
)
PUBLISHER = ActivityClass(
    Q_PUBLISHER, timedelta(minutes=5),
    RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=5), maximum_attempts=8,
                non_retryable_error_types=["PushRejected", "BranchProtected", "MergeConflict",
                                           "PermissionDenied", "PullRequestClosed", "InvalidInput"]),
    schedule_to_close=timedelta(hours=2),
)
PROJECTION = ActivityClass(  # C-STATE section 2.1, verbatim
    Q_PROJECTIONS, timedelta(seconds=30),
    RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=60), maximum_attempts=0),
    schedule_to_close=timedelta(hours=24),
)


from replicant_worker import build_config as _bc  # noqa: E402

if _bc.FAST_RETRY:  # test-only (build_config.FAST_RETRY): exhaust internal retries within seconds
    _fast = RetryPolicy(initial_interval=timedelta(milliseconds=200), backoff_coefficient=1.0,
                        maximum_interval=timedelta(milliseconds=200), maximum_attempts=3,
                        non_retryable_error_types=NON_RETRYABLE_COMMON + ["QaReportInvalid"])
    IO_INTERNAL = ActivityClass(Q_JOBS, IO_INTERNAL.start_to_close, _fast, IO_INTERNAL.schedule_to_close)
    IO_SLOW = ActivityClass(Q_JOBS, IO_SLOW.start_to_close, _fast, IO_SLOW.schedule_to_close, IO_SLOW.heartbeat)
    IDENTITY = ActivityClass(Q_JOBS, IDENTITY.start_to_close, _fast)


@dataclass(frozen=True)
class ActivityDef:
    name: str
    klass: ActivityClass
    side_effect: bool


CATALOG: dict[str, ActivityDef] = {a.name: a for a in [
    # bundle, graph, routing
    ActivityDef("registry.load_bundle_core", IO_INTERNAL, False),
    ActivityDef("graph.compose", IO_INTERNAL, False),
    ActivityDef("router.classify", IO_INTERNAL, False),
    ActivityDef("router.parse_intent", LLM, False),  # tool-less, closed schema, selection only (G-24)
    ActivityDef("scm.resolve_base", PUBLISHER, False),
    # context and memory (WS-04, XD-04-01)
    ActivityDef("memory.create_snapshot", IO_INTERNAL, True),
    ActivityDef("context.build_briefing", IO_SLOW, True),
    ActivityDef("context.stage_inputs", IO_SLOW, True),
    ActivityDef("steering.stage_backlog", IO_INTERNAL, True),
    ActivityDef("memory.extract_candidates", LLM, False),  # tool-less but generative: WS-04 controls (R1-024, XD-04-14)
    ActivityDef("memory.write_candidates", IO_INTERNAL, True),
    ActivityDef("memory.feedback_signals", IO_INTERNAL, True),
    ActivityDef("memory.record_outcome", IO_INTERNAL, True),
    ActivityDef("memory.write_episode", IO_INTERNAL, True),
    ActivityDef("memory.ingest_gate_feedback", LLM, True),  # MEM: request_changes/reject comments -> candidates
    # agent runs (WS-03, XD-03-12)
    ActivityDef("agents.build_run_plan", IO_INTERNAL, False),
    ActivityDef("budget.allocate_run", IO_INTERNAL, False),
    ActivityDef("agents.stage_inputs", IO_INTERNAL, True),  # materialized config + gateway run policy
    ActivityDef("budget.apply_extension", IO_INTERNAL, True),
    ActivityDef("budget.tenant_quota_status", IO_INTERNAL, False),
    ActivityDef("cost.record_run", IO_INTERNAL, True),
    ActivityDef("cost.run_resource_usage", IO_INTERNAL, False),  # XD-FIX-R2-064 compute dollars so far
    # Runner (D-04); runner.start_run signs the run grant (CCR-09-02)
    ActivityDef("runner.start_run", RUNNER_START, True),
    ActivityDef("runner.get_run", RUNNER_GET, False),
    ActivityDef("runner.cancel_run", RUNNER_CANCEL, True),
    ActivityDef("runner.post_message", RUNNER_MESSAGE, True),
    ActivityDef("runner.read_result", IO_INTERNAL, False),
    # QA (WS-05, XD-05-03)
    ActivityDef("qa.stage_contract_stubs", QA_STUBS, True),
    ActivityDef("qa.classify", QA_CLASSIFY, False),
    ActivityDef("qa.index_results", QA_INDEX, True),  # not awaited; drained before Continue-As-New and close
    # risk, verification, checks
    ActivityDef("risk.classify", IO_INTERNAL, False),
    ActivityDef("risk.llm_review", LLM, False),  # may only raise; enum output (G-24)
    ActivityDef("verifier.check", IO_SLOW, False),
    ActivityDef("checks.run_step", IO_SLOW, True),  # generic auxiliary step (NodeCatalog `check_step`)
    # gates and notifications (WS-07 logic, XD-07-01)
    ActivityDef("gates.resolve_routing", IO_INTERNAL, False),
    ActivityDef("gates.build_packet", IO_SLOW, True),
    ActivityDef("gates.write_approval_record", AUDIT_WRITE, True),  # approval_text + reference-only record (C-ART 1.9.0)
    ActivityDef("identity.check_decision", IDENTITY, False),
    ActivityDef("identity.check_action", IDENTITY, False),
    ActivityDef("identity.check_repair_trigger", IDENTITY, False),
    ActivityDef("notify.gate_opened", IO_INTERNAL, True),
    ActivityDef("notify.gate_reminder", IO_INTERNAL, True),
    ActivityDef("notify.gate_escalated", IO_INTERNAL, True),
    ActivityDef("notify.gate_resolved", IO_INTERNAL, True),
    ActivityDef("notify.job_event", IO_INTERNAL, True),
    ActivityDef("notify.operator", IO_INTERNAL, True),  # operator alerts (XD-W2-05-05; WS-07 POST /internal/v1/notifications)
    # evaluation mode (WS-06, CCR-06-01, XD-06-02)
    ActivityDef("eval.read_gate_script", IO_INTERNAL, False),
    ActivityDef("eval.read_review_round", IO_INTERNAL, False),
    ActivityDef("eval.read_harness_spec", IO_INTERNAL, False),  # XD-FIX-R1-134: steps of a harness job
    ActivityDef("eval.await_inputs", EVAL_AWAIT, False),  # waits up to 15 min for inputs/manifest.json
    # publishing and PR feedback (publisher-worker; WS-07 owns GitHub semantics)
    ActivityDef("publisher.check_base", PUBLISHER, False),
    ActivityDef("publisher.publish", PUBLISHER, True),
    ActivityDef("publisher.push_update", PUBLISHER, True),
    ActivityDef("publisher.close_pr", PUBLISHER, True),
    ActivityDef("publisher.push_takeover_branch", PUBLISHER, True),
    ActivityDef("github.post_reply", PUBLISHER, True),
    ActivityDef("feedback.triage", LLM, False),  # tool-less, closed schema, selection only (G-24)
    ActivityDef("artifacts.expire_session_state", IO_INTERNAL, True),
    ActivityDef("artifacts.resolve_superseded_plan", IO_INTERNAL, False),  # XD-FIX-R2-050 (tenant-checked)
    ActivityDef("takeover.assemble_package", IO_SLOW, True),
    # releases
    ActivityDef("release.check_migration_eligibility", IO_INTERNAL, False),
    ActivityDef("release.apply_migration_override", RELEASE_OVERRIDE, True),
    # operations (C-MSG 1.5.0 ops.kill_switch; R2-012 circuit breaker)
    ActivityDef("ops.read_kill_switches", IO_INTERNAL, False),  # out-of-band mirror, read once at start
    ActivityDef("platform.dependency_health", IO_INTERNAL, False),  # breaker state of a platform dependency
    # maintenance fan-out
    ActivityDef("catalog.resolve_targets", IO_SLOW, False),  # -> {children_index_uri, count} (XD-W3-R1V-006)
    ActivityDef("catalog.read_targets", IO_INTERNAL, False),  # page of the S3 repository list (<= 100)
    ActivityDef("intake.create_child_job", IO_INTERNAL, True),
    ActivityDef("cost.batch_spend", IO_INTERNAL, False),  # R2-062: spend of all children of a batch
    # projections (non-awaited; C-STATE section 2)
    ActivityDef("projection.apply", PROJECTION, False),
]}
