"""`replicant_runner_client` + runner.* activities (WS-01 graphs/README.md §2.4; C-RUNNER 3.0.0).

`runner.start_run` receives the RunSpec decision fields from workflow code, adds the per-attempt fields (`run_grant`,
`trace_context`), computes `spec_digest` and signs the grant (ES256 file key locally), then POSTs with
`Idempotency-Key: "ik1:<workflow_id>:<logical_activity_id>"`. Problem codes map to ApplicationError types; a
`retryable: false` problem is non-retryable; 429 honours Retry-After; `run_grant_invalid` is re-signed once.
"""
from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import Any

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from replicant_worker import ids
from replicant_worker.activities import grants, s3
from replicant_worker.activities.base import http, implements, problem
from replicant_worker.activities.config import read_secret, settings

NON_RETRYABLE_CODES = {"invalid_request", "missing_idempotency_key", "forbidden", "run_not_found", "run_expired",
                       "idempotency_key_reuse", "admission_denied", "definition_not_resolvable",
                       "steering_not_supported", "run_not_active", "run_grant_invalid"}
OUTPUT_BUCKET = {"session_state": "session", "workspace_archive": "session"}


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    tok = read_secret(settings().runner_token_file)
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    tp = os.environ.get("TRACEPARENT")
    if tp:
        h["traceparent"] = tp
    return {**h, **(extra or {})}


def runner_error(what: str, r: httpx.Response) -> ApplicationError:
    p = problem(r)
    code = str(p.get("code") or f"http_{r.status_code}")
    retryable = p.get("retryable")
    if retryable is None:
        retryable = r.status_code >= 500 or r.status_code in (401, 429) or code not in NON_RETRYABLE_CODES
    if code in NON_RETRYABLE_CODES:
        retryable = False
    if r.status_code == 429 and code == "tenant_quota_exceeded":
        # XD-FIX-R2-020: typed, non-retryable for the activity; the workflow waits with a durable timer
        return ApplicationError("tenant run quota exceeded", {
            "reason": "tenant_run_quota", "quota_used": p.get("quota_used"), "quota_limit": p.get("quota_limit"),
            "retry_after_seconds": int(r.headers.get("Retry-After") or p.get("retry_after_seconds") or 30)},
            type="CapacityUnavailable", non_retryable=True)
    kw: dict[str, Any] = {}
    ra = r.headers.get("Retry-After") or p.get("retry_after_seconds")
    if ra and str(ra).isdigit():
        kw["next_retry_delay"] = timedelta(seconds=min(int(ra), 120))
    detail = {k: p[k] for k in ("code", "title", "detail", "status") if k in p}
    return ApplicationError(f"{what}: HTTP {r.status_code} {code}", detail, type=code, non_retryable=not retryable, **kw)


async def _post_run(run_spec: dict[str, Any], key: str) -> httpx.Response:
    s = settings()
    body = json.dumps(run_spec, separators=(",", ":"))
    return await http().post(f"{s.runner_url}/v1/runs", content=body,
                             headers=_headers({"Idempotency-Key": json.dumps(key)}))


def _signed(a: dict[str, Any], run_spec: dict[str, Any]) -> dict[str, Any]:
    signer = grants.signer()
    claims = grants.grant_claims(run_spec=run_spec, run_id=a["run_id"], grant=a["grant"], issuer=signer.issuer)
    return {**run_spec, "run_grant": signer.sign(claims)}


@implements("runner.start_run")
async def runner_start_run(a: dict[str, Any]) -> dict[str, Any]:
    key = a["idempotency_key"]
    spec = json.loads(json.dumps(a["run_spec"]))
    expected = ids.run_id_for(a["tenant_id"], key)
    if expected != a["run_id"]:
        raise ApplicationError("run_id is not derived(run, tenant, key)", type="InvalidInput", non_retryable=True)
    tp = os.environ.get("TRACEPARENT")
    if tp:
        spec["trace_context"] = {"traceparent": tp}
    r = await _post_run(_signed(a, spec), key)
    if r.status_code == 422 and problem(r).get("code") == "run_grant_invalid":
        activity.logger.warning("run_grant_invalid; re-signing once (XD-02-14)")
        r = await _post_run(_signed(a, spec), key)
    if r.status_code not in (200, 201):
        raise runner_error("POST /v1/runs", r)
    st = r.json()
    if st.get("run_id") != a["run_id"]:
        raise ApplicationError(f"Runner returned {st.get('run_id')} for {a['run_id']}", type="RunIdMismatch",
                               non_retryable=True)
    return st


