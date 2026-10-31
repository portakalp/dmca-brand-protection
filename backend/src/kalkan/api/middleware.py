"""Pure ASGI middleware: request context and the edge shared-secret check."""

from __future__ import annotations

import hmac
import re
import time
from http import HTTPStatus
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kalkan.api.problems import REQUEST_ID_HEADER, problem_response
from kalkan.logging import get_logger, redact_path

# Process health endpoints: no edge header (Cloud Run probes, uptime checks), quiet logs.
# /api/health is the alias that works through *.run.app (K80).
HEALTH_PATHS = frozenset({"/healthz", "/api/healthz", "/api/health", "/readyz"})
EDGE_AUTH_HEADER = "X-Edge-Auth"

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

log = get_logger(__name__)


class RequestContextMiddleware:
    """Assign a request id, expose it in the response and logs, and log one line per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = incoming if incoming and _VALID_REQUEST_ID.fullmatch(incoming) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        status_code = HTTPStatus.INTERNAL_SERVER_ERROR.value
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            finally:
                path = scope["path"]
                log_method = log.debug if path in HEALTH_PATHS else log.info
                log_method(
                    "http.request",
                    method=scope["method"],
                    path=redact_path(path),
                    status=status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )


class EdgeAuthMiddleware:
    """Reject requests that did not pass the Cloudflare edge Worker (architecture.md §22).

    Disabled when no secret is configured (local development).
    """

    def __init__(self, app: ASGIApp, secret: str | None) -> None:
        self.app = app
        self.secret = secret.encode() if secret else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.secret is None or scope["type"] != "http" or scope["path"] in HEALTH_PATHS:
            await self.app(scope, receive, send)
            return

        provided = Headers(scope=scope).get(EDGE_AUTH_HEADER)
        if provided is not None and hmac.compare_digest(provided.encode(), self.secret):
            await self.app(scope, receive, send)
            return

        response = problem_response(
            scope, HTTPStatus.FORBIDDEN, detail="Requests must come through the edge."
        )
        await response(scope, receive, send)
