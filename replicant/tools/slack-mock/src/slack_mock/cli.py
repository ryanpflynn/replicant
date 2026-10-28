"""`slack-mock [serve]`: env SLACK_MOCK_PORT, SLACK_MOCK_CONFIG, SLACK_MOCK_BOT_TOKEN_FILE, SLACK_MOCK_SIGNING_SECRET_FILE,
SLACK_MOCK_INTERACTIVITY_URL, SLACK_MOCK_COMMANDS_URL, SLACK_MOCK_EVENTS_URL, SLACK_MOCK_PUBLIC_URL (optional)."""
from __future__ import annotations

import argparse
import json
import logging
import os

from replicant_telemetry.common import read_secret
from replicant_telemetry.jsonlog import JsonFormatter, json_logging


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def build_app():  # noqa: ANN201
    from slack_mock.app import Workspace, create_app
    log = logging.getLogger("slack_mock")
    cfg = _env("SLACK_MOCK_CONFIG", "/etc/slack-mock/workspace.yaml") or ""
    ws = Workspace.load(cfg)
    token = read_secret(_env("SLACK_MOCK_BOT_TOKEN_FILE"))
    secret = read_secret(_env("SLACK_MOCK_SIGNING_SECRET_FILE"))
    if not token:
        log.warning(json.dumps({"msg": "no bot token configured: every Web API call answers invalid_auth"}))
    if not secret:
        log.warning(json.dumps({"msg": "no signing secret configured: /_mock/send and viewer clicks are disabled"}))
    log.info(json.dumps({"msg": "workspace loaded", "team_id": ws.team.get("id"), "users": len(ws.users),
                         "channels": len(ws.channels)}))
    port = _env("SLACK_MOCK_PORT", "8410")
    return create_app(ws, bot_token=token, signing_secret=secret,
                      interactivity_url=_env("SLACK_MOCK_INTERACTIVITY_URL"), commands_url=_env("SLACK_MOCK_COMMANDS_URL"),
                      events_url=_env("SLACK_MOCK_EVENTS_URL"),
                      public_url=_env("SLACK_MOCK_PUBLIC_URL", f"http://slack-mock:{port}") or "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="slack-mock")
    ap.add_argument("cmd", nargs="?", default="serve", choices=["serve"])
    ap.parse_args(argv)
    json_logging(JsonFormatter("slack-mock", merge_json=True, exc_limit=None))
    import uvicorn
    uvicorn.run(build_app(), host="0.0.0.0", port=int(_env("SLACK_MOCK_PORT", "8410") or 8410), log_config=None,
                access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
