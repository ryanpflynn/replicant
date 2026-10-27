"""In-worker LLM calls through the AI gateway (gateway.md §5.4: service caller `svc_replicant-worker`, purpose
`worker_llm`, tool-less by rule G-24). Used by feedback.triage (and available to memory extraction).

`script_key` names the scripted-gateway fixture (`X-Replicant-Script-Key`, tests/fixtures/gateway-scripts/<set>/
<script_key>.yaml); passthrough ignores it."""
from __future__ import annotations

import json
import re
from typing import Any

import httpx
from temporalio.exceptions import ApplicationError

from replicant_worker.activities.base import http, retryable_http
from replicant_worker.activities.config import read_secret, settings

DEFAULT_MODEL = "anthropic/claude-haiku-5-5"


async def complete(*, tenant_id: str, job_id: str, script_key: str, system: str, user: str,
                   model: str = DEFAULT_MODEL, max_tokens: int = 1024) -> str:
    """One tool-less Anthropic Messages call; returns the concatenated text blocks."""
    s = settings()
    tok = read_secret(s.gateway_token_file)
    headers = {"X-Replicant-Purpose": "worker_llm", "X-Replicant-Tenant-Id": tenant_id, "X-Replicant-Job-Id": job_id,
               "X-Replicant-Script-Key": script_key, "anthropic-version": "2023-06-01"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    body = {"model": model, "max_tokens": max_tokens, "system": system, "temperature": 0,
            "messages": [{"role": "user", "content": user}]}
    try:
        r = await http().post(f"{s.gateway_url}/anthropic/v1/messages", json=body, headers=headers,
                              timeout=httpx.Timeout(120.0, connect=5.0))
    except httpx.HTTPError as e:
        raise ApplicationError(f"gateway unreachable: {type(e).__name__}", type="GatewayUnavailable") from None
    if r.status_code != 200:
        if r.status_code in (400, 403, 422):
            raise ApplicationError(f"gateway refused worker_llm call: HTTP {r.status_code} {r.text[:200]}",
                                   type="InvalidInput", non_retryable=True)
        raise retryable_http("gateway worker_llm", r)
    d = r.json()
    return "".join(b.get("text", "") for b in d.get("content") or [] if b.get("type") == "text")


def parse_json_object(text: str) -> dict[str, Any] | None:
    """The first JSON object in a model reply (tolerates code fences and prose around it)."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except ValueError:
        return None
    return v if isinstance(v, dict) else None
