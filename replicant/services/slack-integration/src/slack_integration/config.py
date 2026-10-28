"""Configuration. Env names from spec/11-local-dev/compose/compose.yaml (`slack-integration`) plus
REPLICANT_NOTIFY_CALLER_TOKEN_FILE (NOTIFY contract) and the optional SLACK_SIGNING_SECRET_PREVIOUS_FILE (rotation,
approvals.md §4.2)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from replicant_telemetry.common import read_secret  # noqa: F401  (re-exported for callers)

DEFAULT_SLACK_API_URL = "https://slack.com/api/"


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    replicant_env: str = "local"
    http_port: int = 8080
    db_dsn: str | None = None
    db_password_file: str | None = None
    slack_signing_secret_file: str | None = None
    slack_signing_secret_previous_file: str | None = None
    slack_bot_token_file: str | None = None
    slack_api_url: str = DEFAULT_SLACK_API_URL
    notify_caller_token_file: str | None = None
    slack_timeout_s: float = 10.0
    control_plane_url: str = "http://control-plane-api:8401"
    control_plane_token_file: str | None = None          # caller token of svc_slack-integration (gate decisions)
    web_url: str = "http://127.0.0.1:8400"               # links in App Home
    decision_retry_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> "Settings":
        api = env("SLACK_API_URL", DEFAULT_SLACK_API_URL) or DEFAULT_SLACK_API_URL
        return cls(
            replicant_env=env("REPLICANT_ENV", "local") or "local",
            http_port=int(env("REPLICANT_HTTP_PORT", "8080") or 8080),
            db_dsn=env("REPLICANT_DB_DSN"),
            db_password_file=env("REPLICANT_DB_PASSWORD_FILE"),
            slack_signing_secret_file=env("SLACK_SIGNING_SECRET_FILE"),
            slack_signing_secret_previous_file=env("SLACK_SIGNING_SECRET_PREVIOUS_FILE"),
            slack_bot_token_file=env("SLACK_BOT_TOKEN_FILE"),
            slack_api_url=api if api.endswith("/") else api + "/",
            notify_caller_token_file=env("REPLICANT_NOTIFY_CALLER_TOKEN_FILE"),
            slack_timeout_s=float(env("SLACK_API_TIMEOUT_SECONDS", "10") or 10),
            control_plane_url=(env("REPLICANT_CONTROL_PLANE_URL", "http://control-plane-api:8401") or "").rstrip("/"),
            control_plane_token_file=env("REPLICANT_CONTROL_PLANE_TOKEN_FILE"),
            web_url=(env("REPLICANT_WEB_URL", "http://127.0.0.1:8400") or "").rstrip("/"),
            decision_retry_seconds=float(env("REPLICANT_SLACK_DECISION_RETRY_SECONDS", "5") or 5),
        )

    def signing_secrets(self) -> list[str]:
        """Current and (during rotation) previous signing secret; read per request so rotation needs no restart."""
        return [s for s in (read_secret(self.slack_signing_secret_file),
                            read_secret(self.slack_signing_secret_previous_file)) if s]
