"""replicant_db.revision — startup schema-compatibility check (C-PG rule 8, as amended by CCR-10-01).

Every service image embeds BUILT_REVISION (the Alembic head of the replicant-db package it was
built with; generated into replicant_db/_build.py by CI). At startup, before readiness, the
service runs `check_schema_compat()`. Readiness (`/readyz`) stays false and the process exits
non-zero after 10 attempts × 30 s when the result is not OK.

Why not strict equality (the literal reading of C-PG rule 8): with expand → migrate → contract
across separate releases, the database is legitimately *ahead* of running pods for the whole
rollout, and replicant-worker builds pinned by long-running workflows stay alive for up to 37 days
(ADR-006, CC-4) while the schema advances. Strict equality would make every such pod fail on its
next restart (node rotation, OOM, eviction). The rule implemented here keeps the intent ("never
run against a schema you were not built for") while allowing schemas that are provably
backward-compatible:

    OK        current == built
    OK        built is an ancestor of current, and every contract migration X strictly after
              `built` has X.requires_reader_revision at or before `built`
    BEHIND    built is not in the applied chain (DB older than the image, or a different branch)
    TOO_NEW   a contract migration after `built` requires readers newer than `built`

The migrate Job enforces the mirror rule before applying a contract migration: no live workload
may be built before its requires_reader_revision (schema-management.md §7.4).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

try:  # generated at build time
    from replicant_db._build import BUILT_REVISION  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - source checkouts
    BUILT_REVISION = "unknown"


BACKWARD_COMPATIBLE = frozenset({"expand", "data", "reference", "grants"})   # C-PG 1.2.0 rule 8


class Compat(enum.Enum):
    OK = "ok"
    BEHIND = "db_behind_image"
    TOO_NEW = "db_requires_newer_image"
    NO_SCHEMA = "no_schema"


@dataclass(frozen=True)
class CompatResult:
    status: Compat
    built: str
    current: str | None
    blocking_revision: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is Compat.OK


_LOG_QUERY = text(
    """
    SELECT v.version_num AS current,
           l.revision, l.down_revision, l.compat, l.requires_reader_revision
    FROM ops.alembic_version v
    LEFT JOIN ops.migration_log l ON l.downgraded_at IS NULL
    """
)


def evaluate(built: str, current: str | None, log: dict[str, tuple[str | None, str, str | None]]) -> CompatResult:
    """Pure function (unit-tested). log: revision -> (down_revision, compat, requires_reader_revision)."""
    if current is None:
        return CompatResult(Compat.NO_SCHEMA, built, None)
    if current == built:
        return CompatResult(Compat.OK, built, current)
    after_built: list[str] = []
    rev: str | None = current
    while rev is not None and rev != built:
        if rev not in log:
            return CompatResult(Compat.BEHIND, built, current)
        after_built.append(rev)
        rev = log[rev][0]
    if rev is None:  # walked to the root without meeting `built`
        return CompatResult(Compat.BEHIND, built, current)
    newer = set(after_built)
    for r in reversed(after_built):  # oldest first, so the first blocker is reported
        _, compat, needs = log[r]
        if compat in BACKWARD_COMPATIBLE:
            continue
        if compat == "contract" and needs is not None and needs not in newer:
            continue
        # contract requiring a newer reader, or any class C-PG rule 8 does not list: refuse
        return CompatResult(Compat.TOO_NEW, built, current, blocking_revision=r)
    return CompatResult(Compat.OK, built, current)


async def check_schema_compat(engine: AsyncEngine, built: str = BUILT_REVISION,
                              mode: str | None = None) -> CompatResult:
    """mode = REPLICANT_SCHEMA_REVISION_CHECK: 'compatible' (default; required in dev/staging/prod
    because of expand/contract and pinned workers) or 'strict' (equality; allowed for local/CI,
    where migrate always runs first). 'off' does not exist."""
    import os

    mode = mode or os.environ.get("REPLICANT_SCHEMA_REVISION_CHECK", "compatible")
    if mode not in {"compatible", "strict"}:
        raise ValueError(f"REPLICANT_SCHEMA_REVISION_CHECK={mode!r}")
    if mode == "strict" and os.environ.get("REPLICANT_ENV") in {"dev", "staging", "prod"}:
        raise ValueError("strict revision check is local/ci only (CCR-10-01)")
    async with engine.connect() as conn:
        rows = (await conn.execute(_LOG_QUERY)).all()
    current = rows[0].current if rows else None
    if mode == "strict":
        return CompatResult(Compat.OK if current == built else Compat.BEHIND if current else Compat.NO_SCHEMA,
                            built, current)
    log = {r.revision: (r.down_revision, r.compat, r.requires_reader_revision) for r in rows if r.revision}
    return evaluate(built, current, log)


# Prometheus (prometheus_client), exported by every service that talks to the replicant DB, with the label sets of
# WS-08's catalog (08 telemetry-model.md §8; C-IDS §11 label rules; R1V-179):
#   replicant_db_schema_compat{service, outcome}            gauge: 1 when the check fails, 0 when outcome="ok"
#   replicant_db_schema_revision_info{service, kind, revision} 1   (kind = built | current)
#   replicant_db_schema_check_timestamp_seconds{service}
# Alert ReplicantDbSchemaIncompatible (08 rules/mimir/replicant-rules.yaml):
#   max by (service) (replicant_db_schema_compat{outcome!="ok"}) > 0 for 5m
METRIC_OUTCOME = {"ok": "ok", "db_behind_image": "behind", "db_requires_newer_image": "ahead"}


def metric_outcome(result: "CompatResult") -> str:
    """The catalog's `outcome` label value for a compatibility result (unknown for anything unmapped)."""
    return METRIC_OUTCOME.get(result.status.value, "unknown")


if __name__ == "__main__":  # minimal self-test, run in CI as `python -m replicant_db.revision`
    L = {
        "202610240001_ops": (None, "grants", None),
        "202611010900_jobs_read": ("202610240001_ops", "expand", None),
        "202611080900_jobs_read": ("202611010900_jobs_read", "data", None),
        "202611220900_jobs_read": ("202611080900_jobs_read", "contract", "202611010900_jobs_read"),
    }
    assert evaluate("202611220900_jobs_read", "202611220900_jobs_read", L).ok
    assert evaluate("202611010900_jobs_read", "202611220900_jobs_read", L).ok           # built after expand
    r = evaluate("202610240001_ops", "202611220900_jobs_read", L)                           # built before expand
    assert r.status is Compat.TOO_NEW and r.blocking_revision == "202611220900_jobs_read"
    assert evaluate("202611080900_jobs_read", "202611010900_jobs_read", L).status is Compat.BEHIND
    assert metric_outcome(evaluate("202611080900_jobs_read", "202611010900_jobs_read", L)) == "behind"
    assert evaluate("202610240001_ops", "202611080900_jobs_read", L).ok                     # expand + data only
    print("revision compat self-test ok")
