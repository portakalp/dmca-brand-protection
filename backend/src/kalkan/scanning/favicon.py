"""Favicon hash in Shodan's format (architecture.md §10, §12).

MurmurHash3 (x86, 32 bit, signed) of the base64 encoding with a newline after every 76
characters and at the end, which is what ``base64.encodebytes`` produces. Two sites with
the same favicon file get the same hash, so a clone that copies the brand's icon matches.
"""

from __future__ import annotations

import base64
import re

import mmh3

MAX_FAVICON_BYTES = 200 * 1024


def favicon_hash(data: bytes) -> int:
    return int(mmh3.hash(base64.encodebytes(data)))


_MAGIC = (
    (b"\x00\x00\x01\x00", ("ico", "image/x-icon")),
    (b"\x89PNG", ("png", "image/png")),
    (b"GIF8", ("gif", "image/gif")),
    (b"\xff\xd8\xff", ("jpg", "image/jpeg")),
    (b"BM", ("bmp", "image/bmp")),
)
_SVG_PREFIX = re.compile(
    rb"^(?:\xef\xbb\xbf)?\s*(?:<\?xml[^>]*>\s*|<!--.*?-->\s*|<!doctype[^>]*>\s*)*<svg[\s>]",
    re.IGNORECASE | re.DOTALL,
)


def image_type(data: bytes) -> tuple[str, str] | None:
    """``(extension, content type)`` of an icon file, or None when it is not one (e.g. an
    HTML page served at the icon's address)."""
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("webp", "image/webp")
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis", b"heic", b"mif1"):
        return ("avif", "image/avif")
    if _SVG_PREFIX.match(data[:2048]):
        return ("svg", "image/svg+xml")
    return None
