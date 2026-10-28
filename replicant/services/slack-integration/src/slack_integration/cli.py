"""`slack-integration [serve | healthcheck]`."""
from __future__ import annotations

import argparse
import logging
import urllib.request

from replicant_telemetry.jsonlog import JsonFormatter, json_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="slack-integration")
    ap.add_argument("cmd", nargs="?", default="serve", choices=["serve", "healthcheck"])
    a = ap.parse_args(argv)
    json_logging(JsonFormatter("slack-integration", merge_json=True))
    # slack_sdk logs request bodies at DEBUG; keep it at WARNING whatever the root level is.
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)
    from slack_integration.config import Settings
    s = Settings.from_env()
    if a.cmd == "healthcheck":
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{s.http_port}/readyz", timeout=3) as r:
                return 0 if r.status == 200 else 1
        except Exception:  # noqa: BLE001
            return 1
    import uvicorn

    from slack_integration.app import create_app
    uvicorn.run(create_app(s), host="0.0.0.0", port=s.http_port, log_config=None, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
