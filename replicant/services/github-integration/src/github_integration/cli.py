"""`github-integration [serve [--role all|ingress|consumer]] | healthcheck`."""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
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
        base = {"level": record.levelname.lower(), "logger": record.name, "service": "github-integration", **_log_ctx(record)}
        if msg.startswith("{"):
            try:
                return json.dumps({**base, **json.loads(msg)})
            except ValueError:
                pass
        out = {**base, "msg": msg}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="github-integration")
    ap.add_argument("cmd", nargs="?", default="serve", choices=["serve", "healthcheck"])
    ap.add_argument("--role", choices=["all", "ingress", "consumer"], default=None)
    a = ap.parse_args(argv)
    from replicant_telemetry.redact import RedactingFormatter
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(RedactingFormatter(JsonFormatter()))
    logging.basicConfig(level=os.environ.get("REPLICANT_LOG_LEVEL", "INFO"), handlers=[h], force=True)
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
