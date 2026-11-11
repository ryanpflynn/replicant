"""inventory.* activities (design §8c, onboarding-interfaces.md §7): what each agent run loaded, skipped and failed,
and the readiness probe's result, sent to control-plane-api (the only writer of schema `inventory`).

* inventory.record_observations   after each agent run of a job with a pinned repo profile:
                                  POST /internal/v1/inventory/observations, built from the run result's
                                  `role_result.cli.repo_config` section and, for QA runs, the failed stages' causes
* inventory.record_readiness      at the end of a readiness job (label replicant-readiness):
                                  PUT /internal/v1/repos/{repo_id}/readiness with ready | warnings | blocked

Both are best effort, like memory.* (activities/memory.py): a failure never changes the job's outcome. The worker's
caller token (REPLICANT_CONTROL_PLANE_TOKEN_FILE) authenticates, as for notify.*.
"""
from __future__ import annotations

import re
from typing import Any

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from replicant_worker.activities import s3
from replicant_worker.activities.base import http, implements, retryable_http
from replicant_worker.activities.config import read_secret, settings

PROBE_CAUSES = ("missing_tool", "network", "timeout", "test_failure", "build_failure", "install_failure",
                "lint_failure")
BLOCKING_CAUSES = frozenset({"missing_tool", "network", "timeout", "install_failure", "build_failure"})
ENVIRONMENT_CAUSES = frozenset({"missing_tool", "network", "timeout", "install_failure"})
STAGE_CAUSE = {"install": "install_failure", "bootstrap": "install_failure", "prepare": "install_failure",
               "build": "build_failure", "image_build": "build_failure", "lint": "lint_failure"}
RUN_FAILURE_CAUSE = {"EnvironmentSetupFailed": "install_failure", "DeadlineExceeded": "timeout",
                     "ActiveDeadlineExceeded": "timeout", "StartupTimeout": "timeout", "AdmissionDenied": "missing_tool"}
_DETAIL = re.compile(r"[^A-Za-z0-9._@/+-]+")


def _detail(v: Any) -> str:
    return _DETAIL.sub("-", str(v or "")).strip("-")[:64]


def _run_result(uri: str | None) -> dict[str, Any]:
    if not uri:
        return {}
    try:
        doc = s3.get_json(s3.parse(uri))
    except ApplicationError as e:
        if e.type == "ObjectNotFound":
            return {}
        raise
    return doc if isinstance(doc, dict) else {}


# ------------------------------------------------------------------------------------------------ pure parts
def qa_causes(role_result: dict[str, Any] | None, *, failure_reason: str | None = None) -> list[dict[str, Any]]:
    """[{stage, cause, detail}] of a QA run: each failed stage's classified cause (replicant-qa, run report §6); a
    stage without one is classified by its name. A run that produced no QA report fails as a whole."""
    out: list[dict[str, Any]] = []
    for st in (role_result or {}).get("stages") or []:
        if not isinstance(st, dict) or st.get("status") not in ("failed", "error"):
            continue
        stage = str(st.get("stage") or "unknown")
        cause = st.get("cause") if st.get("cause") in PROBE_CAUSES else STAGE_CAUSE.get(stage, "test_failure")
        out.append({"stage": stage, "cause": cause, "detail": str(st.get("detail") or "")[:256] or None})
    if not out and failure_reason:
        out.append({"stage": "prepare", "cause": RUN_FAILURE_CAUSE.get(failure_reason, "install_failure"),
                    "detail": failure_reason[:256]})
    return out[:40]


def readiness_status(verdict: str | None, causes: list[dict[str, Any]]) -> str:
    """The QA verdict of the probe (replicant-qa: a failing lint is pass_with_warnings; a missing tool, the network or
    a failed install is inconclusive; a failing test or build is fail): pass -> ready, pass_with_warnings (or nothing to
    run, not_applicable) -> warnings, anything else (fail, inconclusive, no verdict) -> blocked. A verdict-less run with an environment cause is
    blocked too."""
    if verdict == "pass" and not {c.get("cause") for c in causes} & BLOCKING_CAUSES:
        return "ready"
    if verdict in ("pass_with_warnings", "not_applicable") and not {c.get("cause") for c in causes} & BLOCKING_CAUSES:
        return "warnings"
    # Failing tests on the default branch don't stop Replicant working on the repo (fixing them may be the job): the
    # environment ran them, so the repo is usable, with a warning.
    if verdict == "fail" and causes and {c.get("cause") for c in causes} <= {"test_failure", "lint_failure"}:
        return "warnings"
    return "blocked"


def _mcp_identity(m: dict[str, Any]) -> str:
    """stdio:<command>:<pinned package> (onboarding-interfaces.md §3): the first non-flag argument."""
    pkg = next((str(x) for x in m.get("args") or [] if not str(x).startswith("-")), "")
    return f"stdio:{m.get('command', '')}:{pkg}" if pkg else f"stdio:{m.get('command', '')}"


