"""Postmark webhooks (architecture.md §16, §17):
``/api/webhooks/postmark/inbound`` and ``/api/webhooks/postmark/bounce``.

Postmark signs nothing; the webhook URLs carry HTTP basic auth (``POSTMARK_WEBHOOK_BASIC_AUTH``,
``user:pass``), compared in constant time. Without the setting every request is refused. The API
only stores the payload in ``webhook_events`` and the outbox row of ``process_webhook_event`` in
one transaction, then answers 200; worker-default does the rest. Attachment contents are dropped
before storing (their name, type and size stay): replies are read as text (K146).
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from kalkan.config import Settings
from kalkan.db.outbox import PROCESS_WEBHOOK_EVENT
from kalkan.db.session import Database
from kalkan.domain.enums import WebhookSource
from kalkan.logging import get_logger

router = APIRouter(prefix="/api/webhooks")
# Postmark accepts inbound messages up to 35 MB; the JSON is base64 and larger still.
MAX_BODY = 64 * 1024 * 1024
UNAUTHORIZED = {"WWW-Authenticate": 'Basic realm="webhooks"'}

log = get_logger(__name__)


def authorized(settings: Settings, header: str | None) -> bool:
    expected = settings.postmark_webhook_basic_auth
    if expected is None or not header:
        return False
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        given = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    return hmac.compare_digest(given, expected.get_secret_value().encode())


def _without_attachment_contents(payload: dict[str, Any]) -> dict[str, Any]:
    attachments = payload.get("Attachments")
    if isinstance(attachments, list):
        payload["Attachments"] = [
            {key: value for key, value in item.items() if key != "Content"}
            for item in attachments
            if isinstance(item, dict)
        ]
    payload.pop("Content", None)  # a bounce's full content, when enabled
    return payload


def _write(database: Database, source: WebhookSource, payload: dict[str, Any]) -> uuid.UUID:
    with database.api_preauth_session() as preauth:
        event_id = preauth.store_webhook_event(source=source, payload=payload, signature_ok=True)
        preauth.enqueue_job(PROCESS_WEBHOOK_EVENT, {"event_id": str(event_id)})
    return event_id


async def _json_body(request: Request, limit: int) -> dict[str, Any] | JSONResponse:
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > limit:
        return JSONResponse({"detail": "Too large"}, status_code=413)
    body = await request.body()
    if len(body) > limit:
        return JSONResponse({"detail": "Too large"}, status_code=413)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    return payload


async def _accept(request: Request, source: WebhookSource, payload: dict[str, Any]) -> Response:
    database: Database = request.app.state.database
    event_id = await run_in_threadpool(_write, database, source, payload)
    log.info("webhooks.stored", source=source.value, event_id=str(event_id))
    return JSONResponse({"ok": True})


async def _store(request: Request, source: WebhookSource) -> Response:
    settings: Settings = request.app.state.settings
    if not authorized(settings, request.headers.get("authorization")):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401, headers=UNAUTHORIZED)
    payload = await _json_body(request, MAX_BODY)
    if isinstance(payload, JSONResponse):
        return payload
    return await _accept(request, source, _without_attachment_contents(payload))


@router.post("/postmark/inbound")
async def postmark_inbound(request: Request) -> Response:
    return await _store(request, WebhookSource.POSTMARK_INBOUND)


@router.post("/postmark/bounce")
async def postmark_bounce(request: Request) -> Response:
    return await _store(request, WebhookSource.POSTMARK_BOUNCE)
