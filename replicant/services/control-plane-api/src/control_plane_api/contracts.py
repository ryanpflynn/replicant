"""Frozen contract schemas (verbatim copies of spec/00-contracts and spec/07 schemas, packaged with the service).
`REPLICANT_CONTRACTS_DIR` (e.g. replicant/contracts) overrides the packaged copies."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

_FILES = {
    "job-request": "job-request.schema.json",
    "workflow-messages": "workflow-messages.schema.json",
    "release-bundle": "release-bundle.schema.json",
    "tenant-settings": "tenant-settings.schema.json",
    "definition-envelope": "definition-envelope.schema.json",
    "notification-preferences": "notification-preferences.schema.json",
    "ui-preferences": "ui-preferences.schema.json",
    "project-config": "project-config.schema.json",
}


@lru_cache(maxsize=None)
def schema(name: str) -> dict[str, Any]:
    d = os.environ.get("REPLICANT_CONTRACTS_DIR")
    fname = _FILES[name]
    if d:
        for sub in ("", "schemas"):
            p = os.path.join(d, sub, fname)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
    return json.loads(resources.files("control_plane_api.schemas").joinpath(fname).read_text("utf-8"))


@lru_cache(maxsize=None)
def validator(name: str, definition: str | None = None) -> Draft202012Validator:
    s = schema(name)
    if definition:
        s = {"$schema": s.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
             "$defs": s["$defs"], "$ref": f"#/$defs/{definition}"}
    return Draft202012Validator(s, format_checker=FormatChecker())


def errors(name: str, doc: Any, definition: str | None = None, limit: int = 20) -> list[dict[str, str]]:
    out = []
    for e in sorted(validator(name, definition).iter_errors(doc), key=lambda e: list(e.absolute_path)):
        out.append({"path": "/" + "/".join(str(p) for p in e.absolute_path), "message": e.message[:300]})
        if len(out) >= limit:
            break
    return out
