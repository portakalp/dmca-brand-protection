"""Images in a form Claude accepts (architecture.md §13).

The Messages and Files APIs take JPEG, PNG, GIF and WebP only. Other formats Pillow can read
(ICO, BMP) are converted to PNG; SVG and anything unreadable is left out. An image whose
longer side is above :data:`MAX_SIDE` is scaled down first: Claude scales it down anyway, and
the smaller image costs fewer input tokens.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

MAX_SIDE = 1568
MAX_BYTES = 5 * 1024 * 1024  # per image in a request
MAX_INPUT_BYTES = 20 * 1024 * 1024  # what we are willing to decode
ACCEPTED = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}


@dataclass(frozen=True)
class PreparedImage:
    media_type: str
    data: bytes


def prepare(data: bytes) -> PreparedImage | None:
    """``data`` ready to send, or None when it is not an image Claude can read."""
    if not data or len(data) > MAX_INPUT_BYTES:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = image.format or ""
            size = image.size
            if image_format in ACCEPTED and max(size) <= MAX_SIDE and len(data) <= MAX_BYTES:
                return PreparedImage(ACCEPTED[image_format], data)
            image.load()
            converted = image.convert("RGBA" if _has_alpha(image) else "RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
    converted.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    converted.save(out, format="PNG", optimize=True)
    if out.tell() > MAX_BYTES:
        out = io.BytesIO()
        converted.convert("RGB").save(out, format="JPEG", quality=85)
        return PreparedImage("image/jpeg", out.getvalue())
    return PreparedImage("image/png", out.getvalue())


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info
