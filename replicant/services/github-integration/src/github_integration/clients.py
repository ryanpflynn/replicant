"""Outbound clients: control-plane-api intake, github-token-broker, S3 (pr_event_payload), Temporal (pr.event)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from functools import cached_property
from typing import Any

import httpx

from github_integration.config import Settings, read_secret

log = logging.getLogger(__name__)


@dataclass
class IntakeResponse:
    status: int
    body: dict[str, Any]


class ControlPlane:
    def __init__(self, s: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.s = s
        self._transport = transport

    async def submit_job(self, jr: dict[str, Any], idempotency_key: str) -> IntakeResponse:
        tok = read_secret(self.s.control_plane_token_file)
        headers = {"Idempotency-Key": f'"{idempotency_key}"', "Content-Type": "application/json"}
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        async with httpx.AsyncClient(base_url=self.s.control_plane_url, timeout=15, transport=self._transport) as c:
            r = await c.post("/v1/jobs", json=jr, headers=headers)
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:300]}
        return IntakeResponse(r.status_code, body)

    async def put_profile(self, repo_id: str, profile: dict[str, Any]) -> int:
        """Inventory scanner -> control-plane-api `PUT /internal/v1/repos/{repo_id}/profile` (ONBOARD-01 §1); the
        same internal caller token as job submission (svc_github-integration)."""
        tok = read_secret(self.s.control_plane_token_file)
        headers = {"Content-Type": "application/json"}
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        async with httpx.AsyncClient(base_url=self.s.control_plane_url, timeout=30, transport=self._transport) as c:
            r = await c.put(f"/internal/v1/repos/{repo_id}/profile", json=profile, headers=headers)
        if r.status_code >= 300:
            log.warning("profile PUT %s: HTTP %s %s", repo_id, r.status_code, r.text[:200])
        return r.status_code


class Broker:
    """Installation tokens from github-token-broker (github-integration may hold issues/PR/checks write)."""

    def __init__(self, s: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.s = s
        self._transport = transport

    async def token(self, installation_id: int, repo_id: str, purpose: str, job_id: str | None = None,
                    permissions: dict[str, str] | None = None) -> str:
        tok = read_secret(self.s.token_broker_token_file)
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        body: dict[str, Any] = {"installation_id": installation_id, "repository_ids": [int(repo_id)],
                                "permissions": permissions or {"issues": "write", "pull_requests": "write"},
                                "purpose": purpose}
        if job_id:
            body["job_id"] = job_id
        async with httpx.AsyncClient(base_url=self.s.token_broker_url, timeout=15, transport=self._transport) as c:
            r = await c.post("/v1/installation-tokens", json=body, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"token broker answered {r.status_code}: {r.text[:200]}")
        return r.json()["token"]


class S3:
    def __init__(self, s: Settings) -> None:
        self.s = s

    @cached_property
    def client(self):  # noqa: ANN201
        import boto3
        from botocore.config import Config
        return boto3.client("s3", endpoint_url=self.s.s3_endpoint, region_name=self.s.s3_region,
                            config=Config(s3={"addressing_style": "path" if self.s.s3_force_path_style else "auto"},
                                          retries={"max_attempts": 3}, connect_timeout=5, read_timeout=10))

    async def put(self, bucket: str, key: str, body: bytes) -> None:
        await asyncio.to_thread(self.client.put_object, Bucket=bucket, Key=key, Body=body,
                                ContentType="application/json")


class Temporal:
    """github-integration holds only SignalWorkflowExecution (C-MSG §1). Default converter: no codec in M1."""

    def __init__(self, s: Settings, client: Any = None) -> None:
        self.s = s
        self._client = client
        self._lock = asyncio.Lock()

    async def client(self) -> Any:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    from replicant_temporal import TemporalSettings, connect
                    self._client = await asyncio.wait_for(connect(TemporalSettings(
                        address=self.s.temporal_address, namespace=self.s.temporal_namespace,
                        identity="github-integration")), timeout=10)
        return self._client

    async def signal(self, workflow_id: str, name: str, arg: dict[str, Any]) -> None:
        c = await self.client()
        await c.get_workflow_handle(workflow_id).signal(name, arg, rpc_timeout=dt.timedelta(seconds=10))
