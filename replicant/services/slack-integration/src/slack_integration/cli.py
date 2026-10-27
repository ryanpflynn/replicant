"""`slack-integration [serve | healthcheck]`."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        base = {"level": record.levelname.lower(), "logger": record.name, "service": "slack-integration"}
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
    ap = argparse.ArgumentParser(prog="slack-integration")
    ap.add_argument("cmd", nargs="?", default="serve", choices=["serve", "healthcheck"])
    a = ap.parse_args(argv)
    from replicant_telemetry.redact import RedactingFormatter
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(RedactingFormatter(JsonFormatter()))
    logging.basicConfig(level=os.environ.get("REPLICANT_LOG_LEVEL", "INFO"), handlers=[h], force=True)
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
