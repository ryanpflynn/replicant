"""WS-05 QA activities (qa-activities-io.schema.json), v0: no contract stubs (no ecosystem in M1); classification
from the QA run's role_result (replicant.role-result.qa/v1) or, when there is none, from the run phase and failure."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import jsonschema
from temporalio.exceptions import ApplicationError

from replicant_worker import resources
from replicant_worker.activities import s3
from replicant_worker.activities.base import implements


@lru_cache(maxsize=None)
def _validator(name: str) -> jsonschema.Draft202012Validator:
    d = resources.load_json("05-qa/schemas/qa-activities-io.schema.json")
    return jsonschema.Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": d["$defs"]})


def check(name: str, doc: Any) -> None:
    errs = sorted(_validator(name).iter_errors(doc), key=lambda e: list(e.path))
    if errs:
        raise ApplicationError(f"{name}: {errs[0].message[:200]} at {list(errs[0].path)}", type="InvalidInput",
                               non_retryable=True)


@implements("qa.stage_contract_stubs")
async def qa_stage_contract_stubs(a: dict[str, Any]) -> dict[str, Any]:
    check("StageContractStubsInput", a)
    return {"staged": [], "missing": []}


EXIT_CLASS = {"pass": "pass", "pass_with_warnings": "pass_with_warnings", "fail": "fail", "not_applicable": "not_applicable"}
FLOOR = {"pass": "low", "pass_with_warnings": "low", "not_applicable": "low", "fail": "high", "inconclusive": "high"}


@implements("qa.classify")
async def qa_classify(a: dict[str, Any]) -> dict[str, Any]:
    check("ClassifyInput", a)
    rr: dict[str, Any] = {}
    outcome = None
    if a.get("result_uri"):
        res = s3.get_json(s3.parse(a["result_uri"]))
        outcome = res.get("outcome")
        rr = res.get("role_result") or {}
    if rr.get("schema") == "replicant.role-result.qa/v1" and rr.get("verdict"):
        verdict, reason = rr["verdict"], str(rr.get("reason_code") or "unknown")[:32]
        delta = rr.get("test_inventory_delta") or {}
        exit_class = rr.get("agentrun_exit_class") or EXIT_CLASS.get(verdict, "inconclusive_other")
        blocking = int(((rr.get("counts") or {}).get("failed")) or 0)
    elif a["run_phase"] == "Succeeded" and outcome:
        # C-RUNNER mapping (XD-FIX-R1-116): completed -> pass, no_changes -> not_applicable, failed -> fail
        verdict = {"completed": "pass", "no_changes": "not_applicable", "failed": "fail"}.get(outcome, "inconclusive")
        reason = {"pass": "all_passed", "fail": "regression", "not_applicable": "no_tests"}.get(verdict, "unknown_failure")
        delta, exit_class, blocking = {}, EXIT_CLASS.get(verdict, "inconclusive_other"), 1 if verdict == "fail" else 0
    else:
        env = a.get("failure_reason") in ("EnvironmentSetupFailed", "AdmissionDenied")
        verdict, reason = "inconclusive", "environment_failure" if env else "run_failed"
        delta, exit_class, blocking = {}, "environment_setup" if env else "inconclusive_other", 0
    inv = {"tests_removed": int(delta.get("tests_removed", 0)), "tests_skipped_added": int(delta.get("tests_skipped_added", 0)),
           "deleted_or_skipped": bool(delta.get("deleted_or_skipped", False))}
    key = f"tenants/{a['tenant_id']}/jobs/{a['job_id']}/steps/{a['step_id']}/qa_report/classified.json"
    report = {"schema": "replicant.qa-classified/v0", "run_id": a["run_id"], "verdict": verdict, "reason_code": reason,
              "run_phase": a["run_phase"], "failure_reason": a.get("failure_reason"), "qa_report_uri": a.get("qa_report_uri"),
              "role_result": rr or None, "blocking": blocking}
    put = s3.put_once(s3.ref("artifacts", key), json.dumps(report, sort_keys=True).encode(), "application/json")
    out: dict[str, Any] = {"verdict": verdict, "reason_code": reason, "oscillation": False,
                           "env_signature_transient": verdict == "inconclusive", "agentrun_exit_class": exit_class,
                           "risk_floor_hint": FLOOR[verdict], "classified_report_uri": put["uri"], "blocking": blocking,
                           "test_inventory_delta": inv}
    if verdict == "inconclusive":
        sig = "env_setup_failed" if exit_class == "environment_setup" else "run_failed"
        out["first_env_signature"] = sig
        out["env_failure"] = {"stage": "prepare", "check_id": "qa-run", "evidence_key": key, "definition_ref": "go-service@1.0.0",
                              "signature": sig}
    check("ClassifyOutput", out)
    return out


@implements("qa.index_results")
async def qa_index_results(a: dict[str, Any]) -> dict[str, Any]:  # registered by replicant-projector, not here
    raise ApplicationError("qa.index_results runs on replicant-projector", type="WrongWorker", non_retryable=True)
