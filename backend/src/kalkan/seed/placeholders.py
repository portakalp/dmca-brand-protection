"""Placeholder evidence files for the demo workspace (no real captures).

Sizes can be fixed so the evidence package matches the design (e.g. ``desktop.png`` 1.2 MB):
PNG files are padded with a private ancillary chunk, which image readers ignore.
"""

from __future__ import annotations

import json
import struct
import zlib
from typing import Any

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Ancillary, private, reserved-bit clear, safe to copy (PNG chunk naming rules).
_PADDING_CHUNK = b"kaLk"
_CHUNK_OVERHEAD = 12


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def png(width: int, height: int, rgb: tuple[int, int, int], *, size: int | None = None) -> bytes:
    """A solid-colour RGB PNG with a darker header band; padded to ``size`` bytes if given."""
    band = tuple(max(0, channel - 40) for channel in rgb)
    band_height = max(1, height // 12)
    header_row = b"\x00" + bytes(band) * width
    body_row = b"\x00" + bytes(rgb) * width
    raw = header_row * band_height + body_row * (height - band_height)
    image = (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
    )
    end = _chunk(b"IEND", b"")
    if size is None:
        return image + end
    padding = size - len(image) - len(end) - _CHUNK_OVERHEAD
    if padding < 0:
        raise ValueError(f"a {width}x{height} PNG does not fit in {size} bytes")
    return image + _chunk(_PADDING_CHUNK, b"\x00" * padding) + end


def html_page(title: str, body: str, *, size: int) -> bytes:
    page = (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f"<title>{title}</title></head><body>{body}</body></html>\n"
    )
    return _pad_text(page, size, "<!-- {} -->\n")


def json_document(document: dict[str, Any], *, size: int | None = None) -> bytes:
    if size is None:
        return json.dumps(document, indent=2).encode()
    # Spaces inside a JSON string add exactly one byte each.
    filler = size - len(json.dumps(dict(document, _padding=""), indent=2).encode())
    if filler < 0:
        raise ValueError("document is larger than the requested size")
    return json.dumps(dict(document, _padding=" " * filler), indent=2).encode()


def _pad_text(text: str, size: int, wrapper: str) -> bytes:
    data = text.encode()
    overhead = len(wrapper.format("").encode())
    filler = size - len(data) - overhead
    if filler < 0:
        raise ValueError("text is larger than the requested size")
    return data + wrapper.format(" " * filler).encode()
