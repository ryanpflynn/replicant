"""Activity-side configuration from the environment (names from spec/11-local-dev/compose/compose.yaml). Secrets are
read from files and never logged."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def read_secret(path: str | None) -> str | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


@dataclass(frozen=True)
class Settings:
    replicant_env: str
    org: str
    s3_endpoint: str | None
    s3_region: str
    runner_url: str
    runner_token_file: str | None
    gateway_url: str
    gateway_token_file: str | None
    control_plane_url: str
    control_plane_token_file: str | None
    grant_key_file: str | None
    grant_kid: str
    grant_issuer: str
    cassette_set: str | None
    script_set: str | None
    script_key: str
    kill_switch_mirror: str | None
    deterministic: bool
    registry_publish: bool

    @property
    def local(self) -> bool:
        return self.replicant_env == "local"

    def bucket(self, bucket_class: str) -> str:
        return f"{self.org}-replicant-{self.replicant_env}-{bucket_class}"

    @property
    def buckets(self) -> set[str]:
        return {self.bucket(c) for c in ("artifacts", "transcripts", "session", "audit", "memory", "eval", "registry", "ops")}


@lru_cache(maxsize=1)
def settings() -> Settings:
    fenv = env("REPLICANT_ENV", "local") or "local"
    return Settings(
        replicant_env=fenv, org=env("REPLICANT_ORG", "local") or "local",
        s3_endpoint=env("REPLICANT_S3_ENDPOINT"), s3_region=env("REPLICANT_S3_REGION", "us-east-1") or "us-east-1",
        runner_url=(env("REPLICANT_RUNNER_URL", "http://runner-local:8480") or "").rstrip("/"),
        runner_token_file=env("REPLICANT_RUNNER_TOKEN_FILE"),
        gateway_url=(env("REPLICANT_GATEWAY_URL", "http://gateway-shim:8420") or "").rstrip("/"),
        gateway_token_file=env("REPLICANT_GATEWAY_SERVICE_TOKEN_FILE"),
        control_plane_url=(env("REPLICANT_CONTROL_PLANE_URL", "http://control-plane-api:8401") or "").rstrip("/"),
        control_plane_token_file=env("REPLICANT_CONTROL_PLANE_TOKEN_FILE"),
        grant_key_file=env("REPLICANT_RUN_GRANT_KEY_FILE"), grant_kid=env("REPLICANT_RUN_GRANT_KID", "rg-local-1") or "rg-local-1",
        grant_issuer=env("REPLICANT_RUN_GRANT_ISSUER", f"replicant-{fenv}-core") or f"replicant-{fenv}-core",
        cassette_set=env("REPLICANT_GATEWAY_CASSETTE_SET"), script_set=env("REPLICANT_GATEWAY_SCRIPT_SET"),
        script_key=env("REPLICANT_GATEWAY_SCRIPT_KEY", "{node_key}.c{cycle}.n{ordinal}") or "{node_key}.c{cycle}.n{ordinal}",
        kill_switch_mirror=env("REPLICANT_KILL_SWITCH_MIRROR"),
        deterministic=env("REPLICANT_DETERMINISTIC_RUNS", "false") == "true",
        registry_publish=env("REPLICANT_LOCAL_REGISTRY_PUBLISH", "true" if fenv == "local" else "false") == "true",
    )
