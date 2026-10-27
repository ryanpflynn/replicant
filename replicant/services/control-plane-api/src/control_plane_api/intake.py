"""Intake: `POST /v1/jobs` (architecture.md §2.3, C-JOBREQ 2.0.0, versioning.md §3.1).

Order: validate -> kill switch -> submitter rule -> requester authorization -> per-PR cap -> one transaction
(insert-or-get on (tenant_id, dedupe_key), bundle resolution in the same transaction, audit) -> commit -> start the
workflow PINNED to the bundle's worker build -> mark started. A failed start leaves `start_status='pending'`; the
intake retrier (every 5 s, advisory-lock singleton) restarts pending rows, and USE_EXISTING makes retries idempotent.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from control_plane_api import audit, contracts, derived, projects, registry
from control_plane_api.auth import AuthContext, check
from control_plane_api.authz import Principal
from control_plane_api.db import OPERATOR_SCOPE, Database
from control_plane_api.ids import ulid
from control_plane_api.problems import ProblemError
from control_plane_api.temporal_client import TemporalGateway

log = logging.getLogger(__name__)

# R1-161: service principals may submit only for their channel
SERVICE_CHANNELS = {
    "svc_github-integration": ("github", "user"),
    "svc_slack-integration": ("slack", "user"),
    "svc_jira-integration": ("jira", "user"),
    "svc_scheduler": ("schedule", "service"),
    "svc_eval-worker": ("eval", "service"),
    "svc_release-controller": ("eval", "service"),
}
INTAKE_RETRIER_LOCK = 0x46414354  # 'FACT'
INTAKE_RETRIER_LOCK2 = 1


@dataclass
class IntakeResult:
    status_code: int
    body: dict[str, Any]


def repo_ref(t: dict[str, Any]) -> str:
    r = t["repo"]
    return f"{r['host']}/{r['owner']}/{r['name']}"


class Intake:
    def __init__(self, db: Database, temporal: TemporalGateway, auth: AuthContext, env: str, web_origin: str) -> None:
        self.db = db
        self.temporal = temporal
        self.auth = auth
        self.env = env
        self.web_origin = web_origin.rstrip("/")
        self.app_state: Any = None     # app.state (S3 + settings for derived bundles); set by the app factory

    def ui_link(self, job_id: str) -> str:
        return f"{self.web_origin}/jobs/{job_id}"

    async def submit(self, principal: Principal, jr: dict[str, Any]) -> IntakeResult:
        # 1. contract validation
        errs = contracts.errors("job-request", jr)
        if errs:
            raise ProblemError(422, "invalid_job_request", detail="body does not validate against C-JOBREQ 2.0.0",
                               errors=errs)
        tenant = jr["tenant_id"]
        channel = jr["source"]["channel"]
        req = jr["requester"]
        if channel == "github" and (jr["source"].get("github") or {}).get("app_registration") == "dev" \
                and self.env in ("prod", "staging"):
            raise ProblemError(422, "dev_registration_rejected")
        # 2. kill switch (intake switch, global / tenant / repo scope; Postgres record; the mirror is M1-deferred)
        await self._check_intake_switch(tenant, [repo_ref(t) for t in jr.get("targets", [])])
        # 3. submitter rule
        if principal.is_user:
            if req["principal_id"] != principal.principal_id:
                raise ProblemError(403, "requester_mismatch", detail="a person may submit only for themself")
        else:
            rule = SERVICE_CHANNELS.get(principal.principal_id)
            if rule is None or rule[0] != channel or rule[1] != req["kind"]:
                raise ProblemError(403, "service_channel_not_allowed")
        # 4. authorize the requester (or on_behalf_of)
        subject_id = req.get("on_behalf_of") if req["kind"] == "service" else req["principal_id"]
        subject = self.auth.directory.get(subject_id)
        if subject is None:
            raise ProblemError(403, "identity_unlinked", detail="requester is not a known principal")
        repo_ids = tuple(t["repo"]["repo_id"] for t in jr.get("targets", []))
        if not check(self.auth, subject, "job:create", tenant_id=tenant, repo_ids=repo_ids):
            raise ProblemError(403, "missing_permission", detail="requester lacks job:create on the tenant")
        # CONFIG: the repo's project and its effective configuration, read fresh (live: who may trigger; pinned:
        # gates/risk/workflows compiled into a derived bundle below)
        proj = await self._project_config(tenant, repo_ids)
        if proj is not None:
            need = {"approver": "gate:decide", "project_admin": "tenant:policy:edit"}.get(
                proj[2]["triggers"].get("min_trigger_role", "engineer"))
            if need and not check(self.auth, subject, need, tenant_id=tenant, repo_ids=repo_ids):
                raise ProblemError(403, "missing_permission", detail="the project requires a higher role to start jobs")
        may_pin = check(self.auth, subject, "release:pin")
        if jr["priority"] == "urgent" and jr["workflow_type"] != "hotfix":
            raise ProblemError(422, "urgent_requires_hotfix")
        evaluation_mode = ((jr.get("options") or {}).get("evaluation") or {}).get("mode")
        # tenant must exist (quota settings are required by schema at tenant creation, R2-055)
        await self._check_tenant(tenant)
        pr_repo_id, pr_number = None, None
        if jr["workflow_type"] == "pr_repair":
            pr_t = next((t for t in jr.get("targets", []) if t.get("pull_request")), None)
            if pr_t:
                pr_repo_id, pr_number = pr_t["repo"]["repo_id"], pr_t["pull_request"]["number"]
                await self._check_pr_cap(tenant, pr_repo_id, pr_number)
        wf_type = "MaintenanceBatch" if jr["workflow_type"] == "maintenance" and jr.get("target_selector") \
            else "ReplicantJob"

        # 7. one transaction: insert-or-get + bundle resolution + audit
        job_id = ulid("job")
        try:
            async with self.db.tx(role="svc_cpa_intake", tenant=tenant) as conn:
                existing = (await conn.execute(text(
                    "SELECT job_id, release_bundle_id, start_status FROM intake.job_requests WHERE tenant_id = :t AND "
                    "dedupe_key = :k AND NOT dedupe_pruned"), {"t": tenant, "k": jr["dedupe_key"]})).first()
                if existing is not None:
                    return await self._duplicate(existing.job_id, existing.release_bundle_id, existing.start_status,
                                                 tenant)
                # Bundle resolution reads registry in the same transaction (C-DEF §4.5); registry SELECT is granted to
                # svc_cpa_registry (WS-06 DDL), so the transaction switches module role around the read.
                if self.db.settings.db_set_role:
                    await conn.execute(text("SET LOCAL ROLE svc_cpa_registry"))
                try:
                    res = await registry.resolve(conn, self.env, tenant, job_id, jr.get("requested_release_bundle_id"),
                                                 may_pin, evaluation_mode, req["principal_id"])
                except registry.PinNotAllowed:
                    raise ProblemError(403, "release_pin_required")
                if proj is not None and jr.get("requested_release_bundle_id") is None and \
                        not projects.is_default_pinned(proj[2]) and self.app_state is not None:
                    try:
                        drb = await derived.ensure_derived(self.app_state, conn, self.db, res.release_bundle_id,
                                                           tenant, proj[0], proj[2], "svc_control-plane-api")
                    except derived.CompileError as e:
                        raise ProblemError(503, "project_config_unbuildable", detail=str(e)[:300],
                                           headers={"Retry-After": "30"})
                    reason = res.assignment_reason if res.assignment_reason.endswith("+layer") \
                        else res.assignment_reason + "+layer"
                    res = dataclasses.replace(res, release_bundle_id=drb, assignment_reason=reason)
                if self.db.settings.db_set_role:
                    await conn.execute(text("SET LOCAL ROLE svc_cpa_intake"))
                cohort = evaluation_mode if evaluation_mode in ("eval", "shadow") else res.release_cohort
                row = (await conn.execute(text(
                    "INSERT INTO intake.job_requests (job_id, tenant_id, request_id, dedupe_key, channel, requester, "
                    "on_behalf_of, submitted_by, workflow_type, workflow_type_started, evaluation_mode, pr_repo_id, "
                    "pr_number, request, release_bundle_id, assignment_reason, release_cohort, assignment_version, "
                    "received_at) VALUES (:job, :t, :rq, :k, :ch, :req, :obo, :by, :wt, :wts, :ev, :prr, :prn, "
                    "CAST(:doc AS jsonb), :rb, :ar, :rc, :av, :recv) "
                    "ON CONFLICT (tenant_id, dedupe_key) WHERE NOT dedupe_pruned DO NOTHING RETURNING job_id"),
                    {"job": job_id, "t": tenant, "rq": jr["request_id"], "k": jr["dedupe_key"], "ch": channel,
                     "req": req["principal_id"], "obo": req.get("on_behalf_of"), "by": principal.principal_id,
                     "wt": jr["workflow_type"], "wts": wf_type, "ev": evaluation_mode, "prr": pr_repo_id,
                     "prn": pr_number, "doc": json.dumps(jr), "rb": res.release_bundle_id,
                     "ar": res.assignment_reason, "rc": cohort, "av": res.assignment_version,
                     "recv": jr["received_at"]})).first()
                if row is None:  # lost a race on the same dedupe key
                    ex = (await conn.execute(text(
                        "SELECT job_id, release_bundle_id, start_status FROM intake.job_requests WHERE tenant_id = :t "
                        "AND dedupe_key = :k AND NOT dedupe_pruned"), {"t": tenant, "k": jr["dedupe_key"]})).first()
                    return await self._duplicate(ex.job_id, ex.release_bundle_id, ex.start_status, tenant)
                await audit.emit(conn, self.db, {"event_type": "job.requested", "outcome": "success", "tenant_id": tenant,
                                                 "actor_principal_id": req["principal_id"],
                                                 "on_behalf_of": req.get("on_behalf_of"), "channel": channel,
                                                 "job_id": job_id, "release_bundle_id": res.release_bundle_id,
                                                 "details": {"workflow_type": jr["workflow_type"],
                                                             "assignment_reason": res.assignment_reason}})
        except registry.AssignmentError as e:
            log.warning("bundle_unresolvable tenant=%s reason=%s", tenant, e)
            raise ProblemError(503, "bundle_unresolvable", detail=str(e), headers={"Retry-After": "30"})

        # 8. after commit: start the workflow (never inside the intake transaction, XD-FIX-R2-048a)
        start_status = await self.start(job_id, tenant, res.release_bundle_id, res.worker_build_id, jr, wf_type)
        return IntakeResult(201, {"job_id": job_id, "status": "queued", "created": True, "start_status": start_status,
                                  "release_bundle_id": res.release_bundle_id, "links": {"ui": self.ui_link(job_id)}})

    async def _duplicate(self, job_id: str, rb: str, start_status: str, tenant: str) -> IntakeResult:
        status = "queued"
        try:
            async with self.db.tx(tenant=tenant) as conn:
                r = (await conn.execute(text("SELECT status FROM jobs_read.jobs WHERE job_id = :j"), {"j": job_id})).first()
                if r is not None:
                    status = r.status
        except Exception:  # noqa: BLE001 - projection not available: report queued
            pass
        return IntakeResult(200, {"job_id": job_id, "status": status, "created": False, "start_status": start_status,
                                  "release_bundle_id": rb, "links": {"ui": self.ui_link(job_id)}})

    async def start(self, job_id: str, tenant: str, rb: str, build: str, jr: dict[str, Any], wf_type: str) -> str:
        try:
            await self.temporal.start_job(workflow_type=wf_type, job_id=job_id, tenant_id=tenant, release_bundle_id=rb,
                                          build_id=build, job_request=jr, priority=jr.get("priority", "normal"),
                                          repos=[repo_ref(t) for t in jr.get("targets", [])])
        except Exception as e:  # noqa: BLE001 - leave pending; the retrier continues
            log.warning("workflow start failed job=%s: %s", job_id, f"{type(e).__name__}: {str(e)[:200]}")
            async with self.db.tx(role="svc_cpa_intake", tenant=tenant) as conn:
                await conn.execute(text("UPDATE intake.job_requests SET start_attempts = start_attempts + 1, "
                                        "start_last_error = :e WHERE job_id = :j"),
                                   {"e": f"{type(e).__name__}: {str(e)[:500]}", "j": job_id})
            return "pending"
        async with self.db.tx(role="svc_cpa_intake", tenant=tenant) as conn:
            await conn.execute(text("UPDATE intake.job_requests SET start_status = 'started', started_at = now(), "
                                    "start_attempts = start_attempts + 1, start_last_error = NULL WHERE job_id = :j"),
                               {"j": job_id})
        return "started"

    async def retry_pending(self, limit: int = 50) -> int:
        """Intake retrier body: restart committed requests without a workflow (start_status='pending')."""
        async with self.db.tx(scope=OPERATOR_SCOPE) as conn:
            rows = (await conn.execute(text(
                "SELECT job_id, tenant_id, release_bundle_id, request, workflow_type_started FROM intake.job_requests "
                "WHERE start_status = 'pending' ORDER BY created_at LIMIT :n"), {"n": limit})).all()
        n = 0
        builds: dict[str, str] = {}
        for r in rows:
            if r.release_bundle_id not in builds:
                async with self.db.tx(role="svc_cpa_registry") as conn:
                    builds[r.release_bundle_id] = (await conn.execute(text(
                        "SELECT worker_build_id FROM registry.release_bundles WHERE release_bundle_id = :rb"),
                        {"rb": r.release_bundle_id})).scalar_one()
            if await self.start(r.job_id, r.tenant_id, r.release_bundle_id, builds[r.release_bundle_id], r.request,
                                r.workflow_type_started) == "started":
                n += 1
        return n

    async def retrier_loop(self, interval_s: float = 5.0) -> None:
        while True:
            try:
                async with self.db.engine.connect() as lock_conn:
                    got = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:a, :b)"),
                                                   {"a": INTAKE_RETRIER_LOCK, "b": INTAKE_RETRIER_LOCK2})).scalar()
                    await lock_conn.commit()
                    if got:
                        try:
                            while True:
                                await self.retry_pending()
                                await asyncio.sleep(interval_s)
                        finally:
                            await lock_conn.execute(text("SELECT pg_advisory_unlock(:a, :b)"),
                                                    {"a": INTAKE_RETRIER_LOCK, "b": INTAKE_RETRIER_LOCK2})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("intake retrier: %s: %s", type(e).__name__, str(e)[:300])
            await asyncio.sleep(interval_s)

    async def _check_intake_switch(self, tenant: str, repos: list[str]) -> None:
        if not await self.db.has_relation("tenancy.kill_switches"):
            return
        async with self.db.tx(scope=OPERATOR_SCOPE) as conn:
            rows = (await conn.execute(text(
                "SELECT scope, scope_key, intake_state FROM tenancy.kill_switches WHERE mode = 'intake' AND engaged AND "
                "((scope = 'global') OR (scope = 'tenant' AND scope_key = :t) OR (scope = 'repo' AND scope_key = ANY(:r)))"),
                {"t": tenant, "r": repos})).all()
        states = {r.intake_state for r in rows}
        if "closed" in states:
            raise ProblemError(403, "intake_closed")
        if "paused" in states:
            raise ProblemError(409, "intake_paused")

    async def _project_config(self, tenant: str, repo_ids: tuple[str, ...]) -> tuple[str, str, dict[str, Any]] | None:
        if not repo_ids or not await self.db.has_relation("tenancy.project_repos"):
            return None
        async with self.db.tx(tenant=tenant) as conn:
            v = await projects.effective_for_repo_conn(conn, repo_ids[0])
        return v if v is not None and v[1] == tenant else None

    async def _check_tenant(self, tenant: str) -> None:
        async with self.db.tx(tenant=tenant) as conn:
            r = (await conn.execute(text("SELECT status FROM tenancy.tenants WHERE tenant_id = :t"), {"t": tenant})).first()
        if r is None:
            raise ProblemError(422, "unknown_tenant")
        if r.status not in ("active", "onboarding"):
            raise ProblemError(403, "tenant_not_active", detail=r.status)

    async def _check_pr_cap(self, tenant: str, repo_id: str, number: int) -> None:
        async with self.db.tx(tenant=tenant) as conn:
            n = (await conn.execute(text("SELECT count(*) FROM intake.job_requests WHERE pr_repo_id = :r AND "
                                         "pr_number = :n"), {"r": repo_id, "n": number})).scalar_one()
            cap = (await conn.execute(text("SELECT value FROM tenancy.platform_caps WHERE cap_name = "
                                           "'pr_repair_jobs_per_pr_max'"))).scalar()
        if n >= int(cap or 3):
            raise ProblemError(409, "pr_repair_cap_reached", cap=int(cap or 3))


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
