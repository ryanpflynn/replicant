"""RFC 9457 problem responses with `code`, `request_id` and (on 403) `audit_id` (architecture.md §2.2)."""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from control_plane_api.ids import ulid


class ProblemError(Exception):
    def __init__(self, status: int, code: str, title: str | None = None, *, detail: str | None = None,
                 headers: dict[str, str] | None = None, audit_id: str | None = None, **extra: Any) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code
        self.title = title or code.replace("_", " ")
        self.detail = detail
        self.headers = headers or {}
        self.audit_id = audit_id
        self.extra = extra


def request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if rid is None:
        rid = request.headers.get("x-request-id") or ulid("req").removeprefix("req_")
        request.state.request_id = rid
    return rid


def problem_response(request: Request, err: ProblemError) -> JSONResponse:
    body: dict[str, Any] = {"type": "about:blank", "title": err.title, "status": err.status, "code": err.code,
                            "request_id": request_id(request)}
    if err.detail:
        body["detail"] = err.detail
    if err.audit_id:
        body["audit_id"] = err.audit_id
    body.update(err.extra)
    return JSONResponse(body, status_code=err.status, headers=err.headers, media_type="application/problem+json")


async def problem_handler(request: Request, exc: ProblemError) -> JSONResponse:
    return problem_response(request, exc)


async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errs = [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()][:20]
    return problem_response(request, ProblemError(400, "invalid_request", detail="request validation failed",
                                                  errors=errs))
