"""RFC 9457 problem details (``application/problem+json``) for every API error."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from kalkan.logging import get_logger

PROBLEM_CONTENT_TYPE = "application/problem+json"
REQUEST_ID_HEADER = "X-Request-ID"

log = get_logger(__name__)


class ProblemError(Exception):
    """Raise from application code to return a specific problem response."""

    def __init__(
        self,
        status: int,
        *,
        title: str | None = None,
        detail: str | None = None,
        type_: str = "about:blank",
        **extensions: Any,
    ) -> None:
        super().__init__(detail or title or HTTPStatus(status).phrase)
        self.status = status
        self.title = title
        self.detail = detail
        self.type = type_
        self.extensions = extensions


def request_id_from_scope(scope: Scope) -> str | None:
    state = scope.get("state") or {}
    request_id = state.get("request_id")
    return request_id if isinstance(request_id, str) else None


def problem_response(
    scope: Scope,
    status: int,
    *,
    title: str | None = None,
    detail: str | None = None,
    type_: str = "about:blank",
    extensions: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    status = int(status)
    body: dict[str, Any] = {
        "type": type_,
        "title": title or HTTPStatus(status).phrase,
        "status": status,
    }
    if detail:
        body["detail"] = detail
    body["instance"] = scope.get("path", "")
    request_id = request_id_from_scope(scope)
    if request_id:
        body["request_id"] = request_id
    if extensions:
        body.update({k: v for k, v in extensions.items() if k not in body})
    response_headers = dict(headers or {})
    if request_id:
        response_headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(
        body, status_code=status, media_type=PROBLEM_CONTENT_TYPE, headers=response_headers
    )


async def _http_exception(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(
        exc, StarletteHTTPException
    ):  # pragma: no cover - registered for this type only
        raise exc
    phrase = HTTPStatus(exc.status_code).phrase
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail != phrase else None
    return problem_response(
        request.scope, exc.status_code, detail=detail, headers=dict(exc.headers or {})
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(
        exc, RequestValidationError
    ):  # pragma: no cover - registered for this type only
        raise exc
    # Submitted values are left out on purpose: they may contain passwords or tokens.
    errors = [
        {"loc": list(error.get("loc", ())), "msg": error.get("msg"), "type": error.get("type")}
        for error in exc.errors()
    ]
    return problem_response(
        request.scope,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        title="Validation failed",
        extensions={"errors": errors},
    )


async def _problem_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ProblemError):  # pragma: no cover - registered for this type only
        raise exc
    return problem_response(
        request.scope,
        exc.status,
        title=exc.title,
        detail=exc.detail,
        type_=exc.type,
        extensions=exc.extensions,
    )


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    log.error(
        "http.unhandled_error",
        request_id=request_id_from_scope(request.scope),
        exc_info=exc,
    )
    return problem_response(request.scope, HTTPStatus.INTERNAL_SERVER_ERROR)


def register_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(ProblemError, _problem_error)
    app.add_exception_handler(Exception, _unhandled_error)
