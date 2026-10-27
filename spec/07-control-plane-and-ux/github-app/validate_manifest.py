#!/usr/bin/env python3
"""Validate github-app/manifest.json.

  uv run --with jsonschema==4.* python github-app/validate_manifest.py [manifest.json ...]

Checks: (1) JSON Schema (manifest.schema.json); (2) every subscribed event is permitted by a
granted permission (GitHub docs "Webhook events and payloads", checked 2026-10-24);
(3) the dev variant rendered by `replicant dev github-app create` (name/url overrides) also passes.
"""
from __future__ import annotations

import json
import pathlib
import sys

from jsonschema import Draft202012Validator, FormatChecker

HERE = pathlib.Path(__file__).parent
SCHEMA = json.loads((HERE / "manifest.schema.json").read_text())

# event -> (permission, minimum level) per https://docs.github.com/en/webhooks/webhook-events-and-payloads
EVENT_NEEDS = {
    "issues": ("issues", "read"),
    "issue_comment": ("issues", "read"),
    "pull_request": ("pull_requests", "read"),
    "pull_request_review": ("pull_requests", "read"),
    "pull_request_review_comment": ("pull_requests", "read"),
    "pull_request_review_thread": ("pull_requests", "read"),
    "check_run": ("checks", "write"),      # write needed to receive requested_action
    "check_suite": ("checks", "read"),
    "repository": ("metadata", "read"),
    "label": ("metadata", "read"),
    "membership": ("members", "read"),
    "member": ("members", "read"),
    "organization": ("members", "read"),
    "push": ("contents", "read"),
}
LEVEL = {"read": 1, "write": 2}


def dev_variant(m: dict, handle: str = "mtorres", tunnel: str = "https://fancy-otter-1234.trycloudflare.com") -> dict:
    d = json.loads(json.dumps(m))
    d["name"] = f"replicant-dev-{handle}"[:34]
    d["url"] = "https://github.com/acme/replicant"
    d["hook_attributes"]["url"] = f"{tunnel}/github/webhook"
    d["redirect_url"] = "http://localhost:8765/manifest/callback"
    d["callback_urls"] = ["http://localhost:8080/v1/identity/links/github/callback"]
    d["setup_url"] = "http://localhost:8080/admin/github-app/installed"
    return d


def check(m: dict, label: str) -> list[str]:
    errs = [f"{label}: schema: {e.message} at {list(e.absolute_path)}"
            for e in Draft202012Validator(SCHEMA, format_checker=FormatChecker()).iter_errors(m)]
    perms = m.get("default_permissions", {})
    for ev in m.get("default_events", []):
        need = EVENT_NEEDS.get(ev)
        if need is None:
            errs.append(f"{label}: unknown event {ev}")
            continue
        p, lvl = need
        if LEVEL.get(perms.get(p, ""), 0) < LEVEL[lvl]:
            errs.append(f"{label}: event {ev} requires {p}:{lvl}, manifest grants {perms.get(p)}")
    return errs


def main(paths: list[str]) -> int:
    paths = paths or [str(HERE / "manifest.json")]
    errs: list[str] = []
    for p in paths:
        m = json.loads(pathlib.Path(p).read_text())
        errs += check(m, p)
        errs += check(dev_variant(m), p + " (dev variant)")
    print("\n".join(errs) if errs else "manifest OK (prod and dev variant)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
