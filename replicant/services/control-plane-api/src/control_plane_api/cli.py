"""Entrypoint.

    control-plane-api serve [--port P]          the API (REPLICANT_HTTP_PORT, default 8401) + intake/outbox retriers
    control-plane-api serve-grants --port P     the minimal run-grant JWKS / revocation app (architecture.md §2.7)
    control-plane-api healthcheck [--url U]     exit 0 when /readyz answers 200
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request


def _log_ctx(record: logging.LogRecord) -> dict:
    """job_id / run_id / trace_id / span_id for the line (replicant-telemetry, M2); {} when not installed."""
    try:
        from replicant_telemetry import log_context
    except ImportError:
        return {}
    try:
        return log_context(record)
    except Exception:  # noqa: BLE001 - logging must never fail
        return {}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        base = {"level": record.levelname.lower(), "logger": record.name, "service": "control-plane-api", **_log_ctx(record)}
        if msg.startswith("{") and record.name == "replicant.audit":
            try:
                return json.dumps({**base, **json.loads(msg)})
            except ValueError:
                pass
        out = {**base, "msg": msg}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(out)


def _logging() -> None:
    import os
    from replicant_telemetry.redact import RedactingFormatter
    h = logging.StreamHandler(sys.stdout)
    if os.environ.get("REPLICANT_LOG_FORMAT", "json") == "json":
        h.setFormatter(RedactingFormatter(JsonFormatter()))
    logging.basicConfig(level=os.environ.get("REPLICANT_LOG_LEVEL", "INFO"), handlers=[h], force=True)


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
    _logging()
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
