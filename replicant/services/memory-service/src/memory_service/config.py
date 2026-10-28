"""Configuration from the environment (names as in dev/compose/compose.yaml `memory-service`)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from replicant_telemetry.common import read_secret  # noqa: F401  (re-exported for callers)


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    replicant_env: str = "local"
    org: str = "local"
    http_port: int = 8080
    db_dsn: str | None = None
    db_password_file: str | None = None
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    caller_tokens_file: str | None = None
    gateway_url: str = "http://gateway-shim:8420"
    gateway_token_file: str | None = None
    admin_group: str = "replicant-admins"
    judge_model: str = "anthropic/claude-haiku-5-5"

    def bucket(self, cls: str) -> str:
        return f"{self.org}-replicant-{self.replicant_env}-{cls}"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            replicant_env=env("REPLICANT_ENV", "local") or "local",
            org=env("REPLICANT_ORG", "local") or "local",
            http_port=int(env("REPLICANT_HTTP_PORT", "8080") or 8080),
            db_dsn=env("REPLICANT_DB_DSN"),
            db_password_file=env("REPLICANT_DB_PASSWORD_FILE"),
            s3_endpoint=env("REPLICANT_S3_ENDPOINT"),
            s3_region=env("REPLICANT_S3_REGION", "us-east-1") or "us-east-1",
            caller_tokens_file=env("REPLICANT_MEMORY_CALLER_TOKENS_FILE"),
            gateway_url=(env("REPLICANT_GATEWAY_URL", "http://gateway-shim:8420") or "").rstrip("/"),
            gateway_token_file=env("REPLICANT_GATEWAY_SERVICE_TOKEN_FILE"),
            admin_group=env("REPLICANT_MEMORY_ADMIN_GROUP", "replicant-admins") or "replicant-admins",
            judge_model=env("REPLICANT_MEMORY_JUDGE_MODEL", "anthropic/claude-haiku-5-5") or "anthropic/claude-haiku-5-5",
        )
