"""Client factory. Every replicant process connects through `connect()` so TLS, namespace, interceptors and the
header codec behaviour are identical everywhere (env names from spec/11-local-dev/compose/compose.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Sequence

from temporalio.client import Client, Interceptor, TLSConfig


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


@dataclass
class TemporalSettings:
    address: str = "localhost:7233"
    namespace: str = "replicant-local"
    tls: bool = False
    tls_cert_file: str | None = None
    tls_key_file: str | None = None
    tls_ca_file: str | None = None
    identity: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, *, identity: str | None = None) -> "TemporalSettings":
        env = _env("REPLICANT_ENV", "local")
        return cls(
            address=_env("REPLICANT_TEMPORAL_ADDRESS", "localhost:7233") or "localhost:7233",
            namespace=_env("REPLICANT_TEMPORAL_NAMESPACE", f"replicant-{env}") or f"replicant-{env}",
            tls=(_env("REPLICANT_TEMPORAL_TLS", "false") == "true"),
            tls_cert_file=_env("REPLICANT_TEMPORAL_TLS_CERT_FILE"),
            tls_key_file=_env("REPLICANT_TEMPORAL_TLS_KEY_FILE"),
            tls_ca_file=_env("REPLICANT_TEMPORAL_TLS_CA_FILE"),
            identity=identity,
        )

    def tls_config(self) -> TLSConfig | bool:
        if not self.tls:
            return False
        if not (self.tls_cert_file and self.tls_key_file):
            raise ValueError("REPLICANT_TEMPORAL_TLS=true needs REPLICANT_TEMPORAL_TLS_CERT_FILE and _KEY_FILE")
        return TLSConfig(client_cert=_read(self.tls_cert_file), client_private_key=_read(self.tls_key_file),
                         server_root_ca_cert=_read(self.tls_ca_file) if self.tls_ca_file else None)


async def connect(settings: TemporalSettings | None = None, *, data_converter: Any = None,
                  interceptors: Sequence[Interceptor] | None = None, runtime: Any = None,
                  default_interceptors: bool = True, **kwargs: Any) -> Client:
    """Connect to Temporal. Adds ReplicantContextInterceptor (header `_replicant-ctx`) unless
    default_interceptors=False, and keeps headers out of the payload codec (HeaderCodecBehavior.NO_CODEC, the SDK
    default, stated explicitly; XD-08-01) so non-tenant processes can read them. v0 has no payload codec (P2)."""
    from temporalio.common import HeaderCodecBehavior

    from replicant_temporal.interceptors import ReplicantContextInterceptor

    s = settings or TemporalSettings.from_env()
    ics: list[Interceptor] = list(interceptors or [])
    if default_interceptors and not any(isinstance(i, ReplicantContextInterceptor) for i in ics):
        ics.append(ReplicantContextInterceptor())
    if default_interceptors:
        # WS-08 tracing (M2): spans for starts, Signals/Updates and activities in the job's T-1 trace, when
        # replicant-telemetry is installed and OTEL_SDK_DISABLED is not true. Absent package = v0 behavior.
        try:
            from replicant_telemetry import runtime as _ft_runtime
            from replicant_telemetry.temporal import TracingInterceptor
        except ImportError:
            TracingInterceptor = None  # type: ignore[assignment,misc]  # noqa: N806
        if TracingInterceptor is not None and _ft_runtime.enabled() and \
                not any(isinstance(i, TracingInterceptor) for i in ics):
            ics.append(TracingInterceptor())
    kw: dict[str, Any] = {"namespace": s.namespace, "tls": s.tls_config(), "interceptors": ics,
                          "header_codec_behavior": HeaderCodecBehavior.NO_CODEC, **kwargs}
    if s.identity:
        kw["identity"] = s.identity
    if data_converter is not None:
        kw["data_converter"] = data_converter
    if runtime is not None:
        kw["runtime"] = runtime
    return await Client.connect(s.address, **kw)
