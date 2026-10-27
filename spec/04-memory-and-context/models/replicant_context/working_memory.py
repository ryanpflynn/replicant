"""Job working memory carried in ReplicantJob workflow state (WS-04 sketch for WS-01).

Lives in workflow state and is passed explicitly on every Continue-As-New (C-IDS §3 item 5).
Bodies stay in S3; this holds references and small values only. Budget: <= 64 KiB serialized
of the <= 256 KiB total workflow state (C-STATE §3 rule 1). Serialized with the Temporal
default JSON converter (dataclasses are supported by the Python SDK's pydantic-free converter).

Deterministic code only: no I/O, no clocks other than values passed in from workflow.now().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Role = Literal["planner", "implementer", "repair", "reviewer", "qa"]
ContinuityMode = Literal["handoff_only", "resume_same_cycle"]

MAX_TOUCHED_PATHS = 200
MAX_DECISIONS_INLINE = 20
MAX_STATE_BYTES = 64 * 1024


@dataclass(frozen=True)
class ArtifactRef:
    """Logical artifact reference (C-ART §3) plus integrity digest."""

    bucket_class: Literal["artifacts", "transcripts", "session", "memory"]
    key: str
    sha256: str


@dataclass(frozen=True)
class SessionPointer:
    """Captured CLI session state that a later run of the SAME cycle may resume (ADR-04-001)."""

    run_id: str
    cli: Literal["claude-code", "claude-agent-sdk", "codex"]
    cli_image_digest: str  # resume only if the next run uses the identical digest
    cli_session_id: str
    state: ArtifactRef  # bucket_class == "session"
    cycle: int
    captured_at: str  # RFC 3339, from the run manifest
    size_bytes: int


@dataclass
class ReviewFeedbackRef:
    """One debounced batch of PR feedback that starts a repair cycle."""

    cycle: int
    delivery_ids: list[str]
    payloads: list[ArtifactRef]  # pr_event_payload objects
    triage: ArtifactRef | None = None  # steps/feedback_triage.c<n>.n1/ output
    addressed_comment_ids: list[int] = field(default_factory=list)
    declined_comment_ids: list[int] = field(default_factory=list)
    question_comment_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class SteeringMessage:
    """One queued job.steer message (XD-W2-01-05). text is untrusted, at most 16000 chars."""
    message_id: str
    text: str
    author: str          # usr_ principal ID
    sent_at: str         # RFC 3339


@dataclass(frozen=True)
class SteeringBacklogInput:
    """Input of activity steering.stage_backlog (schemas/steering-stage-backlog.schema.json)."""
    job_id: str
    node_key: str
    cycle: int
    ordinal: int
    message_ids: list[str]
    messages: list[SteeringMessage]

    def check(self) -> None:
        if [m.message_id for m in self.messages] != list(self.message_ids):
            raise ValueError("steering_backlog_mismatch")   # non-retryable ApplicationError in the activity


@dataclass(frozen=True)
class SteeringBacklogResult:
    keys: list[str]


@dataclass
class WorkingMemory:
    """Everything one node hands to the next. Bodies by reference; summaries by value."""

    job_id: str
    goal_summary: str  # replicant-written from the task packet, <= 2000 chars
    acceptance_criteria: list[str] = field(default_factory=list)

    # Long-term memory binding
    memory_snapshot_id: str | None = None
    memory_binding_ordinal: int = 0

    # Ecosystem
    briefing: ArtifactRef | None = None
    graph_seq: int | None = None

    # Plan
    plan: ArtifactRef | None = None
    plan_summary: str | None = None  # <= 2000 chars, replicant-rendered from the plan's structured steps
    planned_paths: list[str] = field(default_factory=list)

    # Implementation state
    latest_diff_bundle: ArtifactRef | None = None
    touched_paths: list[str] = field(default_factory=list)  # from diff metadata, capped
    latest_handoff: ArtifactRef | None = None
    handoff_history: list[ArtifactRef] = field(default_factory=list)  # last 5
    latest_session: SessionPointer | None = None
    decisions_inline: list[str] = field(default_factory=list)  # short replicant-quoted lines from handoffs

    # Validation state
    latest_qa_report: ArtifactRef | None = None
    qa_passed: bool | None = None
    failing_tests: list[str] = field(default_factory=list)
    latest_review_report: ArtifactRef | None = None

    # PR and repair
    pr_repo: str | None = None
    pr_number: int | None = None
    pr_head_sha: str | None = None
    feedback: list[ReviewFeedbackRef] = field(default_factory=list)

    # Steering backlog (message IDs; texts live in history inside the Update requests)
    steering_pending: list[str] = field(default_factory=list)

    def record_run(
        self,
        *,
        handoff: ArtifactRef | None,
        diff: ArtifactRef | None,
        touched: list[str],
        session: SessionPointer | None,
        decisions: list[str],
    ) -> None:
        if handoff is not None:
            if self.latest_handoff is not None:
                self.handoff_history = (self.handoff_history + [self.latest_handoff])[-5:]
            self.latest_handoff = handoff
        if diff is not None:
            self.latest_diff_bundle = diff
        merged = list(dict.fromkeys(self.touched_paths + touched))
        self.touched_paths = merged[:MAX_TOUCHED_PATHS]
        self.latest_session = session
        self.decisions_inline = (self.decisions_inline + decisions)[-MAX_DECISIONS_INLINE:]

    def start_repair_cycle(self, feedback: ReviewFeedbackRef) -> None:
        """Called after Continue-As-New into a new cycle. Sessions never cross cycles (ADR-04-001)."""
        self.feedback.append(feedback)
        self.feedback = self.feedback[-5:]
        self.latest_session = None
        self.qa_passed = None
        self.failing_tests = []

    def continuity_for(self, role: Role, cycle: int, cli_image_digest: str, mode: ContinuityMode) -> SessionPointer | None:
        """Session to resume for the next run of `role`, or None for a fresh session plus handoff."""
        s = self.latest_session
        if mode != "resume_same_cycle" or role not in ("implementer", "repair") or s is None:
            return None
        if s.cycle != cycle or s.cli_image_digest != cli_image_digest:
            return None
        if s.size_bytes > 256 * 1024 * 1024:
            return None
        return s
