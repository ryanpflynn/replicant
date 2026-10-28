"""`github-integration [serve [--role all|ingress|consumer]] | healthcheck`."""
from __future__ import annotations

import argparse
import dataclasses
import urllib.request

from replicant_telemetry.jsonlog import JsonFormatter, json_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="github-integration")
    ap.add_argument("cmd", nargs="?", default="serve", choices=["serve", "healthcheck"])
    ap.add_argument("--role", choices=["all", "ingress", "consumer"], default=None)
    a = ap.parse_args(argv)
    json_logging(JsonFormatter("github-integration", context="head", merge_json=True))
    from github_integration.config import Settings
    s = Settings.from_env()
    if a.role:
        s = dataclasses.replace(s, role=a.role)
    if a.cmd == "healthcheck":
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{s.http_port}/readyz", timeout=3) as r:
                return 0 if r.status == 200 else 1
        except Exception:  # noqa: BLE001
            return 1
    import uvicorn

    from github_integration.app import create_app
    uvicorn.run(create_app(s), host="0.0.0.0", port=s.http_port, log_config=None, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
