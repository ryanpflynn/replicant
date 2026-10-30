"""SQL operations on schema `memory` (the single writer): the constants and row helpers shared by `snapshots`,
`candidates`, `curation` and `analytics`. State changes always go through `curation._transition`, which writes
`state_transitions` first: the `entries_state_guard` trigger refuses a state change without it."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from memory_service import core
from memory_service.db import jdump
from replicant_ids import ulid

log = logging.getLogger(__name__)
SVC = "svc_memory-service"
LIVE = ("candidate", "active")
EXTRACTOR_DEFAULT = "memory-extraction@1"
ORIGIN_BY_KIND = {"pr_review_comment": "pr_feedback", "pr_review_body": "pr_feedback", "pr_issue_comment": "pr_feedback",
                  "replicant_command": "pr_feedback", "gate_comment": "pr_feedback", "review_finding": "negative_mining", "human_edit": "pr_feedback",
                  "qa_failure": "negative_mining", "agent_proposal": "agent_proposal", "import": "import",
                  "curator": "curator", "repair_rejection": "pr_feedback", "job_outcome": "negative_mining"}
HUMAN_KINDS = ("pr_review_comment", "pr_review_body", "pr_issue_comment", "replicant_command")
# MEM-D13: agent-generated sources. Lower prior, labelled machine-sourced, approval needs a human-sourced row.
MACHINE_KINDS = ("review_finding", "qa_failure")
# MEM-D12: candidates from these sources (or from any repair cycle) wait for the PR outcome (hold awaiting_merge).
PR_DERIVED_KINDS = ("pr_review_comment", "pr_review_body", "pr_issue_comment", "replicant_command", "repair_rejection",
                    "human_edit")
REVIEW_DUE_DAYS = int(os.environ.get("REPLICANT_MEMORY_REVIEW_DUE_DAYS", "90"))   # MEM-D14 review_due_at on approval
STALE_DAYS = int(os.environ.get("REPLICANT_MEMORY_STALE_DAYS", "90"))             # unused this long -> "stale" badge
CHURN_FLAG_PRS = int(os.environ.get("REPLICANT_MEMORY_CHURN_FLAG_PRS", "3"))      # merged PRs on an entry's paths
SOURCE_PREFIX = {"pr_review_comment": "gh-review-comment", "pr_review_body": "gh-review", "pr_issue_comment":
                 "gh-issue-comment", "replicant_command": "gh-issue-comment", "gate_comment": "gate",
                 "review_finding": "review", "qa_failure": "qa", "agent_proposal": "agent", "import": "scan",
                 "job_outcome": "outcome", "repair_rejection": "agent", "curator": "agent", "human_edit": "edit"}
TRANSITIONS = {"approve": (("candidate",), "active"), "reject": (("candidate",), "rejected"),
               "disable": (("active",), "disabled"), "deprecate": (("active", "disabled"), "deprecated"),
               "restore": (("disabled", "deprecated", "quarantined"), "active"),
               "quarantine": (("candidate", "active", "disabled", "deprecated"), "quarantined")}
ID_RE = {"job": re.compile(r"^job_[0-9a-hjkmnp-tv-z]{26}$"), "run": re.compile(r"^run_[0-9a-hjkmnp-tv-z]{26}$"),
         "usr": re.compile(r"^usr_[0-9a-hjkmnp-tv-z]{26}$"), "rb": re.compile(r"^rb_[0-9a-hjkmnp-tv-z]{26}$")}


def _ok(kind: str, v: Any) -> Any:
    return v if isinstance(v, str) and ID_RE[kind].match(v) else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_:./-]", "-", s)[:200]


# ================================================================================================ read helpers
ITEM_SQL = """
SELECT e.entry_id, v.version, e.memory_type, v.scope_level, v.repo, v.statement, v.title, v.path_globs, v.roles,
       e.enforced, e.confidence::float AS confidence, e.created_at, e.last_confirmed_at, e.state{extra}
  FROM memory.entries e JOIN memory.entry_versions v ON v.entry_id = e.entry_id AND v.version = e.current_version
"""


def _item(r: dict[str, Any]) -> core.Item:
    return core.Item(entry_id=r["entry_id"], version=r["version"], memory_type=r["memory_type"],
                     scope_level=r["scope_level"], repo=r["repo"], statement=r["statement"] or "", title=r.get("title"),
                     path_globs=list(r.get("path_globs") or []), roles=list(r.get("roles") or []),
                     enforced=bool(r.get("enforced")), confidence=float(r.get("confidence") or 0.5),
                     created_at=r.get("created_at"), last_confirmed_at=r.get("last_confirmed_at"))


async def _emit(conn: AsyncConnection, ev: dict[str, Any]) -> str:
    """audit.emit inside a SAVEPOINT (control-plane-api's pattern); a failure is logged and an aud_ minted."""
    full = {"occurred_at": _now().isoformat(), "source_service": "memory-service", "channel": "ui",
            "outcome": "success", **ev}
    try:
        async with conn.begin_nested():
            aid = (await conn.execute(text("SELECT audit.emit(CAST(:e AS jsonb))"), {"e": jdump(full)})).scalar_one()
            log.info(json.dumps({"log_type": "audit", "audit_event_id": aid, "event_type": ev.get("event_type")}))
            return aid
    except Exception as e:  # noqa: BLE001
        log.warning("audit.emit failed (%s: %s)", type(e).__name__, str(e).splitlines()[0][:200])
    return ulid("aud")


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, datetime):
        return v.isoformat()
    if hasattr(v, "is_finite"):  # Decimal
        return float(v)
    return v
