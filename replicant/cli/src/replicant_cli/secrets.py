"""Local secret generation (stack.md §3). Writes only missing files; never prints a value.

Layout: dev/.secrets/secrets/<compose secret name> (0600), dev/.secrets/generated/*, dev/compose/.env.
Formats (INTERFACES-LOG PLATFORM-03): tokens are 43-char base64url without newline; *_signing_key are PKCS#8
PEM P-256; temporal_codec_key is 64 lowercase hex chars (32 bytes); s3_*_credentials are AWS INI ([default]);
*_tokens / *_readers / *_callers are YAML maps `<token>: <subject>`.
"""
from __future__ import annotations

import base64
import json
import os
import secrets as _rnd
import string
from pathlib import Path

from replicant_cli import engine
from replicant_cli.paths import Paths

DB_ROLES = (
    "replicant_migrator", "svc_control_plane_api", "svc_identity_service", "svc_github_integration",
    "svc_slack_integration", "svc_replicant_projector", "svc_memory_service", "svc_ecosystem_indexer",
    "svc_context_mcp", "svc_usage_ingestor", "svc_eval_worker", "svc_replicant_worker", "svc_publisher_worker",
    "svc_github_token_broker", "svc_retention_sweeper", "grafana_ro",
)
SIMPLE_TOKENS = (
    "pg_replicant_superuser", "s3_admin_secret", "s3_runner_secret", "oidc_replicant_web", "scim_token",
    "dev_seed_token", "gitea_admin_password", "gitea_bot_password", "gitea_webhook_secret", "slack_signing_secret",
    "usage_ingest_token", "runner_revocations_token", "runner_worker_token", "runner_eval_token",
    "broker_mirror_token", "gateway_service_token", "control_plane_shim_token",
    # v0 (CONTROL-02)
    "github_integration_cp_token", "broker_publisher_token", "broker_github_integration_token",
    "replicant_worker_cp_token", "notify_delivery_token", "slack_integration_cp_token",
    # v0 MEM: replicant-worker -> github-token-broker (read-only PR inspection, MEM-D20)
    "broker_worker_token",
    # v0 MEM: control-plane-api -> memory-service (admin proxy; also the HMAC key of the signed caller principal)
    "memory_cpa_token",
    # not mounted by compose; inputs of rendered files
    "s3_services_secret", "s3_gateway_shim_secret", "s3_dev_reader_secret", "s3_langfuse_secret",
)

SUBJECT_REPLICANT_WORKER = "system:serviceaccount:replicant-workers:replicant-worker"
SUBJECT_EVAL_WORKER = "system:serviceaccount:replicant-eval:eval-worker"


def token(n: int = 32) -> str:
    return base64.urlsafe_b64encode(_rnd.token_bytes(n)).rstrip(b"=").decode()


