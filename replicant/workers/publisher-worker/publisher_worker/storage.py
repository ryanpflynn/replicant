"""S3 reads (diff bundles, task packet, kill-switch mirror) with the C-ART bucket check."""
from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import threading
from typing import Any

from temporalio.exceptions import ApplicationError

from publisher_worker.config import settings

_lock = threading.Lock()
_client: Any = None


def client() -> Any:
    global _client
    with _lock:
        if _client is None:
            import boto3
            from botocore.config import Config
            s = settings()
            _client = boto3.client("s3", endpoint_url=s.s3_endpoint, region_name=s.s3_region,
                                   config=Config(s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=60))
        return _client


def set_client(c: Any) -> None:
    global _client
    _client = c


def parse(uri: str) -> tuple[str, str]:
    if not uri or not uri.startswith("s3://"):
        raise ApplicationError("not an s3 URI", type="InvalidInput", non_retryable=True)
    b, _, k = uri[5:].partition("/")
    if b not in settings().buckets:
        raise ApplicationError(f"bucket {b} is not one of this environment's buckets", type="InvalidInput", non_retryable=True)
    return b, k


def get(bucket: str, key: str, max_bytes: int = 256 << 20) -> bytes:
    try:
        o = client().get_object(Bucket=bucket, Key=key)
    except Exception as e:  # noqa: BLE001
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("NoSuchKey", "NoSuchBucket", "404", "NotFound"):
            raise ApplicationError("object not found", type="ObjectNotFound", non_retryable=True) from None
        raise
    b = o["Body"].read(max_bytes + 1)
    if len(b) > max_bytes:
        raise ApplicationError("object too large", type="InvalidInput", non_retryable=True)
    return b


def get_uri(uri: str) -> bytes:
    return get(*parse(uri))


def open_diff_bundle(raw: bytes) -> tuple[bytes, dict[str, Any]]:
    data = raw
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        import zstandard
        data = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read()
    elif raw[:2] == b"\x1f\x8b":
        import gzip
        data = gzip.decompress(raw)
    patch, meta = b"", {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            name = m.name.lstrip("./")
            body = tf.extractfile(m).read()  # type: ignore[union-attr]
            if name == "changes.patch":
                patch = body
            elif name == "metadata.json":
                meta = json.loads(body or b"{}")
    return patch, meta


def patch_paths(patch: bytes) -> list[str]:
    out: list[str] = []
    for line in patch.decode("utf-8", "replace").splitlines():
        m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
        if m and m.group(2) not in out:
            out.append(m.group(2))
    return out


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def task_packet(tenant: str, job: str) -> dict[str, Any]:
    """The job's task packet (replicant-worker context.build_briefing, steps/context.c0.n1/task_packet/task.json)."""
    try:
        return json.loads(get(settings().bucket("artifacts"), f"tenants/{tenant}/jobs/{job}/steps/context.c0.n1/task_packet/task.json",
                              4 << 20)).get("task") or {}
    except Exception:  # noqa: BLE001 - best effort: PR title/body fall back to generic text
        return {}


def kill_switch_engaged(tenant: str, repo: str, bundle: str | None, switch: str = "publishing") -> bool:
    uri = settings().kill_switch_mirror
    if not uri:
        return False
    try:
        doc = json.loads(get_uri(uri))
    except ApplicationError as e:
        if e.type == "ObjectNotFound":
            return False
        raise
    want = {("global", "*"), ("tenant", tenant), ("repo", repo), ("bundle", bundle)}
    return any(sw.get("engaged") and sw.get("mode") == switch and (sw.get("scope"), sw.get("scope_value") or "*") in want
               for sw in doc.get("switches") or [])
