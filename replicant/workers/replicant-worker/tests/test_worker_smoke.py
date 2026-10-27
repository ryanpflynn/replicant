"""Component smoke test: the real replicant-worker (cli.build_workers, ReplicantJob on InterpreterEngine, the v0 activities)
runs the shipped bugfix graph end to end on a local Temporal dev server, PINNED to a Worker Deployment version that
is set current, against contract fakes of its neighbours:

* runner-local: C-RUNNER 3.0.0 (RunSpec schema, ES256 run grant over spec_digest, Idempotency-Key, the inputs
  manifest digest, run_result in S3, `run.status_changed` hint Signals with plain JSON payloads);
* gateway-shim: RunPolicy (gateway-interface.openapi.yaml);
* control-plane-api: postNotification;
* publisher-worker / replicant-projector: the real replicant_projector schema validation and a digest-checking
  publisher fake (G-03);
* S3: in-memory, write-once.

The approval goes through the `gate.decide` Update like control-plane-api sends it; the merge through `pr.event`.
Run: uv run pytest workers/replicant-worker/tests/test_worker_smoke.py
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import tarfile
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import zstandard
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from temporalio import activity
from temporalio.client import Client
from temporalio.common import Priority, VersioningOverride, PinnedVersioningOverride, WorkerDeploymentVersion  # noqa: F401
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import fakes
from replicant_temporal.search_attributes import dev_server_args, job_start_attributes
from replicant_temporal.versioning import set_current_version
from replicant_worker import ids, resources
from replicant_worker.activities import base, bundle, config, grants, s3

BUILD = "b20261024-0000000000ab"
T = "payments"
ART = "local-replicant-local-artifacts"
BOB = ids.derived("usr", "replicant-dev-user", "bob")
ALICE = ids.derived("usr", "replicant-dev-user", "alice")
REPO = {"provider": "gitea", "host": "git.replicant.localhost:3300", "owner": "acme-payments", "name": "ledger-api", "repo_id": "1"}
BASE_SHA = "8c1e4f2a9b7d3c6e5f0a1b2c3d4e5f6a7b8c9d0e"


def job_request(job: str) -> dict[str, Any]:
    jr = json.loads((resources.CONTRACTS / "job-request.schema.json").read_text())["examples"][0]
    jr.update(workflow_type="auto", source={"channel": "github", "event_type": "issues.labeled", "event_id": "d-1",
                                            "url": "http://git.replicant.localhost:3300/acme-payments/ledger-api/issues/1",
                                            "github": {"installation_id": 1, "app_registration": "dev"}},
              requester={"kind": "user", "principal_id": ALICE, "channel_identity": {"kind": "gitea", "id": "2", "login": "alice"}},
              targets=[{"repo": REPO, "base_ref": "main", "issue": {"number": 1}}])
    jr.pop("attachments", None)
    return jr


def bundle_bytes() -> bytes:
    patch = (b"diff --git a/internal/ledger/ledger.go b/internal/ledger/ledger.go\n--- a/internal/ledger/ledger.go\n"
             b"+++ b/internal/ledger/ledger.go\n@@ -64 +64 @@\n-\t\tfxReversal := c.FXFeeMinor\n"
             b"+\t\tfxReversal := Prorate(c.FXFeeMinor, refundMinor, c.AmountMinor)\n")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for n, b in (("changes.patch", patch), ("metadata.json", json.dumps({"base_sha": BASE_SHA,
                                                                            "paths": ["internal/ledger/ledger.go"]}).encode())):
            ti = tarfile.TarInfo(n)
            ti.size = len(b)
            tf.addfile(ti, io.BytesIO(b))
    return zstandard.ZstdCompressor().compress(buf.getvalue())


class FakeRunner:
    """runner-local per RUNNER-02, enough for the bugfix path."""

    def __init__(self, fs3: fakes.FakeS3, client: Client, pub_key: ec.EllipticCurvePublicKey) -> None:
        self.s3, self.client, self.key = fs3, client, pub_key
        self.runs: dict[str, dict[str, Any]] = {}
        self.policies: dict[str, dict[str, Any]] = {}
        self.job_budgets: list[dict[str, Any]] = []
        self.specs = fakes.openapi_validator("00-contracts/runner-api.openapi.yaml", "RunSpec")
        self.errors: list[str] = []

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if req.method == "POST" and p == "/v1/runs":
            return await self.create(req)
        if req.method == "GET" and p.startswith("/v1/runs/"):
            r = self.runs.get(p.rsplit("/", 1)[1])
            return httpx.Response(200, json=self.status(r)) if r else httpx.Response(404, json={"code": "run_not_found", "retryable": False})
        if req.method == "PUT" and p.startswith("/internal/v1/runs/"):
            self.policies[p.split("/")[4]] = json.loads(req.content)  # gateway run policy
            return httpx.Response(200, json=json.loads(req.content))
        if req.method == "GET" and p.startswith("/internal/v1/runs/"):
            pol = self.policies.get(p.split("/")[4])
            return httpx.Response(200, json=pol) if pol else httpx.Response(404, json={"code": "not_found"})
        if req.method == "PUT" and p.startswith("/internal/v1/jobs/"):
            self.job_budgets.append(json.loads(req.content))
            return httpx.Response(200, json={})
        if req.method == "POST" and p == "/internal/v1/notifications":
            return httpx.Response(202)
        return httpx.Response(404)

    async def create(self, req: httpx.Request) -> httpx.Response:
        key = json.loads(req.headers["idempotency-key"])
        spec = json.loads(req.content)
        errs = fakes.errors(self.specs, spec)
        h, claims, sig, si = fakes.jwt_parts(spec["run_grant"])
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        try:
            self.key.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")), si,
                            ec.ECDSA(hashes.SHA256()))
        except Exception:  # noqa: BLE001
            errs.append("bad grant signature")
        if claims["spec_digest"] != grants.spec_digest(spec):
            errs.append("grant spec_digest mismatch")
        mkey = spec["inputs"]["manifest_uri"].split("/", 3)[3]
        if hashlib.sha256(self.s3.objs.get((ART, mkey), b"")).hexdigest() != spec["inputs"]["manifest_sha256"]:
            errs.append("inputs manifest digest mismatch (A-4)")
        if errs:
            self.errors += errs
            return httpx.Response(400, json={"code": "invalid_request", "retryable": False, "detail": errs[0]})
        run_id = ids.run_id_for(spec["tenant_id"], key)
        if run_id in self.runs:
            return httpx.Response(200, json=self.status(self.runs[run_id]))
        r = {"run_id": run_id, "key": key, "spec": spec, "phase": "Pending"}
        self.runs[run_id] = r
        asyncio.get_running_loop().create_task(self.progress(r))
        return httpx.Response(201, json=self.status(r))

    async def progress(self, r: dict[str, Any]) -> None:
        for phase in ("Running", "Succeeded"):
            await asyncio.sleep(0.4)
            if phase == "Succeeded":
                self.finish(r)
            r["phase"] = phase
            try:  # run.status_changed hint, plain JSON payload (WORKER-01: no codec in M1)
                await self.client.get_workflow_handle(r["spec"]["callback"]["workflow_id"]).signal("run.status_changed", {
                    "schema": "replicant.msg.run-status-changed/v1", "notification_id": f"{r['run_id']}:{phase}:1",
                    "run_id": r["run_id"], "job_id": r["spec"]["job_id"], "tenant_id": T, "idempotency_key": r["key"],
                    "phase": phase, "terminal": phase == "Succeeded", "observed_generation": 2,
                    "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
            except Exception:  # noqa: BLE001 - a hint is best effort
                pass

    def finish(self, r: dict[str, Any]) -> None:
        sp, role = r["spec"], r["spec"]["role"]
        pre = f"tenants/{T}/jobs/{sp['job_id']}/runs/{r['run_id']}/"
        outputs: dict[str, str] = {}
        rr: dict[str, Any] | None = None
        if role == "planner":
            self.s3.objs[(ART, pre + "plan/plan.md")] = b"# Plan\nProrate the FX fee reversal.\n"
            outputs["plan"] = pre + "plan/plan.md"
        elif role == "implementer":
            self.s3.objs[(ART, pre + "diff_bundle/changes.tar.zst")] = bundle_bytes()
            outputs["diff_bundle"] = pre + "diff_bundle/changes.tar.zst"
        elif role == "qa":
            rr = {"schema": "replicant.role-result.qa/v1", "verdict": "pass", "reason_code": "all_passed", "counts": {"failed": 0}}
            self.s3.objs[(ART, pre + "qa_report/report.json")] = b"{}"
            outputs["qa_report"] = pre + "qa_report/report.json"
        elif role == "reviewer":
            self.s3.objs[(ART, pre + "review_report/review.md")] = b"approve"
            outputs["review_report"] = pre + "review_report/review.md"
        res = {"schema": "replicant.run-result/v1", "run_id": r["run_id"], "job_id": sp["job_id"], "role": role,
               "outcome": "completed", "summary": "ok", "usage": {"wall_seconds": 1}, "outputs": outputs}
        if rr:
            res["role_result"] = rr
        self.s3.objs[(ART, pre + "run_result/result.json")] = json.dumps(res).encode()
        r["result_uri"] = f"s3://{ART}/{pre}run_result/result.json"

    def status(self, r: dict[str, Any]) -> dict[str, Any]:
        s = {"run_id": r["run_id"], "phase": r["phase"], "terminal": r["phase"] == "Succeeded", "idempotency_key": r["key"]}
        if r["phase"] == "Succeeded":
            s["result_uri"] = r["result_uri"]
            s["artifact_manifest_uri"] = r["result_uri"].replace("run_result/result.json", "manifest.json")
        return s


def peer_activities(fs3: fakes.FakeS3, published: list[dict[str, Any]], projections: list[dict[str, Any]]) -> dict[str, list]:
    try:
        from replicant_projector.activities import validate  # the real projector's C-MSG validation
    except ImportError:  # replicant-projector not installed in this env: the same check from the vendored contract
        import jsonschema
        d = json.loads((resources.CONTRACTS / "schemas" / "workflow-messages.schema.json").read_text())
        _v = jsonschema.Draft202012Validator({"$ref": "#/$defs/ProjectionEvent", "$defs": d["$defs"]})

        def validate(ev: Any) -> None:
            errs = list(_v.iter_errors(ev))
            if errs:
                raise ApplicationError(errs[0].message[:200], type="SchemaViolation", non_retryable=True)

    @activity.defn(name="projection.apply")
    async def projection_apply(ev: dict[str, Any]) -> None:
        validate(ev)
        projections.append(ev)

    @activity.defn(name="qa.index_results")
    async def qa_index_results(a: dict[str, Any]) -> dict[str, Any]:
        return {"indexed": False}

    @activity.defn(name="scm.resolve_base")
    async def scm_resolve_base(a: dict[str, Any]) -> dict[str, Any]:
        return {"base_ref": a.get("base_ref") or "main", "base_sha": BASE_SHA}

    @activity.defn(name="publisher.check_base")
    async def check_base(a: dict[str, Any]) -> dict[str, Any]:
        return {"moved": False, "new_base_sha": BASE_SHA, "applies_cleanly": True}

    @activity.defn(name="publisher.publish")
    async def publish(a: dict[str, Any]) -> dict[str, Any]:
        raw = fs3.objs[(ART, a["diff_bundle_uri"].split("/", 3)[3])]
        if hashlib.sha256(raw).hexdigest() != a["approved_diff_sha256"] or not a.get("approval_decision_id"):
            raise ApplicationError("digest mismatch", type="PushRejected", non_retryable=True)
        published.append(a)
        return {"repo": "git.replicant.localhost:3300/acme-payments/ledger-api", "number": 7, "pr_number": 7, "state": "open",
                "url": "http://git.replicant.localhost:3300/acme-payments/ledger-api/pulls/7", "head_sha": "c" * 40,
                "branch": f"replicant/{a['job_id']}"}

    return {"replicant-projections": [projection_apply, qa_index_results],
            "replicant-publisher": [scm_resolve_base, check_base, publish]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    kf = tmp_path / "rg.pem"
    kf.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    for k, v in {"REPLICANT_ENV": "local", "REPLICANT_RUN_GRANT_KEY_FILE": str(kf), "REPLICANT_RUNNER_URL": "http://runner.test",
                 "REPLICANT_GATEWAY_URL": "http://runner.test", "REPLICANT_CONTROL_PLANE_URL": "http://runner.test",
                 "REPLICANT_GATEWAY_SCRIPT_SET": "ledger-api-v0", "REPLICANT_LOCAL_REGISTRY_PUBLISH": "false",
                 "REPLICANT_BUNDLE_FALLBACK_ANY": "true"}.items():  # CONFIG-D5: arbitrary rb_ in tests
        monkeypatch.setenv(k, v)
    config.settings.cache_clear()
    grants.signer.cache_clear()
    bundle._cache.clear()
    fs3 = fakes.FakeS3()
    s3.set_client(fs3)
    yield fs3, key
    s3.set_client(None)
    base.set_http(None)


def test_bugfix_end_to_end_on_the_real_worker(env) -> None:
    fs3, key = env

    async def main() -> None:
        tenv = await WorkflowEnvironment.start_local(namespace="replicant-local", dev_server_extra_args=[
            *dev_server_args(), "--dynamic-config-value", "frontend.workerVersioningWorkflowAPIs=true", "--log-level", "error"])
        try:
            from replicant_temporal import TemporalSettings, connect
            client = await connect(TemporalSettings(address=tenv.client.service_client.config.target_host,
                                                    namespace=tenv.client.namespace))
            fr = FakeRunner(fs3, client, key.public_key())
            base.set_http(httpx.AsyncClient(transport=httpx.MockTransport(fr)))
            from replicant_worker import cli
            from replicant_worker.activities.catalog_impl import ACTIVITIES_BY_QUEUE
            published: list[dict[str, Any]] = []
            projections: list[dict[str, Any]] = []
            peers = peer_activities(fs3, published, projections)
            ws = cli.build_workers(client, ACTIVITIES_BY_QUEUE, BUILD)
            ws += [Worker(client, task_queue=q, activities=a) for q, a in peers.items()]
            async with contextlib.AsyncExitStack() as st:
                for w in ws:
                    await st.enter_async_context(w)
                await set_current_version(client, "replicant-worker", BUILD, identity="smoke", attempts=30, delay=1)
                job = ids.derived("job", "smoke", "1")
                rb = "rb_ds5p3wmcj4dag2e9a8ncp4py6k"
                from replicant_worker.workflows.replicant_job import ReplicantJobInput
                h = await client.start_workflow(
                    "ReplicantJob", ReplicantJobInput(mode="start", job_id=job, tenant_id=T, release_bundle_id=rb,
                                                  job_request=job_request(job)),
                    id=job, task_queue="replicant-jobs",
                    search_attributes=job_start_attributes(job_id=job, tenant_id=T, release_bundle_id=rb),
                    versioning_override=PinnedVersioningOverride(WorkerDeploymentVersion("replicant-worker", BUILD)),
                    priority=Priority(priority_key=3, fairness_key=T))

                async def gate() -> dict[str, Any] | None:
                    r = await h.query("job.open_gates")
                    return next((g for g in r["gates"] if g["kind"] == "publish_approval"), None)
                g = None
                for _ in range(240):
                    g = await gate()
                    if g or (await h.query("job.snapshot"))["status"] in ("failed", "succeeded", "cancelled"):
                        break
                    await asyncio.sleep(0.5)
                assert g, f"no publish gate; runner errors={fr.errors} snapshot={await h.query('job.snapshot')}"
                dec = ids.derived("dec", "ui", g["gate_id"])
                res = await h.execute_update("gate.decide", {
                    "schema": "replicant.msg.gate-decide/v1", "decision_id": dec, "gate_id": g["gate_id"], "decision": "approve",
                    "actor": {"principal_id": BOB}, "channel": "ui", "presented_digest": g["presented_digest"],
                    "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}, id=dec)
                assert res["outcome"] == "accepted", res
                for _ in range(120):
                    if (await h.query("job.snapshot"))["status"] == "waiting_external":
                        break
                    await asyncio.sleep(0.5)
                await h.signal("pr.event", {
                    "schema": "replicant.msg.pr-event/v1", "delivery_id": "00000000-0000-0000-0000-000000000001",
                    "event": "pull_request_merged", "job_id": job, "repo": "git.replicant.localhost:3300/acme-payments/ledger-api",
                    "pr_number": 7, "actor": {"github_user_id": 2, "login": "alice", "is_bot": False, "linked": True,
                                              "principal_id": ALICE, "repo_permission": "write"},
                    "payload_uri": f"s3://{ART}/tenants/{T}/jobs/{job}/events/pr/1.json", "received_at": "2026-10-24T11:00:00Z"})
                out = await asyncio.wait_for(h.result(), 120)
                snap = await h.query("job.snapshot")
                desc = await h.describe()
            assert out["outcome"] == "pr_merged" and out["status"] == "succeeded", out
            assert not fr.errors, fr.errors
            assert {p["role"] for p in fr.policies.values()} == {"planner", "implementer", "qa", "reviewer"}
            assert all(p["stub"]["root"] == "scripts" and p["stub"]["set"] == "ledger-api-v0" for p in fr.policies.values())
            assert fr.job_budgets and fr.job_budgets[0]["tenant_id"] == T
            roles = sorted(r["spec"]["role"] for r in fr.runs.values())
            assert roles == ["implementer", "planner", "qa", "reviewer"], roles
            assert len(published) == 1 and published[0]["approval_decision_id"] == dec
            seqs = [p["seq"] for p in projections]
            assert len(seqs) == len(set(seqs)) and projections, "duplicate or missing projections"
            assert snap["worker_deployment_version"] == {"deployment_name": "replicant-worker", "build_id": BUILD}
            assert desc.raw_description.workflow_execution_info.versioning_info.behavior == 1  # PINNED
            dl = next(x for x in snap["gates"] if x["kind"] == "publish_approval")["decision_log"]
            assert dl[0]["principal_id"] == BOB and dl[0]["outcome"] == "accepted"
            # every manifest the Runner admitted validates against WS-03's run-inputs schema
            v = fakes.ws03_validator("run-inputs.schema.json")
            for r in fr.runs.values():
                m = json.loads(fs3.objs[(ART, r["spec"]["inputs"]["manifest_uri"].split("/", 3)[3])])
                assert not fakes.errors(v, m), fakes.errors(v, m)
        finally:
            await tenv.shutdown()
    asyncio.run(main())