async def _ensure_run_policy(run_id: str, p: dict[str, Any]) -> None:
    """XD-FIX-R1-286: the shim keeps run policies in memory (GATEWAY-D1); re-PUT one that is gone (404)."""
    from replicant_worker.activities.staging import policy_body, put_run_policy
    s = settings()
    tok = read_secret(s.gateway_token_file)
    try:
        r = await http().get(f"{s.gateway_url}/internal/v1/runs/{run_id}/policy",
                             headers={"Authorization": f"Bearer {tok}"} if tok else {})
    except httpx.HTTPError:
        return  # the run wait never fails on the gateway
    if r.status_code == 404:
        await put_run_policy(run_id, policy_body(
            tenant_id=p["tenant_id"], job_id=p["job_id"], role=p["role"], release_bundle_id=p["release_bundle_id"],
            models=list(p.get("models") or []), budget_usd=float(p["budget_usd"]),
            max_concurrency=int(p.get("max_concurrency") or 4), lifetime_seconds=int(p.get("expires_after_seconds") or 7200)))
        activity.logger.warning("gateway run policy re-registered", extra={"run_id": run_id})


@implements("runner.get_run")
async def runner_get_run(a: dict[str, Any]) -> dict[str, Any]:
    r = await http().get(f"{settings().runner_url}/v1/runs/{a['run_id']}", headers=_headers())
    if r.status_code != 200:
        raise runner_error("GET /v1/runs/{id}", r)
    st = r.json()
    if a.get("ensure_run_policy") and not st.get("terminal"):
        try:
            await _ensure_run_policy(a["run_id"], a["ensure_run_policy"])
        except ApplicationError as e:
            activity.logger.warning("run policy check failed", extra={"error": str(e)[:200]})
    return st


@implements("runner.cancel_run")
async def runner_cancel_run(a: dict[str, Any]) -> dict[str, Any]:
    body = {"reason": a.get("reason", "job_cancelled")}
    if a.get("detail"):
        body["detail"] = str(a["detail"])[:500]
    r = await http().post(f"{settings().runner_url}/v1/runs/{a['run_id']}:cancel", json=body, headers=_headers())
    if r.status_code == 404:
        return {}
    if r.status_code not in (200, 202):
        raise runner_error("POST /v1/runs/{id}:cancel", r)
    return r.json()


@implements("runner.post_message")
async def runner_post_message(a: dict[str, Any]) -> dict[str, Any]:
    body = {k: a[k] for k in ("message_id", "text", "author", "sent_at", "kind") if k in a}
    r = await http().post(f"{settings().runner_url}/v1/runs/{a['run_id']}/messages", json=body,
                          headers=_headers({"Idempotency-Key": json.dumps(str(a.get("message_id")))}))
    if r.status_code not in (200, 201, 202):
        raise runner_error("POST /v1/runs/{id}/messages", r)
    return r.json()


def role_exports(res: dict[str, Any]) -> dict[str, Any]:
    rr = res.get("role_result") or {}
    out: dict[str, Any] = {}
    diff = rr.get("diff") or rr.get("changes") or {}
    if isinstance(diff, dict):
        if "files_changed" in diff:
            out["files_changed"] = diff["files_changed"]
        if "paths" in diff:
            out["paths"] = list(diff["paths"])[:200]
    for k in ("files_changed", "conflicts", "paths", "verdict", "reason_code"):
        if k in rr and k not in out:
            out[k] = rr[k]
    ar = rr.get("agent_report") or {}
    if "verdict" in ar and "verdict" not in out:
        out["verdict"] = ar["verdict"]
    return out


@implements("runner.read_result")
async def runner_read_result(a: dict[str, Any]) -> dict[str, Any]:
    """Validate and summarise run_result (C-RUNNER run-result.schema.json): outcome, output URIs, usage, role exports."""
    ref = s3.parse(a["result_uri"])
    res = s3.get_json(ref)
    if not isinstance(res, dict) or res.get("schema") != "replicant.run-result/v1" or res.get("run_id") != a["run_id"]:
        raise ApplicationError("run_result invalid or for another run", type="SchemaViolation", non_retryable=True)
    s = settings()
    outputs = {name: f"s3://{s.bucket(OUTPUT_BUCKET.get(name, 'artifacts'))}/{key}"
               for name, key in (res.get("outputs") or {}).items() if isinstance(key, str) and key}
    return {"outcome": res.get("outcome"), "outputs": outputs, "usage": res.get("usage"),
            "memory_entries_used": res.get("memory_entries_used") or [], "role_exports": role_exports(res),
            "summary_len": len(res.get("summary") or "")}
