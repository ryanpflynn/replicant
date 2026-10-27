"""S3 access for activities (C-ART 2.0.0): physical bucket names `<org>-replicant-<env>-<class>`, write-once keys with
`If-None-Match: *` (a 412 or an existing object with equal bytes is success), URI checks against the environment's
buckets (anything else is rejected, C-ART section 3)."""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any

from temporalio.exceptions import ApplicationError

from replicant_worker.activities.config import settings

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
                                   config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3},
                                                 connect_timeout=5, read_timeout=60))
        return _client


def set_client(c: Any) -> None:  # tests
    global _client
    _client = c


@dataclass(frozen=True)
class Ref:
    bucket: str
    key: str

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


def parse(uri: str) -> Ref:
    if not uri or not uri.startswith("s3://"):
        raise ApplicationError(f"not an s3 URI: {str(uri)[:100]}", type="InvalidInput", non_retryable=True)
    bucket, _, key = uri[5:].partition("/")
    if bucket not in settings().buckets:
        raise ApplicationError(f"bucket {bucket} is not one of this environment's buckets", type="InvalidInput",
                               non_retryable=True)
    return Ref(bucket, key)


def ref(bucket_class: str, key: str) -> Ref:
    return Ref(settings().bucket(bucket_class), key)


def _code(e: Exception) -> str:
    return str(getattr(e, "response", {}).get("Error", {}).get("Code", "")) if hasattr(e, "response") else ""


def get_bytes(r: Ref, *, max_bytes: int = 256 << 20) -> bytes:
    try:
        o = client().get_object(Bucket=r.bucket, Key=r.key)
    except Exception as e:  # noqa: BLE001
        if _code(e) in ("NoSuchKey", "NoSuchBucket", "404", "NotFound"):
            raise ApplicationError(f"object not found: {r.key[:200]}", type="ObjectNotFound", non_retryable=True) from None
        raise
    body = o["Body"].read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ApplicationError("object too large", type="InvalidInput", non_retryable=True)
    return body


def get_json(r: Ref) -> Any:
    return json.loads(get_bytes(r, max_bytes=16 << 20))


def exists(r: Ref) -> bool:
    try:
        client().head_object(Bucket=r.bucket, Key=r.key)
        return True
    except Exception as e:  # noqa: BLE001
        if _code(e) in ("NoSuchKey", "NoSuchBucket", "404", "NotFound"):
            return False
        raise


def put_once(r: Ref, body: bytes, content_type: str, *, tags: dict[str, str] | None = None) -> dict[str, Any]:
    """Write-once put. Returns {key, sha256, size_bytes, created}. An existing object with different bytes keeps the
    first write (C-ART rule 3: it is a bug, so it is reported as `ObjectConflict`) unless `body` digests equal."""
    sha = hashlib.sha256(body).hexdigest()
    kw: dict[str, Any] = {"Bucket": r.bucket, "Key": r.key, "Body": body, "ContentType": content_type,
                          "ChecksumSHA256": None}
    kw.pop("ChecksumSHA256")
    if tags:
        from urllib.parse import urlencode
        kw["Tagging"] = urlencode(tags)
    created = True
    try:
        client().put_object(IfNoneMatch="*", **kw)
    except Exception as e:  # noqa: BLE001
        code = _code(e)
        if code in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
            created = False
        elif code in ("NotImplemented", "501", "InvalidArgument") or "IfNoneMatch" in str(e):
            if exists(r):
                created = False
            else:
                client().put_object(**kw)
        else:
            raise
    if not created:
        prev = get_bytes(r)
        if hashlib.sha256(prev).hexdigest() != sha:
            raise ApplicationError(f"write-once object {r.key[:200]} exists with different content",
                                   type="ObjectConflict", non_retryable=True)
    return {"key": r.key, "bucket": r.bucket, "uri": r.uri, "sha256": sha, "size_bytes": len(body), "created": created}


def put_json_once(r: Ref, doc: Any, **kw: Any) -> dict[str, Any]:
    return put_once(r, json.dumps(doc, sort_keys=True, separators=(",", ":")).encode(), "application/json", **kw)


def copy(src: Ref, dst: Ref) -> None:
    client().copy_object(Bucket=dst.bucket, Key=dst.key, CopySource={"Bucket": src.bucket, "Key": src.key})


def sha256_of(r: Ref) -> tuple[str, int]:
    b = get_bytes(r)
    return hashlib.sha256(b).hexdigest(), len(b)
