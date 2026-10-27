"""PR review loop (D-15): feedback.triage and the repair-run inputs. Fake S3 and a stubbed gateway call."""
from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

import fakes
from replicant_worker.activities import config, feedback, llm, s3, staging

JOB = "job_01m5z3xz399c11r2jh39kz69de"
ART = "local-replicant-local-artifacts"


@pytest.fixture
def fs3(monkeypatch):
    monkeypatch.setenv("REPLICANT_ENV", "local")
    config.settings.cache_clear()
    f = fakes.FakeS3()
    s3.set_client(f)
    yield f
    s3.set_client(None)
    config.settings.cache_clear()


def run(fn, arg: Any) -> Any:  # noqa: ANN001
    e = ActivityEnvironment()
    e.info = dataclasses.replace(e.info, workflow_id=JOB, activity_id="triage.c1.n1.run")
    return asyncio.run(e.run(fn, arg))


def payload(fs3: fakes.FakeS3, delivery: str, comments: list[dict]) -> str:
    key = f"tenants/payments/jobs/{JOB}/events/pr/{delivery}.json"
    fs3.objs[(ART, key)] = json.dumps({
        "delivery_id": delivery, "repo": "git.replicant.localhost:3300/acme-payments/ledger-api", "pr_number": 7,
        "review_state": "commented", "received_at": "2026-10-27T10:00:00Z",
        "actor": {"id": 5, "login": "carol", "principal_id": "usr_carol", "repo_permission": "write"},
        "comments": comments}).encode()
    return f"s3://{ART}/{key}"


def test_parse_triage() -> None:
    assert feedback.parse_triage('```json\n{"classification":"actionable_fix","confidence":0.9,"summary":"s"}\n```') == \
        ("actionable_fix", 0.9, "s")
    assert feedback.parse_triage('{"classification":"actionable_fix","confidence":0.3}')[0] == "question"
    assert feedback.parse_triage("OK.")[0] == "question"
    assert feedback.outcome_of([{"classification": "no_action"}, {"classification": "question"}]) == "question"


def test_triage_writes_mem01_items_and_stages_repair_input(fs3, monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_complete(**kw: Any) -> str:
        calls.append(kw)
        if "why" in kw["user"]:
            return '{"classification": "question", "confidence": 0.8, "summary": "asks why"}'
        return '{"classification": "actionable_fix", "confidence": 0.95, "summary": "add a doc comment"}'
    monkeypatch.setattr(llm, "complete", fake_complete)
    uri = payload(fs3, "d1", [
        {"comment_id": 91, "kind": "review_comment", "body": "Add a doc comment to Prorate", "path": "a.go", "line": 3,
         "review_id": 4, "url": "http://g/pulls/7#c91", "no_memory": False, "remember": False},
        {"comment_id": 4, "kind": "review_body", "body": "why half-even?", "review_id": 4},
        {"comment_id": 92, "kind": "issue_comment", "body": "/replicant no-memory", "command_only": True,
         "no_memory": True}])
    r = run(feedback.feedback_triage, {"job_id": JOB, "tenant_id": "payments", "cycle": 1,
                                        "events": [{"delivery_id": "d1", "payload_uri": uri, "event": "review_submitted"}]})
    assert r["outcome"] == "actionable" and r["actionable"] == 1
    assert len(calls) == 2 and all(c["script_key"] == "feedback_triage" for c in calls)
    assert all('<untrusted_input source="pr_review">' in c["user"] for c in calls)
    doc = json.loads(fs3.objs[(ART, f"tenants/payments/jobs/{JOB}/steps/triage.c1.n1/feedback_triage.json")])
    fix = doc["items"][0]
    assert fix["source_kind"] == "pr_review_comment" and fix["classification"] == "actionable_fix"
    assert fix["memory_directive"] == "no_memory"  # the author's command-only comment applies to the batch
    assert fix["author_principal_id"] == "usr_carol" and fix["author_repo_permission"] == "write"
    assert fix["source_url"].startswith("http") and fix["pr_number"] == 7 and fix["text"].startswith("Add a doc")
    # a retry keeps the first classification
    monkeypatch.setattr(llm, "complete", None)
    assert run(feedback.feedback_triage, {"job_id": JOB, "tenant_id": "payments", "cycle": 1, "events": []}) == r
    block = staging._pr_review_block(r["triage_uri"])
    assert '<untrusted_input source="pr_review" id="91" path="a.go" line="3">' in block
    assert "why half-even" not in block  # only actionable fixes go to the repair run
