"""ReplicantContextInterceptor: carries the `_replicant-ctx` header (job_id, tenant_id, optional traceparent) from
clients into workflows, from workflows into activities and child workflows, and from activities back into
any client call they make. WS-08's replicant_telemetry adds spans and log fields on top of the same header; in v0
(no observability profile) this is the whole implementation.

Headers are never codec-encoded (HeaderCodecBehavior.NO_CODEC), so run-notifier-style processes can read them.
The header value is a JSON object, at most 1 KiB, holding identifiers only (never tenant content).
"""
from __future__ import annotations

import contextvars
import logging
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional, Type

import temporalio.client
import temporalio.worker
from temporalio import converter
from temporalio.api.common.v1 import Payload

HEADER = "_replicant-ctx"
_FIELDS = ("job_id", "tenant_id", "run_id", "traceparent", "request_id")


@dataclass(frozen=True)
class ReplicantContext:
    job_id: str | None = None
    tenant_id: str | None = None
    run_id: str | None = None
    traceparent: str | None = None
    request_id: str | None = None

    def to_payload(self) -> Payload:
        return converter.default().payload_converter.to_payloads([{k: v for k, v in asdict(self).items() if v}])[0]

    @classmethod
    def from_payload(cls, p: Payload | None) -> Optional["ReplicantContext"]:
        if p is None:
            return None
        try:
            d = converter.default().payload_converter.from_payloads([p])[0]
        except Exception:  # noqa: BLE001 - a malformed header never breaks a call
            return None
        if not isinstance(d, dict):
            return None
        return cls(**{k: str(d[k])[:200] for k in _FIELDS if d.get(k)})

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any]) -> "ReplicantContext":
        return cls(**{k: str(m[k]) for k in _FIELDS if m.get(k)})


_current: contextvars.ContextVar[ReplicantContext | None] = contextvars.ContextVar("replicant_ctx", default=None)


def current_context() -> ReplicantContext | None:
    """The `_replicant-ctx` of the activity (or client scope) being executed, if any."""
    return _current.get()


class use_context:  # noqa: N801 - context manager used like a function
    """`with use_context(ReplicantContext(job_id=...)):` - client calls inside carry the header."""

    def __init__(self, ctx: ReplicantContext | Mapping[str, Any]) -> None:
        self._ctx = ctx if isinstance(ctx, ReplicantContext) else ReplicantContext.from_mapping(ctx)
        self._tok: contextvars.Token | None = None

    def __enter__(self) -> ReplicantContext:
        self._tok = _current.set(self._ctx)
        return self._ctx

    def __exit__(self, *exc: Any) -> None:
        if self._tok is not None:
            _current.reset(self._tok)


def _with_header(headers: Mapping[str, Payload], ctx: ReplicantContext | None) -> Mapping[str, Payload]:
    if ctx is None or HEADER in headers:
        return headers
    return {**headers, HEADER: ctx.to_payload()}


# --------------------------------------------------------------------------------------------- client side
class _ClientOutbound(temporalio.client.OutboundInterceptor):
    async def start_workflow(self, input: temporalio.client.StartWorkflowInput) -> Any:  # noqa: A002
        ctx = current_context()
        if ctx is None and input.id.startswith("job_"):
            ctx = ReplicantContext(job_id=input.id)
        input.headers = _with_header(input.headers, ctx)
        return await super().start_workflow(input)

    async def signal_workflow(self, input: temporalio.client.SignalWorkflowInput) -> None:  # noqa: A002
        input.headers = _with_header(input.headers, current_context())
        return await super().signal_workflow(input)

    async def start_workflow_update(self, input: temporalio.client.StartWorkflowUpdateInput) -> Any:  # noqa: A002
        input.headers = _with_header(input.headers, current_context())
        return await super().start_workflow_update(input)


# --------------------------------------------------------------------------------------------- activity side
class _ActivityInbound(temporalio.worker.ActivityInboundInterceptor):
    async def execute_activity(self, input: temporalio.worker.ExecuteActivityInput) -> Any:  # noqa: A002
        ctx = ReplicantContext.from_payload(input.headers.get(HEADER))
        if ctx is None:
            return await super().execute_activity(input)
        tok = _current.set(ctx)
        try:
            return await super().execute_activity(input)
        finally:
            _current.reset(tok)


# --------------------------------------------------------------------------------------------- workflow side
class _WorkflowOutbound(temporalio.worker.WorkflowOutboundInterceptor):
    def __init__(self, next: temporalio.worker.WorkflowOutboundInterceptor, owner: "_WorkflowInbound") -> None:  # noqa: A002
        super().__init__(next)
        self._owner = owner

    def start_activity(self, input: temporalio.worker.StartActivityInput) -> Any:  # noqa: A002
        if self._owner.header is not None and HEADER not in input.headers:
            input.headers = {**input.headers, HEADER: self._owner.header}
        return super().start_activity(input)

    def start_local_activity(self, input: temporalio.worker.StartLocalActivityInput) -> Any:  # noqa: A002
        if self._owner.header is not None and HEADER not in input.headers:
            input.headers = {**input.headers, HEADER: self._owner.header}
        return super().start_local_activity(input)

    async def start_child_workflow(self, input: temporalio.worker.StartChildWorkflowInput) -> Any:  # noqa: A002
        if self._owner.header is not None and HEADER not in input.headers:
            input.headers = {**input.headers, HEADER: self._owner.header}
        return await super().start_child_workflow(input)


class _WorkflowInbound(temporalio.worker.WorkflowInboundInterceptor):
    header: Payload | None = None

    def init(self, outbound: temporalio.worker.WorkflowOutboundInterceptor) -> None:
        super().init(_WorkflowOutbound(outbound, self))

    async def execute_workflow(self, input: temporalio.worker.ExecuteWorkflowInput) -> Any:  # noqa: A002
        self.header = input.headers.get(HEADER) or self.header
        return await super().execute_workflow(input)


class ReplicantContextInterceptor(temporalio.client.Interceptor, temporalio.worker.Interceptor):
    """Pass to Client.connect (replicant_temporal.connect adds it); workers built from that client inherit it."""

    def intercept_client(self, next: temporalio.client.OutboundInterceptor) -> temporalio.client.OutboundInterceptor:  # noqa: A002
        return _ClientOutbound(next)

    def intercept_activity(self, next: temporalio.worker.ActivityInboundInterceptor) -> temporalio.worker.ActivityInboundInterceptor:  # noqa: A002
        return _ActivityInbound(next)

    def workflow_interceptor_class(self, input: temporalio.worker.WorkflowInterceptorClassInput) -> Type[_WorkflowInbound]:  # noqa: A002
        return _WorkflowInbound


class ContextLogFilter(logging.Filter):
    """Adds job_id / tenant_id / run_id from the current `_replicant-ctx` to log records (JSON log format)."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = current_context()
        for k in ("job_id", "tenant_id", "run_id"):
            if not hasattr(record, k):
                setattr(record, k, getattr(ctx, k, None) if ctx else None)
        return True
