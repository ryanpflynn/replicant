"""Activity-side configuration from the environment (names from spec/11-local-dev/compose/compose.yaml). Secrets are
read from files and never logged."""
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
    s3_endpoint: str | None
    s3_region: str
    runner_url: str
    runner_token_file: str | None
    gateway_url: str
    gateway_token_file: str | None
    control_plane_url: str
    control_plane_token_file: str | None
    cassette_set: str | None
    script_set: str | None
    script_key: str
    script_sets: dict[str, str]  # repo name -> script set (REPLICANT_GATEWAY_SCRIPT_SETS)
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


def script_sets(v: str | None) -> dict[str, str]:
    """`billing-py=walkthrough-billing-py,...`: the script set for jobs on that repo instead of
    REPLICANT_GATEWAY_SCRIPT_SET (local scripted gateway)."""
    out = {}
    for part in (v or "").split(","):
        repo, sep, name = part.strip().partition("=")
        if sep and repo.strip() and name.strip():
            out[repo.strip()] = name.strip()
    return out


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
        cassette_set=env("REPLICANT_GATEWAY_CASSETTE_SET"), script_set=env("REPLICANT_GATEWAY_SCRIPT_SET"),
        script_key=env("REPLICANT_GATEWAY_SCRIPT_KEY", "{node_key}.c{cycle}.n{ordinal}") or "{node_key}.c{cycle}.n{ordinal}",
        script_sets=script_sets(env("REPLICANT_GATEWAY_SCRIPT_SETS")),
        kill_switch_mirror=env("REPLICANT_KILL_SWITCH_MIRROR"),
        deterministic=env("REPLICANT_DETERMINISTIC_RUNS", "false") == "true",
        registry_publish=env("REPLICANT_LOCAL_REGISTRY_PUBLISH", "true" if fenv == "local" else "false") == "true",
    )
