"""Configuration (env names from spec/11-local-dev/compose/compose.yaml; additions recorded in INTERFACES-LOG)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def read_secret(path: str | None) -> str | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


DEFAULT_LINKED = "alice,bob,carol,dave,erin,grace"


@dataclass(frozen=True)
class Settings:
    replicant_env: str = "local"
    org: str = "local"
    http_port: int = 8080
    db_dsn: str | None = None
    db_password_file: str | None = None
    gitea_webhook_secret_file: str | None = None
    gitea_base_url: str = "http://git.replicant.localhost:3300"
    bot_logins: tuple[str, ...] = ("replicant-bot",)
    control_plane_url: str = "http://control-plane-api:8401"
    control_plane_token_file: str | None = None
    token_broker_url: str = "http://github-token-broker:8080"
    token_broker_token_file: str | None = None
    dev_seed_token_file: str | None = None
    notify_caller_token_file: str | None = None
    dev_linked_logins: tuple[str, ...] = tuple(DEFAULT_LINKED.split(","))
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "replicant-local"
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_force_path_style: bool = True
    web_origin: str = "http://app.replicant.localhost:8400"
    consumer_workers: int = 4
    role: str = "all"

    @property
    def is_local(self) -> bool:
        return self.replicant_env == "local"

    def bucket(self, cls: str) -> str:
        return f"{self.org}-replicant-{self.replicant_env}-{cls}"

    @classmethod
    def from_env(cls) -> "Settings":
        fenv = env("REPLICANT_ENV", "local") or "local"
        return cls(
            replicant_env=fenv, org=env("REPLICANT_ORG", "local") or "local",
            http_port=int(env("REPLICANT_HTTP_PORT", "8080") or 8080),
            db_dsn=env("REPLICANT_DB_DSN"), db_password_file=env("REPLICANT_DB_PASSWORD_FILE"),
            gitea_webhook_secret_file=env("REPLICANT_GITEA_WEBHOOK_SECRET_FILE"),
            gitea_base_url=env("REPLICANT_GITEA_BASE_URL", "http://git.replicant.localhost:3300") or "",
            bot_logins=tuple(x.strip() for x in (env("REPLICANT_REPLICANT_BOT_LOGINS", "replicant-bot") or "").split(",") if x),
            control_plane_url=env("REPLICANT_CONTROL_PLANE_URL", "http://control-plane-api:8401") or "",
            control_plane_token_file=env("REPLICANT_CONTROL_PLANE_TOKEN_FILE"),
            token_broker_url=env("REPLICANT_TOKEN_BROKER_URL", "http://github-token-broker:8080") or "",
            token_broker_token_file=env("REPLICANT_TOKEN_BROKER_TOKEN_FILE"),
            dev_seed_token_file=env("REPLICANT_DEV_SEED_TOKEN_FILE"),
            notify_caller_token_file=env("REPLICANT_NOTIFY_CALLER_TOKEN_FILE"),
            dev_linked_logins=tuple(x.strip() for x in (env("REPLICANT_DEV_LINKED_LOGINS", DEFAULT_LINKED) or "")
                                    .split(",") if x.strip()),
            temporal_address=env("REPLICANT_TEMPORAL_ADDRESS", "localhost:7233") or "",
            temporal_namespace=env("REPLICANT_TEMPORAL_NAMESPACE", f"replicant-{fenv}") or "",
            s3_endpoint=env("REPLICANT_S3_ENDPOINT"), s3_region=env("REPLICANT_S3_REGION", "us-east-1") or "us-east-1",
            s3_force_path_style=(env("REPLICANT_S3_FORCE_PATH_STYLE", "true") == "true"),
            web_origin=env("REPLICANT_WEB_ORIGIN", "http://app.replicant.localhost:8400") or "",
            consumer_workers=int(env("REPLICANT_GITHUB_CONSUMER_WORKERS", "4") or 4),
            role=env("REPLICANT_GITHUB_INTEGRATION_ROLE", "all") or "all",
        )
