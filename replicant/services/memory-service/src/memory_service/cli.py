"""memory-service serve | healthcheck. JSON logs through replicant_telemetry's RedactingFormatter."""
from __future__ import annotations

import argparse
import urllib.request

from replicant_telemetry.jsonlog import JsonFormatter, json_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="memory-service")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int)
    hc = sub.add_parser("healthcheck")
    hc.add_argument("--url", default=None)
    a = ap.parse_args(argv)
    from memory_service.config import Settings
    settings = Settings.from_env()
    if (a.cmd or "serve") == "serve":
        json_logging(JsonFormatter("memory-service"), format_env="json_or_default")
        import uvicorn

        from memory_service.app import create_app
        uvicorn.run(create_app(settings), host="0.0.0.0", port=getattr(a, "port", None) or settings.http_port,
                    log_config=None, access_log=False)
        return 0
    try:
        with urllib.request.urlopen(a.url or f"http://127.0.0.1:{settings.http_port}/readyz", timeout=3) as r:
            return 0 if r.status == 200 else 1
    except Exception:  # noqa: BLE001
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
