"""Evidence and brand files streamed from storage through the panel (architecture.md §19; K118).

Captured files come from hostile sites. Only raster images are shown inline; everything else
(HTML, HAR, JSON, SVG) is a download as ``application/octet-stream``. Uploaded SVG logos are
shown too.
Every file response carries a sandbox policy, so even a mislabelled file cannot run script in
the panel's origin.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import IO, Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from kalkan.db.models import Artifact, BrandAsset, Customer
from kalkan.ops import auth
from kalkan.ops.web import current_staff, services, transaction
from kalkan.storage.gcs import split_key

router = APIRouter(prefix="/files")

INLINE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/x-icon"})
ASSET_INLINE_TYPES = INLINE_TYPES | {"image/svg+xml"}
FILE_POLICY = "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
CHUNK = 64 * 1024
Staff = Annotated[auth.Staff, Depends(current_staff)]


def _chunks(stream: IO[bytes]) -> Iterator[bytes]:
    with stream:
        while chunk := stream.read(CHUNK):
            yield chunk


def _stream(
    request: Request,
    key: str,
    content_type: str,
    name: str,
    inline_types: frozenset[str] = INLINE_TYPES,
) -> Response:
    try:
        stream = services(request).store.open_stream(key)
    except FileNotFoundError:
        raise HTTPException(status_code=404) from None
    inline = content_type in inline_types
    headers = {
        "Content-Security-Policy": FILE_POLICY,
        "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{name}"',
        "Cache-Control": "private, max-age=300",
    }
    media_type = content_type if inline else "application/octet-stream"
    return StreamingResponse(_chunks(stream), media_type=media_type, headers=headers)


def _name(key: str) -> str:
    return split_key(key)[1].rsplit("/", 1)[-1].replace('"', "")


@router.get("/artifacts/{artifact_id}")
def artifact(request: Request, artifact_id: uuid.UUID, _: Staff) -> Response:
    with transaction(request) as session:
        found = session.get(Artifact, artifact_id)
    if found is None:
        raise HTTPException(status_code=404)
    return _stream(request, found.storage_key, found.content_type, _name(found.storage_key))


@router.get("/assets/{asset_id}")
def asset(request: Request, asset_id: uuid.UUID, _: Staff) -> Response:
    with transaction(request) as session:
        found = session.get(BrandAsset, asset_id)
    if found is None:
        raise HTTPException(status_code=404)
    name = _name(found.storage_key)
    content_type = _asset_type(name)
    # Brand files come from the operator; an SVG logo is shown too (no script runs in <img>,
    # and the sandbox policy covers opening it directly).
    return _stream(request, found.storage_key, content_type, name, ASSET_INLINE_TYPES)


def _asset_type(name: str) -> str:
    extension = name.rsplit(".", 1)[-1].lower()
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "ico": "image/x-icon",
        "svg": "image/svg+xml",
    }.get(extension, "application/octet-stream")


@router.get("/loa/{customer_id}")
def loa(request: Request, customer_id: uuid.UUID, _: Staff) -> Response:
    with transaction(request) as session:
        customer = session.get(Customer, customer_id)
    if customer is None or customer.loa_file_key is None:
        raise HTTPException(status_code=404)
    key = customer.loa_file_key
    return _stream(request, key, "application/octet-stream", _name(key))