def _write(path: Path, data: str | bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    b = data.encode() if isinstance(data, str) else data
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(b)
    os.chmod(path, mode)


def _ec_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    k = ec.generate_private_key(ec.SECP256R1())
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()


def _jwk_from_pem(pem: str, kid: str) -> dict:
    from cryptography.hazmat.primitives import serialization

    pub = serialization.load_pem_private_key(pem.encode(), None).public_key().public_numbers()
    b64 = lambda i: base64.urlsafe_b64encode(i.to_bytes(32, "big")).rstrip(b"=").decode()  # noqa: E731
    return {"kty": "EC", "crv": "P-256", "x": b64(pub.x), "y": b64(pub.y), "kid": kid, "alg": "ES256", "use": "sig"}


def _yaml_map(m: dict[str, str]) -> str:
    return "".join(f'"{k}": "{v}"\n' for k, v in m.items())


def ensure(p: Paths) -> list[str]:
    """Create missing secrets and generated files. Returns the names created (never values)."""
    created: list[str] = []
    for d in (p.dev_home, p.secrets, p.generated, p.dev_home / "secret-agent"):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(d, 0o700 if d != p.dev_home / "secret-agent" else 0o755)

    def have(name: str) -> bool:
        return (p.secrets / name).is_file() and (p.secrets / name).stat().st_size > 0

    def put(name: str, value: str) -> None:
        _write(p.secrets / name, value)
        created.append(name)

    for r in DB_ROLES:
        if not have(f"db_{r}"):
            put(f"db_{r}", token())
    for n in SIMPLE_TOKENS:
        if not have(n):
            put(n, token())
    if not have("slack_bot_token"):
        put("slack_bot_token", "xoxb-local-" + token(24))
    if not have("langfuse_secret_key"):
        put("langfuse_secret_key", "sk-lf-" + token(24))
    if not have("temporal_codec_key"):
        put("temporal_codec_key", _rnd.token_hex(32))
    for k in ("run_token_signing_key", "run_grant_signing_key"):
        if not have(k):
            put(k, _ec_pem())
    s = p.secret
    maps = {
        "runner_worker_tokens": {s("runner_worker_token"): SUBJECT_REPLICANT_WORKER, s("runner_eval_token"): SUBJECT_EVAL_WORKER},
        "gateway_service_tokens": {s("gateway_service_token"): "svc_replicant-services"},
        "runner_revocations_readers": {s("runner_revocations_token"): "revocation-readers"},
        "control_plane_internal_callers": {s("control_plane_shim_token"): "svc_replicant-gateway-shim",
                                           s("github_integration_cp_token"): "svc_github-integration",
                                           s("replicant_worker_cp_token"): "svc_replicant-worker",
                                           s("slack_integration_cp_token"): "svc_slack-integration"},
        "memory_internal_callers": {s("memory_cpa_token"): "svc_control-plane-api",
                                    s("replicant_worker_cp_token"): "svc_replicant-worker"},
        "broker_caller_tokens": {s("broker_mirror_token"): "repo-mirror", s("broker_publisher_token"): "publisher-worker",
                                 s("broker_github_integration_token"): "github-integration",
                                 s("broker_worker_token"): "replicant-worker"},
    }
    for name, m in maps.items():
        want = _yaml_map(m)
        if not have(name) or (p.secrets / name).read_text() != want:
            put(name, want)
    creds = {
        "s3_services_credentials": ("replicant-services", s("s3_services_secret")),
        "s3_gateway_shim_credentials": ("gateway-shim", s("s3_gateway_shim_secret")),
    }
    for name, (ak, sk) in creds.items():
        want = f"[default]\naws_access_key_id = {ak}\naws_secret_access_key = {sk}\n"
        if not have(name) or (p.secrets / name).read_text() != want:
            put(name, want)

    # generated/: S3 identities (rendered from config/s3/*.tmpl), run-grant JWKS, github-app.env
    tmpl_vars = {
        "S3_ADMIN_SECRET": s("s3_admin_secret"), "S3_RUNNER_SECRET": s("s3_runner_secret"),
        "S3_SERVICES_SECRET": s("s3_services_secret"), "S3_GATEWAY_SHIM_SECRET": s("s3_gateway_shim_secret"),
        "S3_DEV_READER_SECRET": s("s3_dev_reader_secret"), "S3_LANGFUSE_SECRET": s("s3_langfuse_secret"),
    }
    sts = p.secrets / "s3_sts_signing_key"
    if not sts.is_file():
        _write(sts, base64.b64encode(_rnd.token_bytes(32)).decode())
        created.append("s3_sts_signing_key")
    tmpl_vars["S3_STS_SIGNING_KEY_B64"] = sts.read_text().strip()
    for t in ("s3.json", "iam.json"):
        src = (p.compose_dir / "config" / "s3" / f"{t}.tmpl").read_text()
        out = string.Template(src).substitute(tmpl_vars)
        json.loads(out)
        dst = p.generated / t
        if not dst.is_file() or dst.read_text() != out:
            _write(dst, out, 0o644)
            created.append(f"generated/{t}")
    jwks = json.dumps({"keys": [_jwk_from_pem(s("run_grant_signing_key"), "rg-local-1")]}, indent=2) + "\n"
    dst = p.generated / "run-grant-jwks.json"
    if not dst.is_file() or dst.read_text() != jwks:
        _write(dst, jwks, 0o644)
        created.append("generated/run-grant-jwks.json")
    for f, content in (("github-app.env", "# dev GitHub App (M3); empty until `replicant dev github-app create`\n"),):
        if not (p.dev_home / f).is_file():
            _write(p.dev_home / f, content, 0o644)
    if not (p.secrets / "anthropic_api_key").is_file():  # mounted into gateway-shim; empty unless --gateway passthrough
        _write(p.secrets / "anthropic_api_key", "")
    zt = p.generated / "zoekt-gitea-read-token"
    if not zt.is_file():
        _write(zt, "", 0o600)
    _ensure_env(p, created)
    return created


def import_provider_key(p: Paths) -> bool:
    """M2 passthrough: copy ANTHROPIC_API_KEY from replicant/dev/.env (developer-created, gitignored) into the secret
    file `anthropic_api_key` (0600). The value is never printed or logged. Returns True if a non-empty key was found."""
    src = p.root / "dev" / ".env"
    if not src.is_file():
        return False
    key = ""
    for line in src.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if line.startswith("ANTHROPIC_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        return False
    _write(p.secrets / "anthropic_api_key", key)
    return True


def set_env(p: Paths, values: dict[str, str]) -> None:
    """Set keys in dev/compose/.env (mode switches such as REPLICANT_GATEWAY_MODE)."""
    lines = p.env_file.read_text().splitlines() if p.env_file.is_file() else []
    seen: set[str] = set()
    out = []
    for line in lines:
        k = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if k in values:
            out.append(f"{k}={values[k]}")
            seen.add(k)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen]
    _write(p.env_file, "\n".join(out) + "\n")


def _container_socket() -> str:
    """The engine's API socket inside its VM (what a bind mount source refers to)."""
    return engine.socket()


def _ensure_env(p: Paths, created: list[str]) -> None:
    """dev/compose/.env: compose interpolation values (env.example keys). Existing keys are kept."""
    existing: dict[str, str] = {}
    if p.env_file.is_file():
        for line in p.env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v
    defaults = {
        "REPLICANT_DEV_HOME": "../.secrets",
        "REPLICANT_REPO_ROOT": "../..",
        "REPLICANT_IMAGE_REGISTRY": "localhost/replicant",
        "REPLICANT_IMAGE_TAG": "dev",
        "REPLICANT_WORKER_BUILD_ID": "b20261024-000000000000",
        "REPLICANT_WORKER_NEXT_BUILD_ID": "b20261024-111111111111",
        "REPLICANT_GIT_SHA": "000000000000",
        "REPLICANT_RUNNER_URL": "http://runner-local:8480",
        "REPLICANT_GATEWAY_MODE": "script",
        "REPLICANT_LOCAL_RUNTIME": "runc",
        "REPLICANT_MAX_CONCURRENT_RUNS": "2",
        "REPLICANT_OTEL_DISABLED": "true",
        "REPLICANT_CONTAINER_SOCKET": _container_socket(),
        "REPLICANT_CONTAINER_HOST": "unix:///var/run/docker.sock",
        "REPLICANT_KATA": "off",
        "REPLICANT_GVISOR": "off",
        "REPLICANT_S3_IMPL": "seaweedfs",
        "REPLICANT_GATEWAY_REPLAY_MATCH": "sequence",
        "REPLICANT_GATEWAY_CASSETTE_SET": "",
        "REPLICANT_PG_TEMPORAL_PASSWORD": token(),
        "REPLICANT_PG_LANGFUSE_PASSWORD": token(),
        "REPLICANT_CLICKHOUSE_PASSWORD": token(),
        "REPLICANT_VALKEY_PASSWORD": token(),
        "REPLICANT_LANGFUSE_SALT": token(),
        "REPLICANT_LANGFUSE_ENCRYPTION_KEY": _rnd.token_hex(32),
        "REPLICANT_LANGFUSE_NEXTAUTH_SECRET": token(),
        "REPLICANT_LANGFUSE_SECRET_KEY": p.secret("langfuse_secret_key"),
        "REPLICANT_LANGFUSE_ADMIN_PASSWORD": token(18),
        "REPLICANT_S3_LANGFUSE_SECRET": p.secret("s3_langfuse_secret"),
        "REPLICANT_OIDC_WEB_SECRET": p.secret("oidc_replicant_web"),
        "REPLICANT_GRAFANA_RO_PASSWORD": p.secret("db_grafana_ro"),
        "REPLICANT_S3_SSE_KEY": token(),
    }
    defaults["REPLICANT_LANGFUSE_BASIC_AUTH"] = base64.b64encode(
        f"pk-lf-replicant-local:{defaults['REPLICANT_LANGFUSE_SECRET_KEY']}".encode()).decode()
    merged = {**defaults, **existing}
    if merged != existing:
        body = "# Generated by `replicant dev up` (never committed). Edit values here, not in compose.yaml.\n"
        body += "".join(f"{k}={v}\n" for k, v in merged.items())
        _write(p.env_file, body)
        created.append("dev/compose/.env")
