"""Entrypoint.

    control-plane-api serve [--port P]          the API (REPLICANT_HTTP_PORT, default 8401) + intake/outbox retriers
    control-plane-api serve-grants --port P     the minimal run-grant JWKS / revocation app (architecture.md §2.7)
    control-plane-api healthcheck [--url U]     exit 0 when /readyz answers 200
"""
from __future__ import annotations

import argparse
import urllib.request

from replicant_telemetry.jsonlog import JsonFormatter, json_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="control-plane-api")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int)
    g = sub.add_parser("serve-grants")
    g.add_argument("--port", type=int, required=True)
    hc = sub.add_parser("healthcheck")
    hc.add_argument("--url", default=None)
    a = ap.parse_args(argv)
    cmd = a.cmd or "serve"
    json_logging(JsonFormatter("control-plane-api", context="head", merge_json="replicant.audit"),
                 format_env="json_or_default")
    import uvicorn

    from control_plane_api.config import Settings
    settings = Settings.from_env()
    if cmd == "serve":
        from control_plane_api.app import create_app
        uvicorn.run(create_app(settings), host="0.0.0.0", port=a.port if getattr(a, "port", None) else settings.http_port,
                    log_config=None, access_log=False)
        return 0
    if cmd == "serve-grants":
        from control_plane_api.grants import create_grants_app
        uvicorn.run(create_grants_app(settings), host="0.0.0.0", port=a.port, log_config=None, access_log=False)
        return 0
    if cmd == "healthcheck":
        url = a.url or f"http://127.0.0.1:{settings.http_port}/readyz"
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return 0 if r.status == 200 else 1
        except Exception:  # noqa: BLE001
            return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
