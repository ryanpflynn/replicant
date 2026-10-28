from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from replicant_telemetry.common import read_secret  # noqa: F401  (re-exported for callers)


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    replicant_env: str
    org: str
    broker_url: str
    broker_token_file: str | None
    gitea_base_url: str | None
    s3_endpoint: str | None
    s3_region: str
    kill_switch_mirror: str | None
    installation_id: int
    bot_login: str
    ui_base_url: str

    def bucket(self, cls: str) -> str:
        return f"{self.org}-replicant-{self.replicant_env}-{cls}"

    @property
    def buckets(self) -> set[str]:
        return {self.bucket(c) for c in ("artifacts", "transcripts", "session", "audit", "ops", "registry")}


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings(
        replicant_env=env("REPLICANT_ENV", "local") or "local", org=env("REPLICANT_ORG", "local") or "local",
        broker_url=(env("REPLICANT_TOKEN_BROKER_URL", "http://github-token-broker:8080") or "").rstrip("/"),
        broker_token_file=env("REPLICANT_TOKEN_BROKER_TOKEN_FILE"),
        gitea_base_url=(env("REPLICANT_GITEA_BASE_URL") or "").rstrip("/") or None,
        s3_endpoint=env("REPLICANT_S3_ENDPOINT"), s3_region=env("REPLICANT_S3_REGION", "us-east-1") or "us-east-1",
        kill_switch_mirror=env("REPLICANT_KILL_SWITCH_MIRROR"),
        installation_id=int(env("REPLICANT_SCM_INSTALLATION_ID", "1") or "1"),
        bot_login=env("REPLICANT_REPLICANT_BOT_LOGINS", "replicant-bot").split(",")[0],  # type: ignore[union-attr]
        ui_base_url=(env("REPLICANT_WEB_ORIGIN", "http://app.replicant.localhost:8400") or "").rstrip("/"),
    )
