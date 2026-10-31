"""Streaming stored files to the panel (architecture.md §11; K18): no signed URLs.

Images go inline with their type; anything else (HTML, HAR, JSON) is a download as
``application/octet-stream``; every file response carries a sandbox policy, so a captured page
can never run in the panel's origin. ``evidence.zip`` is written while it streams: each file is
read from storage in chunks, and ``manifest.json`` lists every file with its SHA-256.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import IO, Any, Protocol

from fastapi import Request
from fastapi.responses import StreamingResponse

from kalkan.api.evidence import CHUNK, INLINE_TYPES
from kalkan.api.problems import ProblemError

FILE_POLICY = "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
FILE_HEADERS = {
    "Cache-Control": "private, max-age=300",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": FILE_POLICY,
    "Referrer-Policy": "no-referrer",
}


class Store(Protocol):
    def open_stream(self, key: str) -> IO[bytes]: ...


def store(request: Request) -> Store:
    found: Store = request.app.state.store
    return found


def _chunks(stream: IO[bytes]) -> Iterator[bytes]:
    with stream:
        while chunk := stream.read(CHUNK):
            yield chunk


def stream_file(request: Request, key: str, content_type: str, name: str) -> StreamingResponse:
    try:
        stream = store(request).open_stream(key)
    except FileNotFoundError:
        raise ProblemError(404, title="The file is not in storage", code="not_found") from None
    inline = content_type in INLINE_TYPES
    disposition = "inline" if inline else "attachment"
    headers = {**FILE_HEADERS, "Content-Disposition": f'{disposition}; filename="{name}"'}
    media_type = content_type if inline else "application/octet-stream"
    return StreamingResponse(_chunks(stream), media_type=media_type, headers=headers)


@dataclass(frozen=True)
class ZipEntry:
    name: str  # path inside the archive
    key: str
    sha256: str
    size: int
    kind: str
    profile: str | None
    captured_at: datetime


class _Sink(io.RawIOBase):
    """An unseekable stream zipfile writes into; the generator drains it between files."""

    def __init__(self) -> None:
        self.parts: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        chunk = bytes(data)
        self.parts.append(chunk)
        return len(chunk)

    def drain(self) -> Iterator[bytes]:
        parts, self.parts = self.parts, []
        yield from parts


def evidence_zip(
    request: Request, entries: Sequence[ZipEntry], manifest: dict[str, Any], name: str
) -> StreamingResponse:
    opened = store(request)

    def generate() -> Iterator[bytes]:
        sink = _Sink()
        with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for entry in entries:
                try:
                    stream = opened.open_stream(entry.key)
                except FileNotFoundError:
                    continue
                info = zipfile.ZipInfo(entry.name, date_time=entry.captured_at.timetuple()[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                with archive.open(info, "w", force_zip64=True) as target:
                    for chunk in _chunks(stream):
                        target.write(chunk)
                        yield from sink.drain()
                yield from sink.drain()
            document = json.dumps(manifest, indent=2, default=str).encode()
            archive.writestr("manifest.json", document)
        yield from sink.drain()

    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{name}"',
    }
    return StreamingResponse(generate(), media_type="application/zip", headers=headers)
