"""Configuration from the environment. Names are exactly the spec compose's (spec/11-local-dev/compose/compose.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def read_secret(path: str | None) -> str | None:
    """Reads a secret file; strips trailing whitespace. Never logs the value."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class Settings:
    replicant_env: str = "local"
    org: str = "local"
    http_port: int = 8401
    db_dsn: str | None = None
    db_password_file: str | None = None
    db_set_role: bool = True
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "replicant-local"
    web_origin: str = "http://app.replicant.localhost:8400"
    dev_seed_token_file: str | None = None
    internal_caller_tokens_file: str | None = None
    dev_principals_file: str | None = None
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_force_path_style: bool = True
    run_grant_jwks_source: str = "file"
    run_grant_jwks_file: str | None = None
    exec_revocations_urls: tuple[str, ...] = ()
    exec_revocations_token_file: str | None = None
    exec_revocations_poll_seconds: int = 10
    worker_build_id: str = "b20261024-000000000000"
    gateway_shim_url: str | None = None
    background_tasks: bool = True
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def is_local(self) -> bool:
        return self.replicant_env == "local"

    def bucket(self, cls: str) -> str:
        """C-ART bucket naming: <org>-replicant-<env>-<class>."""
        return f"{self.org}-replicant-{self.replicant_env}-{cls}"

    @classmethod
    def from_env(cls) -> "Settings":
        fenv = env("REPLICANT_ENV", "local") or "local"
        return cls(
            replicant_env=fenv,
            org=env("REPLICANT_ORG", "local") or "local",
            http_port=int(env("REPLICANT_HTTP_PORT", "8401") or 8401),
            db_dsn=env("REPLICANT_DB_DSN"),
            db_password_file=env("REPLICANT_DB_PASSWORD_FILE"),
            db_set_role=(env("REPLICANT_DB_SET_ROLE", "true") == "true"),
            temporal_address=env("REPLICANT_TEMPORAL_ADDRESS", "localhost:7233") or "localhost:7233",
            temporal_namespace=env("REPLICANT_TEMPORAL_NAMESPACE", f"replicant-{fenv}") or f"replicant-{fenv}",
            web_origin=env("REPLICANT_WEB_ORIGIN", "http://app.replicant.localhost:8400") or "",
            dev_seed_token_file=env("REPLICANT_DEV_SEED_TOKEN_FILE"),
            internal_caller_tokens_file=env("REPLICANT_INTERNAL_CALLER_TOKENS_FILE"),
            dev_principals_file=env("REPLICANT_DEV_PRINCIPALS_FILE"),
            s3_endpoint=env("REPLICANT_S3_ENDPOINT"),
            s3_region=env("REPLICANT_S3_REGION", "us-east-1") or "us-east-1",
            s3_force_path_style=(env("REPLICANT_S3_FORCE_PATH_STYLE", "true") == "true"),
            run_grant_jwks_source=env("REPLICANT_RUN_GRANT_JWKS_SOURCE", "file") or "file",
            run_grant_jwks_file=env("REPLICANT_RUN_GRANT_JWKS_FILE"),
            exec_revocations_urls=tuple(u for u in (env("REPLICANT_EXEC_REVOCATIONS_URLS", "") or "").split(",") if u),
            exec_revocations_token_file=env("REPLICANT_EXEC_REVOCATIONS_TOKEN_FILE"),
            exec_revocations_poll_seconds=int(env("REPLICANT_EXEC_REVOCATIONS_POLL_SECONDS", "10") or 10),
            worker_build_id=env("REPLICANT_WORKER_BUILD_ID", "b20261024-000000000000") or "b20261024-000000000000",
            gateway_shim_url=env("REPLICANT_GATEWAY_SHIM_URL"),
            background_tasks=(env("REPLICANT_CPA_BACKGROUND_TASKS", "true") == "true"),
        )

    def startup_guard(self) -> None:
        """identity.md §9.3 L-3 and XD-W3-R1V-067: local-only credentials refuse to run outside REPLICANT_ENV=local."""
        if not self.is_local:
            if self.dev_seed_token_file or self.internal_caller_tokens_file or self.dev_principals_file:
                raise SystemExit("P1 security: local-only credential files are configured but REPLICANT_ENV != local")
        else:
            aws_signals = [os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI"),
                           os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")]
            if any(aws_signals) or (self.db_dsn and ".rds.amazonaws.com" in self.db_dsn):
                raise SystemExit("P1 security: REPLICANT_ENV=local while AWS signals are present")


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings.from_env()
