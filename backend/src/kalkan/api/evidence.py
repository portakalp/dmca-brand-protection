"""The evidence page behind a notice's link (architecture.md §11, §17, §24; K140).

``GET /e/{token}`` and ``GET /e/{token}/files/{artifact_id}``. The token is resolved by the
``resolve_evidence_link`` function (expired and revoked links are not found); everything else
is read as ``kalkan_api`` under row level security for the link's customer, and only the link's
own case. The page is server-rendered HTML with every value escaped, never indexed, and shows
what the recipient needs: case number, domain, first seen, screenshots, every captured file
with its SHA-256, registration and hosting. Files are streamed from storage: images inline,
anything else as a download, all under a sandbox policy. Each IP address may make
:data:`LIMIT` requests a minute (process memory; Cloud Run has no Redis).

The token never reaches the logs: request logs mask ``/e/<token>`` (``kalkan.logging``).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update

from kalkan.db.models import Artifact, Case, Domain, EvidenceLink
from kalkan.db.session import Database
from kalkan.domain.enums import ArtifactKind
from kalkan.security import hash_token
from kalkan.storage.gcs import ObjectStore

router = APIRouter(prefix="/e", include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

LIMIT = 60  # requests per IP and minute
WINDOW = 60.0
INLINE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
PAGE_POLICY = (
    "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
FILE_POLICY = "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
HEADERS = {
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}
CHUNK = 64 * 1024


class RateLimiter:
    """A sliding window per key, in this process."""

    def __init__(self, limit: int = LIMIT, window: float = WINDOW) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        at = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= at - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(at)
            if len(self._hits) > 10_000:  # forget idle keys
                for idle in [
                    k for k, v in self._hits.items() if not v or v[-1] <= at - self.window
                ]:
                    del self._hits[idle]
            return True


def client_key(request: Request) -> str:
    """The edge Worker rewrites X-Forwarded-For to the visitor's address; its first hop is the
    visitor. Locally there is no proxy."""
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else "unknown"


def _guard(request: Request) -> None:
    limiter: RateLimiter = request.app.state.evidence_limiter
    if not limiter.allow(client_key(request)):
        raise HTTPException(status_code=429, detail="Too many requests")


def _database(request: Request) -> Database:
    found: Database = request.app.state.database
    return found


def _resolve(request: Request, token: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    if len(token) > 200:
        raise HTTPException(status_code=404)
    with _database(request).api_preauth_session() as preauth:
        link = preauth.resolve_evidence_link(hash_token(token))
    if link is None:
        raise HTTPException(status_code=404)
    return link.link_id, link.case_id, link.customer_id


def _not_found() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><title>Not found</title><p>This evidence link is not valid.</p>",
        status_code=404,
        headers={**HEADERS, "Content-Security-Policy": PAGE_POLICY},
    )


@router.get("/{token}")
def evidence_page(request: Request, token: str) -> Response:
    _guard(request)
    try:
        link_id, case_id, customer_id = _resolve(request, token)
    except HTTPException:
        return _not_found()
    with _database(request).api_session(customer_id) as session:
        case = session.get(Case, case_id)
        if case is None:
            return _not_found()
        domain = session.get_one(Domain, case.domain_id)
        files = list(
            session.scalars(
                select(Artifact)
                .where(Artifact.case_id == case.id)
                .order_by(Artifact.created_at.desc(), Artifact.kind)
            )
        )
        session.execute(
            update(EvidenceLink)
            .where(EvidenceLink.id == link_id)
            .values(views=EvidenceLink.views + 1)
        )
        screenshots = []
        seen: set[object] = set()
        for item in files:
            if item.kind is ArtifactKind.SCREENSHOT and item.profile not in seen:
                seen.add(item.profile)
                screenshots.append(item)
        response = templates.TemplateResponse(
            request,
            "evidence.html",
            {
                "case": case,
                "domain": domain,
                "ips": [str(ip) for ip in domain.ips],
                "files": files,
                "screenshots": screenshots,
                "base": f"/e/{token}",
            },
        )
    response.headers.update({**HEADERS, "Content-Security-Policy": PAGE_POLICY})
    return response


def _chunks(stream: IO[bytes]) -> Iterator[bytes]:
    with stream:
        while chunk := stream.read(CHUNK):
            yield chunk


@router.get("/{token}/files/{artifact_id}")
def evidence_file(request: Request, token: str, artifact_id: uuid.UUID) -> Response:
    _guard(request)
    _, case_id, customer_id = _resolve(request, token)
    with _database(request).api_session(customer_id) as session:
        artifact = session.get(Artifact, artifact_id)
        if artifact is None or artifact.case_id != case_id:
            raise HTTPException(status_code=404)
        key, content_type, kind = artifact.storage_key, artifact.content_type, artifact.kind
    store: ObjectStore = request.app.state.store  # api.app._LazyStore
    try:
        stream = store.open_stream(key)
    except FileNotFoundError:
        raise HTTPException(status_code=404) from None
    inline = content_type in INLINE_TYPES
    name = f"{kind.value}-{artifact_id}"
    headers = {
        **HEADERS,
        "Content-Security-Policy": FILE_POLICY,
        "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{name}"',
    }
    media_type = content_type if inline else "application/octet-stream"
    return StreamingResponse(_chunks(stream), media_type=media_type, headers=headers)
