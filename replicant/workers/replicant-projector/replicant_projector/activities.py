"""Activities on task queue replicant-projections (deployment replicant-projector)."""
from __future__ import annotations

import json
import pathlib
from functools import lru_cache
from typing import Any

import jsonschema
from temporalio import activity
from temporalio.exceptions import ApplicationError

from replicant_projector import db

SCHEMA = pathlib.Path(__file__).resolve().parent / "schemas" / "workflow-messages.schema.json"


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    d = json.loads(SCHEMA.read_text())
    return jsonschema.Draft202012Validator({"$ref": "#/$defs/ProjectionEvent", "$defs": d["$defs"]})


def validate(ev: Any) -> None:
    errs = sorted(_validator().iter_errors(ev), key=lambda e: list(e.path))
    if errs:
        raise ApplicationError(f"invalid ProjectionEvent: {errs[0].message[:200]} at {list(errs[0].path)}",
                               type="SchemaViolation", non_retryable=True)


@activity.defn(name="projection.apply")
async def projection_apply(ev: dict[str, Any]) -> dict[str, Any]:
    """Idempotent, order-insensitive (C-STATE §2.1): every snapshot is complete; the seq guard drops older ones."""
    validate(ev)
    try:
        return await db.apply(ev)
    except db.psycopg.OperationalError as e:  # Postgres down: retried (unlimited within 24 h)
        raise ApplicationError(f"postgres unavailable: {str(e).splitlines()[0][:200]}", type="DatabaseUnavailable") from e


@activity.defn(name="qa.index_results")
async def qa_index_results(a: dict[str, Any]) -> dict[str, Any]:
    """WS-05 qa.index_results (XD-FIX-R1-118). v0: the minimal harness's per-test results are not indexed (no
    qa.test_results producer and no S3 read identity for the projector in M1); the call is acknowledged."""
    for k in ("job_id", "tenant_id", "run_id", "classified_report_uri", "decision"):
        if k not in a:
            raise ApplicationError(f"IndexResultsInput missing {k}", type="QaReportInvalid", non_retryable=True)
    activity.logger.info("qa results not indexed in v0", extra={"job_id": a["job_id"], "run_id": a["run_id"]})
    return {"indexed": False, "reason": "not_in_v0"}


ACTIVITIES = [projection_apply, qa_index_results]
