"""Contract fakes for activity unit tests: in-memory S3 (boto3 subset with If-None-Match), and peers built from their
contracts (C-RUNNER RunSpec / RunGrantClaims, gateway RunPolicy, control-plane notifications)."""
from __future__ import annotations

import base64
import io
import json
from typing import Any, Callable

import httpx
import jsonschema
import referencing
import yaml

from replicant_worker import resources


class S3Error(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self) -> None:
        self.objs: dict[tuple[str, str], bytes] = {}
        self.puts: list[tuple[str, str]] = []

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if (Bucket, Key) not in self.objs:
            raise S3Error("NoSuchKey")
        return {"Body": io.BytesIO(self.objs[(Bucket, Key)])}

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if (Bucket, Key) not in self.objs:
            raise S3Error("404")
        return {"ContentLength": len(self.objs[(Bucket, Key)])}

    def put_object(self, Bucket: str, Key: str, Body: bytes, IfNoneMatch: str | None = None, **kw: Any) -> dict[str, Any]:  # noqa: N803
        if IfNoneMatch == "*" and (Bucket, Key) in self.objs:
            raise S3Error("PreconditionFailed")
        self.objs[(Bucket, Key)] = bytes(Body)
        self.puts.append((Bucket, Key))
        return {}

    def list_objects_v2(self, Bucket: str, Prefix: str = "", **kw: Any) -> dict[str, Any]:  # noqa: N803
        return {"Contents": [{"Key": k} for (b, k) in sorted(self.objs) if b == Bucket and k.startswith(Prefix)],
                "IsTruncated": False}

    def copy_object(self, Bucket: str, Key: str, CopySource: dict[str, str]) -> dict[str, Any]:  # noqa: N803
        self.objs[(Bucket, Key)] = self.objs[(CopySource["Bucket"], CopySource["Key"])]
        return {}

    def json(self, bucket: str, key: str) -> Any:
        return json.loads(self.objs[(bucket, key)])


def _registry() -> referencing.Registry:
    reg = referencing.Registry()
    for p in [*(resources.SPEC / "03-agents-and-tools" / "schemas").glob("*.json"),
              *(resources.CONTRACTS / "schemas").glob("*.json"), resources.CONTRACTS / "job-request.schema.json"]:
        d = json.loads(p.read_text())
        if "$id" in d:
            reg = reg.with_resource(d["$id"], referencing.Resource.from_contents(d))
    return reg


REG = _registry()


def ws03_validator(name: str) -> jsonschema.Draft202012Validator:
    d = json.loads((resources.SPEC / "03-agents-and-tools" / "schemas" / name).read_text())
    return jsonschema.Draft202012Validator(d, registry=REG)


def openapi_validator(rel: str, schema: str) -> jsonschema.Draft202012Validator:
    d = yaml.safe_load((resources.SPEC / rel).read_text())
    return jsonschema.Draft202012Validator({"$ref": f"#/components/schemas/{schema}", "components": d["components"]})


def errors(v: jsonschema.Draft202012Validator, doc: Any) -> list[str]:
    return [f"{e.message[:160]} at {list(e.path)}" for e in sorted(v.iter_errors(doc), key=lambda e: list(e.path))]


def jwt_parts(tok: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    h, c, s = tok.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
    return (json.loads(base64.urlsafe_b64decode(pad(h))), json.loads(base64.urlsafe_b64decode(pad(c))),
            base64.urlsafe_b64decode(pad(s)), f"{h}.{c}".encode())


class Router:
    """httpx MockTransport that dispatches (method, path-prefix) to handlers and records requests."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Callable[[httpx.Request], httpx.Response]]] = []
        self.calls: list[httpx.Request] = []

    def on(self, method: str, prefix: str, fn: Callable[[httpx.Request], httpx.Response]) -> None:
        self.routes.append((method, prefix, fn))

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        for m, p, fn in self.routes:
            if req.method == m and req.url.path.startswith(p):
                return fn(req)
        return httpx.Response(404, json={"code": "not_found"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))
