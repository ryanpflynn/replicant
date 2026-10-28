"""Job graph and agent-activity read endpoints for control-plane-web (GRAPH-*, docs/v0/DEVIATIONS.md; INTERFACES-LOG
GRAPH-01). Both are job-scoped reads with the job's authorization (`job:read` on the job's tenant/repos, 404 otherwise)
and tenant-scoped (RLS) reads of the projection:

* `GET /v1/jobs/{job_id}/graph` — the job's C-DEF graph (pinned through its Release Bundle: registry row or S3 pointer
  → digest-verified definition blob, replicant_cdef) with per-node status from jobs_read.job_timeline, the snapshot and
  jobs_read.job_runs (graph_view.py).
* `GET /v1/jobs/{job_id}/runs/{run_id}/activity` — what the run's agent did: the run's transcript chunks from the
  transcripts bucket (activity.py), the run result, and the gateway shim's usage log rows for the run.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from control_plane_api import activity, graph_view
from control_plane_api.auth import require
from control_plane_api.authz import Principal
from control_plane_api.problems import ProblemError
from control_plane_api.routes_jobs import load_job_row
from replicant_telemetry.common import iso

log = logging.getLogger(__name__)
router = APIRouter()

RUN_ID = re.compile(r"^run_[0-9a-hjkmnp-tv-z]{26}$")
SPOOL_TYPES = ("cli_events", "transcript", "sandbox_log")
MAX_RUN_BYTES = 48 << 20        # decoded transcript bytes considered per run (tail kept)
USAGE_MAX_HOURS = 8
USAGE_MAX_OBJECTS = 4000


class _LRU(OrderedDict):
    def __init__(self, cap: int) -> None:
        super().__init__()
        self.cap = cap

    def get_(self, k: Any) -> Any:
        if k in self:
            self.move_to_end(k)
            return self[k]
        return None

    def put(self, k: Any, v: Any) -> None:
        self[k] = v
        self.move_to_end(k)
        while len(self) > self.cap:
            self.popitem(last=False)


_graph_cache = _LRU(64)        # (rb, workflow_type) -> (spec, pin)   content-addressed, immutable
_chunk_cache = _LRU(2048)      # (bucket, key, etag) -> decoded bytes
_usage_cache = _LRU(8192)      # usage object key -> parsed events (write-once objects)
_result_cache = _LRU(512)      # result key -> dict (write-once)


def _zstd(b: bytes) -> bytes:
    import zstandard
    return zstandard.ZstdDecompressor().decompress(b, max_output_size=64 << 20)


async def _thread(fn: Any, *a: Any) -> Any:
    return await asyncio.to_thread(fn, *a)


# ------------------------------------------------------------------------------------------------ graph
async def _bundle_spec(request: Request, rb: str) -> dict[str, Any] | None:
    async with request.app.state.db.tx(role="svc_cpa_registry") as conn:
        r = (await conn.execute(text("SELECT spec FROM registry.release_bundles WHERE release_bundle_id = :rb"),
                                {"rb": rb})).first()
    if r is not None and r.spec:
        return r.spec
    import replicant_cdef
    st = request.app.state.settings
    s3 = request.app.state.s3.client
    bucket = st.bucket("registry")

    def _read() -> dict[str, Any] | None:
        from botocore.exceptions import ClientError
        try:
            ptr = s3.get_object(Bucket=bucket, Key=f"bundles/{rb}.json")["Body"].read()
        except ClientError:
            return None
        return replicant_cdef.read_bundle_spec(ptr, lambda k: s3.get_object(Bucket=bucket, Key=k)["Body"].read())[1]
    return await _thread(_read)


async def load_graph(request: Request, rb: str, workflow_type: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    hit = _graph_cache.get_((rb, workflow_type))
    if hit:
        return hit
    spec = await _bundle_spec(request, rb)
    pin = ((spec or {}).get("graphs") or {}).get(workflow_type)
    if not pin or not pin.get("digest"):
        return None
    import replicant_cdef
    st = request.app.state.settings
    s3 = request.app.state.s3.client

    def _blob() -> dict[str, Any]:
        body = s3.get_object(Bucket=st.bucket("registry"), Key=replicant_cdef.blob_key(pin["digest"]))["Body"].read()
        return json.loads(replicant_cdef.verify_blob(body, pin["digest"]))
    try:
        doc = await _thread(_blob)
    except Exception as e:  # noqa: BLE001 - absent blob (local fallback set) or digest mismatch
        log.warning("graph definition %s unreadable: %s", pin.get("ref"), type(e).__name__)
        return None
    out = (doc.get("spec") or {}, {"ref": pin.get("ref"), "digest": pin["digest"],
                                   "name": (doc.get("metadata") or {}).get("name")})
    _graph_cache.put((rb, workflow_type), out)
    return out


async def _job_runs(request: Request, r: Any) -> list[dict[str, Any]]:
    from control_plane_api.routes_jobs import _run_row
    db = request.app.state.db
    if await db.has_relation("jobs_read.job_runs"):
        async with db.tx(tenant=r.tenant_id) as conn:
            rows = (await conn.execute(text("SELECT * FROM jobs_read.job_runs WHERE job_id = :j "
                                            "ORDER BY started_at NULLS LAST, seq"), {"j": r.job_id})).all()
        if rows:
            return [_run_row(x) for x in rows]
    return [{k: v for k, v in x.items() if k not in ("result_uri", "artifact_manifest_uri")}
            for x in (r.snapshot or {}).get("runs", [])]


@router.get("/v1/jobs/{job_id}/graph")
async def get_job_graph(job_id: str, request: Request, response: Response,
                        principal: Principal = require("job:read")) -> dict[str, Any]:
    r = await load_job_row(request, principal, job_id)
    snap = r.snapshot or {}
    wt = snap.get("workflow_type") or r.workflow_type
    async with request.app.state.db.tx(tenant=r.tenant_id) as conn:
        tl = (await conn.execute(text("SELECT seq, type, at, summary, refs FROM jobs_read.job_timeline WHERE job_id = :j "
                                      "AND (type LIKE 'node.%' OR type = 'job.terminal') ORDER BY seq LIMIT 20000"),
                                 {"j": job_id})).all()
    timeline = [{"seq": t.seq, "type": t.type, "at": t.at, "summary": t.summary, "refs": t.refs} for t in tl]
    runs = await _job_runs(request, r)
    out: dict[str, Any] = {"job_id": job_id, "workflow_type": wt, "release_bundle_id": r.release_bundle_id,
                           "as_of": iso(dt.datetime.now(dt.UTC))}
    g = await load_graph(request, r.release_bundle_id, wt) if r.release_bundle_id and wt and wt != "unrouted" else None
    if g is None:
        out["graph"] = None
        out["unavailable_reason"] = ("not_routed" if wt in (None, "unrouted") else "definition_not_resolvable")
        return out
    spec, pin = g
    out["definition"] = pin
    out["graph"] = graph_view.build_view(spec, timeline, snap, runs)
    response.headers["Cache-Control"] = "no-store"
    return out


# ------------------------------------------------------------------------------------------------ run activity
async def _find_run(request: Request, r: Any, run_id: str) -> dict[str, Any]:
    for x in await _job_runs(request, r):
        if x.get("run_id") == run_id:
            return x
    raise ProblemError(404, "not_found", detail="no such run in this job")


def _list(s3: Any, bucket: str, prefix: str) -> list[dict[str, Any]]:
    out, token = [], None
    for _ in range(20):
        kw = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        res = s3.list_objects_v2(**kw)
        out.extend(res.get("Contents") or [])
        if not res.get("IsTruncated"):
            break
        token = res.get("NextContinuationToken")
    return out


def _get_chunk(s3: Any, bucket: str, obj: dict[str, Any]) -> bytes:
    ck = (bucket, obj["Key"], obj.get("ETag"))
    hit = _chunk_cache.get_(ck)
    if hit is not None:
        return hit
    raw = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
    body = _zstd(raw) if obj["Key"].endswith(".zst") else raw
    _chunk_cache.put(ck, body)
    return body


def read_spool(s3: Any, bucket: str, prefix: str) -> dict[str, Any]:
    """The run's spool chunks per type, oldest first. Final parts (`<type>/part-NNNNN.ndjson.zst`) are used as they
    are; a provisional `<type>/live/part-NNNNN.ndjson.zst` (the open part, if the uploader ships one) is used only when
    no final part of that number exists yet."""
    objs = _list(s3, bucket, prefix)
    by: dict[str, dict[str, dict[str, Any]]] = {t: {} for t in SPOOL_TYPES}
    live: dict[str, dict[str, dict[str, Any]]] = {t: {} for t in SPOOL_TYPES}
    for o in objs:
        rest = o["Key"][len(prefix):]
        parts = rest.split("/")
        if len(parts) == 2 and parts[0] in by and parts[1].startswith("part-"):
            by[parts[0]][parts[1].split(".")[0]] = o
        elif len(parts) == 3 and parts[0] in live and parts[1] == "live" and parts[2].startswith("part-"):
            live[parts[0]][parts[2].split(".")[0]] = o
    out: dict[str, Any] = {"chunks": 0, "provisional": 0, "last_modified": None}
    for t in SPOOL_TYPES:
        chosen = dict(by[t])
        for name, o in live[t].items():
            if name not in chosen:
                chosen[name] = o
                out["provisional"] += 1
        names = sorted(chosen)
        bodies: list[bytes] = []
        total = 0
        for name in reversed(names):  # newest first until the byte cap, then restore order
            o = chosen[name]
            total += int(o.get("Size") or 0) * 8
            if total > MAX_RUN_BYTES and bodies:
                out.setdefault("skipped_chunks", 0)
                out["skipped_chunks"] += 1
                continue
            try:
                bodies.append(_get_chunk(s3, bucket, o))
            except Exception as e:  # noqa: BLE001 - a chunk that disappeared or does not decode
                log.warning("transcript chunk %s unreadable: %s", o["Key"], type(e).__name__)
            lm = o.get("LastModified")
            if lm and (out["last_modified"] is None or lm > out["last_modified"]):
                out["last_modified"] = lm
        out[t] = list(reversed(bodies))
        out["chunks"] += len(names)
    return out


def _usage_location(settings: Any) -> tuple[str, str]:
    uri = os.environ.get("REPLICANT_GATEWAY_USAGE_S3_URI") or \
        f"s3://{settings.bucket('artifacts')}/platform/gateway-usage/"
    bucket, _, prefix = uri[5:].partition("/")
    return bucket, prefix


def read_usage(s3: Any, bucket: str, prefix: str, start: dt.datetime, end: dt.datetime) -> tuple[list[dict], bool]:
    """Gateway usage events in the hour partitions [start, end + 1h] (the shim flushes in batches)."""
    events: list[dict[str, Any]] = []
    h = start.replace(minute=0, second=0, microsecond=0)
    stop = min(end + dt.timedelta(hours=1), start + dt.timedelta(hours=USAGE_MAX_HOURS))
    seen = 0
    capped = False
    while h <= stop:
        for o in _list(s3, bucket, f"{prefix}{h:%Y/%m/%d/%H}/"):
            seen += 1
            if seen > USAGE_MAX_OBJECTS:
                capped = True
                break
            hit = _usage_cache.get_(o["Key"])
            if hit is None:
                try:
                    hit = activity.parse_ndjson(_get_chunk(s3, bucket, o))
                except Exception:  # noqa: BLE001
                    hit = []
                _usage_cache.put(o["Key"], [{k: e.get(k) for k in ("run_id", "occurred_at", "served_model",
                                                                    "gateway_model_id", "status", "http_status",
                                                                    "usage", "cost", "latency_ms", "retry_attempt",
                                                                    "purpose", "trace_id", "budget_after")}
                                            for e in hit])
                hit = _usage_cache.get_(o["Key"])
            events.extend(hit)
        h += dt.timedelta(hours=1)
    return events, capped


def _result_summary(doc: dict[str, Any]) -> dict[str, Any]:
    rr = doc.get("role_result") or {}
    out: dict[str, Any] = {k: doc.get(k) for k in ("outcome", "summary") if doc.get(k) is not None}
    if out.get("summary"):
        out["summary"] = activity.redact(str(out["summary"]))[:4000]
    for k in ("cli", "budget", "changes", "bootstrap"):
        if isinstance(rr.get(k), dict):
            out[k] = rr[k]
    if isinstance(doc.get("usage"), dict):
        out["usage"] = doc["usage"]
    for k in ("verdict", "qa_verdict", "stages"):
        if rr.get(k) is not None:
            out[k] = rr[k] if k != "stages" else [{kk: s.get(kk) for kk in ("name", "status", "exit_code", "seconds")}
                                                  for s in rr[k] if isinstance(s, dict)][:50]
    if out.get("changes", {}).get("paths"):
        out["changes"] = {**out["changes"], "paths": out["changes"]["paths"][:100]}
    return out


@router.get("/v1/jobs/{job_id}/runs/{run_id}/activity")
async def get_run_activity(job_id: str, run_id: str, request: Request, response: Response,
                           principal: Principal = require("job:read")) -> dict[str, Any]:
    if not RUN_ID.match(run_id):
        raise ProblemError(404, "not_found")
    r = await load_job_row(request, principal, job_id)
    run = await _find_run(request, r, run_id)
    st = request.app.state.settings
    s3 = request.app.state.s3.client
    prefix = f"tenants/{r.tenant_id}/jobs/{r.job_id}/runs/{run_id}/"   # ids validated; never from the client
    live = run.get("phase") in graph_view.RUN_LIVE
    out: dict[str, Any] = {"job_id": job_id, "run": run, "live": live,
                           "as_of": iso(dt.datetime.now(dt.UTC))}
    try:
        spool = await _thread(read_spool, s3, st.bucket("transcripts"), prefix)
    except Exception as e:  # noqa: BLE001 - S3 down: the rest of the page keeps working
        log.warning("transcripts unreadable for %s: %s", run_id, type(e).__name__)
        spool = {"chunks": 0, "provisional": 0, "error": "transcripts_unavailable"}
    cli = [e for b in spool.get("cli_events", []) for e in activity.parse_ndjson(b)]
    tr = [e for b in spool.get("transcript", []) for e in activity.parse_ndjson(b)]
    feed = activity.build_feed(cli, tr, spool.get("sandbox_log", []))
    out.update(feed)
    out["source"] = {"chunks": spool.get("chunks", 0), "provisional_chunks": spool.get("provisional", 0),
                     "last_upload_at": iso(spool.get("last_modified")),
                     **({"skipped_chunks": spool["skipped_chunks"]} if spool.get("skipped_chunks") else {}),
                     **({"error": spool["error"]} if spool.get("error") else {})}
    if not live:
        key = prefix + "run_result/result.json"
        doc = _result_cache.get_(key)
        if doc is None:
            def _res() -> dict[str, Any] | None:
                from botocore.exceptions import ClientError
                try:
                    return json.loads(s3.get_object(Bucket=st.bucket("artifacts"), Key=key)["Body"].read())
                except (ClientError, ValueError):
                    return None
            try:
                doc = await _thread(_res)
            except Exception:  # noqa: BLE001
                doc = None
            if doc is not None:
                _result_cache.put(key, doc)
        if doc:
            out["result"] = _result_summary(doc)
    started = graph_view._ts(run.get("started_at"))
    if started:
        end = graph_view._ts(run.get("finished_at")) or dt.datetime.now(dt.UTC)
        ub, up = _usage_location(st)
        try:
            events, capped = await _thread(read_usage, s3, ub, up, started - dt.timedelta(minutes=5), end)
            out["usage"] = activity.usage_rows(events, run_id)
            if capped:
                out["usage"]["capped"] = True
        except Exception as e:  # noqa: BLE001
            log.warning("gateway usage unreadable for %s: %s", run_id, type(e).__name__)
            out["usage"] = {"requests": [], "totals": {}, "models": [], "error": "usage_unavailable"}
    from replicant_telemetry.trace_ids import job_trace_id
    out["observe"] = {"trace_id": job_trace_id(job_id), "loki_datasource": "loki-sandbox",
                      "loki_expr": f'{{service=~"replicant-sandbox.*"}} | run_id="{run_id}"',
                      "from": run.get("started_at"), "to": run.get("finished_at")}
    response.headers["Cache-Control"] = "no-store"
    return out