def observation_items(role_result: dict[str, Any] | None, *, environment: str, mcp_auto_allow: list[dict[str, Any]],
                      qa_causes_list: list[dict[str, Any]] | None = None, all_causes: bool = False) -> list[dict[str, Any]]:
    """Catalogue items one run observed (kinds mcp_server, hook, probe_failure). From `cli.repo_config`: hooks
    (loaded, or failed with the classified cause), skipped and allowed repo MCP servers. From QA: probe_failure items,
    every cause for a readiness probe (`all_causes`), environment causes only for a job's QA (a failing test there is
    usually the change, not the environment)."""
    rr = role_result or {}
    rc = (rr.get("cli") or {}).get("repo_config") or {}
    items: list[dict[str, Any]] = []
    for h in rc.get("hooks") or []:
        if not isinstance(h, dict) or not h.get("command_hash"):
            continue
        failed = int(h.get("failures") or 0) > 0
        items.append({"kind": "hook", "identity": f"hook:{h.get('event', 'unknown')}:{h['command_hash']}",
                      "status": "failed" if failed else "loaded",
                      "cause": (h.get("cause") or "non_zero_exit") if failed else None})
    for m in rc.get("mcp_skipped") or []:
        if isinstance(m, dict):
            items.append({"kind": "mcp_server", "identity": str(m.get("identity") or f"name:{m.get('name', '')}")[:512],
                          "status": "skipped", "cause": m.get("reason") or "not_allowed"})
    by_name = {str(m.get("name")): m for m in mcp_auto_allow or [] if isinstance(m, dict)}
    failed_mcp = {str(s.get("name")) for s in (rr.get("cli") or {}).get("mcp_servers") or []
                  if isinstance(s, dict) and s.get("status") in ("failed", "error")}
    for name in rc.get("mcp_allowed") or []:
        m = by_name.get(str(name))
        ident = _mcp_identity(m) if m else f"name:{name}"
        items.append({"kind": "mcp_server", "identity": ident[:512],
                      "status": "failed" if str(name) in failed_mcp else "loaded",
                      "cause": "start_failed" if str(name) in failed_mcp else None})
    for c in qa_causes_list or []:
        if not all_causes and c["cause"] not in ENVIRONMENT_CAUSES:
            continue
        d = _detail(c.get("detail"))
        items.append({"kind": "probe_failure", "identity": f"probe:{environment}:{c['cause']}" + (f":{d}" if d else ""),
                      "status": "failed", "cause": c["cause"]})
    seen: set[tuple[str, str, str]] = set()
    out = []
    for i in items:  # one item per (kind, identity, status) and run
        k = (i["kind"], i["identity"], i["status"])
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out[:200]


# ------------------------------------------------------------------------------------------------ control-plane-api
async def _call(method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """control-plane-api internal call. Unreachable or 4xx: logged and dropped (best effort); 5xx: retryable."""
    s = settings()
    tok = read_secret(s.control_plane_token_file)
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    try:
        r = await http().request(method, f"{s.control_plane_url}{path}", json=body, headers=headers,
                                 timeout=httpx.Timeout(20.0, connect=5.0))
    except httpx.HTTPError as e:
        activity.logger.warning("control-plane-api unreachable; inventory record dropped",
                                extra={"error": type(e).__name__, "path": path})
        return {"recorded": False, "reason": "unreachable"}
    if r.status_code in (200, 201, 202, 204):
        return {"recorded": True}
    if r.status_code < 500:
        activity.logger.warning("inventory record refused", extra={"status": r.status_code, "path": path})
        return {"recorded": False, "reason": f"http_{r.status_code}"}
    raise retryable_http(f"{method} {path}", r)


@implements("inventory.record_observations")
async def inventory_record_observations(a: dict[str, Any]) -> dict[str, Any]:
    """One run's observations (onboarding-interfaces.md §7). Input: job_id, run_id, repo_id, workflow_type, role,
    result_uri, environment, mcp_auto_allow, tier {tier, model}, readiness (bool)."""
    try:
        res = _run_result(a.get("result_uri"))
    except ApplicationError as e:  # best effort: an unreadable result is not worth a retry storm
        activity.logger.warning("run result unreadable; no observations", extra={"error": str(e)[:200]})
        return {"recorded": False, "reason": "result_unreadable"}
    rr = res.get("role_result") or {}
    causes = qa_causes(rr, failure_reason=a.get("failure_reason")) if a.get("role") == "qa" else []
    items = observation_items(rr, environment=str(a.get("environment") or "unknown"),
                              mcp_auto_allow=list(a.get("mcp_auto_allow") or []), qa_causes_list=causes,
                              all_causes=bool(a.get("readiness")))
    tier = a.get("tier") or {}
    if not items and not tier:
        return {"recorded": False, "reason": "nothing_observed", "items": 0}
    body = {"job_id": a["job_id"], "run_id": a["run_id"], "repo_id": a.get("repo_id"),
            "workflow_type": a.get("workflow_type"), "role": a.get("role"), "items": items,
            "tier": tier.get("tier"), "model": tier.get("model"),
            "settings_hash": ((rr.get("cli") or {}).get("repo_config") or {}).get("settings_hash")}
    return {**await _call("POST", "/internal/v1/inventory/observations", body), "items": len(items)}


@implements("inventory.record_readiness")
async def inventory_record_readiness(a: dict[str, Any]) -> dict[str, Any]:
    """The readiness probe's result (onboarding-interfaces.md §7). Input: job_id, repo_id, profile_id, outcome,
    verdict (the last QA verdict), qa_result_uri, qa_failure_reason."""
    try:
        rr = (_run_result(a.get("qa_result_uri")).get("role_result") or {})
    except ApplicationError as e:
        activity.logger.warning("QA result unreadable; readiness from the verdict only", extra={"error": str(e)[:200]})
        rr = {}
    causes = qa_causes(rr, failure_reason=a.get("qa_failure_reason"))
    verdict = a.get("verdict") or rr.get("verdict")
    if not causes and verdict in (None, "inconclusive"):
        causes = [{"stage": "prepare", "cause": "install_failure", "detail": str(a.get("outcome") or "no_qa_result")[:256]}]
    status = readiness_status(verdict, causes)
    body = {"job_id": a["job_id"], "profile_id": a.get("profile_id"), "status": status, "causes": causes}
    return {**await _call("PUT", f"/internal/v1/repos/{a['repo_id']}/readiness", body), "status": status}
