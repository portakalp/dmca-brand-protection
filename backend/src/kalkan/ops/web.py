"""Shared pieces of the ops pages: templates, the signed-in staff member, CSRF and flash text."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from redis import Redis
from sqlalchemy.orm import Session

from kalkan.config import ConfigError, Settings
from kalkan.db.session import Database
from kalkan.ops import auth
from kalkan.storage.gcs import ObjectStore

HERE = Path(__file__).parent
FLASH_COOKIE = "kalkan_ops_flash"

templates = Jinja2Templates(directory=HERE / "templates")


def _short(value: Any, limit: int = 120) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _when(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if value else "—"


templates.env.filters["short"] = _short
templates.env.filters["when"] = _when


@dataclass(frozen=True)
class Services:
    """What the pages use; tests pass their own."""

    settings: Settings
    database: Database
    store: ObjectStore
    redis: Redis


class LoginRequired(Exception):  # noqa: N818 - control flow, handled by the app
    """No signed-in staff member: the request is sent to the sign-in page."""


class CsrfFailed(Exception):  # noqa: N818
    """A POST without the right CSRF token."""


def evidence_bucket(request: Request) -> str:
    bucket = services(request).settings.gcs_evidence_bucket
    if bucket is None:  # pragma: no cover - required when the ops process starts
        raise ConfigError("GCS_EVIDENCE_BUCKET is required")
    return bucket


def services(request: Request) -> Services:
    found: Services = request.app.state.services
    return found


def now() -> datetime:
    return datetime.now(UTC)


def client_ip(request: Request) -> str | None:
    """The peer address; behind the IAP tunnel it is the VM's loopback (no proxy headers)."""
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


@contextmanager
def transaction(request: Request) -> Iterator[Session]:
    """One kalkan_worker transaction; pages open it themselves, so it commits before the
    response is sent."""
    with services(request).database.worker_session() as session:
        yield session


def current_staff(request: Request) -> auth.Staff:
    with transaction(request) as session:
        staff = auth.resolve(session, request.cookies.get(auth.SESSION_COOKIE), now())
    if staff is None:
        raise LoginRequired
    request.state.staff = staff
    return staff


async def csrf_guard(
    request: Request,
    staff: auth.Staff = Depends(current_staff),  # noqa: B008
) -> auth.Staff:
    """The signed-in staff member of a POST that carries the session's CSRF token."""
    given = request.headers.get("x-csrf-token")
    if given is None:
        value = (await request.form()).get("csrf")
        given = value if isinstance(value, str) else None
    if not auth.same(staff.csrf, given):
        raise CsrfFailed
    return staff


def render(
    request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200
) -> Response:
    values: dict[str, Any] = {
        "staff": getattr(request.state, "staff", None),
        "flash": unquote(request.cookies.get(FLASH_COOKIE, "")),
        "path": request.url.path,
        **(context or {}),
    }
    response = templates.TemplateResponse(request, name, values, status_code=status_code)
    if values["flash"]:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response


def redirect(url: str, flash: str | None = None) -> Response:
    """303 to ``url``; ``flash`` is shown once on the next page."""
    response = RedirectResponse(url, status_code=303)
    if flash:
        response.set_cookie(
            FLASH_COOKIE,
            quote(flash[:300]),
            httponly=True,
            secure=True,
            samesite="strict",
            path="/",
        )
    return response


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"
