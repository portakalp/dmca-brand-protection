"""Uploaded files of the onboarding screens: type checks by content, size limits, names."""

from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import UploadFile

MAX_ASSET_BYTES = 5 * 1024 * 1024
MAX_LOA_BYTES = 15 * 1024 * 1024
MAX_CSV_BYTES = 2 * 1024 * 1024
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class UploadError(ValueError):
    pass


@dataclass(frozen=True)
class Upload:
    data: bytes
    content_type: str
    extension: str
    filename: str


def _sniff(data: bytes) -> tuple[str, str] | None:
    """``(content type, extension)`` from the first bytes; the browser's claim is ignored."""
    head = data[:512]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", "webp"
    if head.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon", "ico"
    if head.startswith(b"%PDF-"):
        return "application/pdf", "pdf"
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text.startswith((b"<svg", b"<?xml")) and b"<svg" in data[:4096].lower():
        return "image/svg+xml", "svg"
    return None


def safe_name(filename: str | None) -> str:
    name = _UNSAFE.sub("-", (filename or "file").rsplit("/", 1)[-1]).strip(".-")
    return name[:80] or "file"


def read_upload(file: UploadFile, *, max_bytes: int, allowed: frozenset[str]) -> Upload:
    data = file.file.read(max_bytes + 1)
    if not data:
        raise UploadError("the file is empty")
    if len(data) > max_bytes:
        raise UploadError(f"the file is larger than {max_bytes // (1024 * 1024)} MB")
    found = _sniff(data)
    if found is None or found[0] not in allowed:
        kinds = ", ".join(sorted(kind.split("/")[1] for kind in allowed))
        raise UploadError(f"unsupported file type; use {kinds}")
    return Upload(data, found[0], found[1], safe_name(file.filename))


ASSET_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/x-icon", "image/svg+xml"}
)
LOA_TYPES = frozenset({"application/pdf", "image/png", "image/jpeg"})
