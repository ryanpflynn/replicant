"""Entrypoint.

    control-plane-api serve [--port P]          the `core` process (core.py): the API (REPLICANT_HTTP_PORT, default 8401)
                                                with github-integration / slack-integration ingress, the projector
                                                worker and every loop (no run-grants listener since SIMPLIFY-D50)
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
    hc = sub.add_parser("healthcheck")
    hc.add_argument("--url", default=None)
    a = ap.parse_args(argv)
    cmd = a.cmd or "serve"
    json_logging(JsonFormatter("control-plane-api", context="head", merge_json="replicant.audit"),
                 format_env="json_or_default")
    from control_plane_api.config import Settings
    settings = Settings.from_env()
    if cmd == "serve":
        import asyncio

        from control_plane_api.core import create_core, serve
        app = create_core(settings)
        return asyncio.run(serve([(app, a.port if getattr(a, "port", None) else settings.http_port)]))
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
