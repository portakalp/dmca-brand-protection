"""Perceptual hashes and visual similarity (architecture.md §12).

pHash and dHash are 64-bit ``imagehash`` hashes stored as 16 hex digits. ``visual_similarity``
is ``1 - d/64`` for the smallest Hamming distance ``d`` between a candidate screenshot's pHash
and the official references of the same profile, the highest value across profiles.

Screenshots are prepared before hashing (:func:`prepare_screenshot`; K112): the top of the
page, at most as tall as it is wide, is kept (the brand's header, logo and banner; a tall mobile
page squeezed into a square hides them), blurred a little so text and antialiasing do not flip
bits, and histogram-equalised, because on dark, flat backgrounds most DCT coefficients sit next
to the median. On an earlier, dark set of fixture pages plain pHash put the clone 16 bits from
the official page and an unrelated page 20; prepared, 6 and 28. Icons and logos are hashed as
they are.

Screenshots come from the untrusted browser and favicons from candidate sites, so decoding is
capped: a file over :data:`MAX_PIXELS` pixels (a decompression bomb) or one Pillow cannot read
gives no hash, never an error.
"""

from __future__ import annotations

import io
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import NamedTuple

import imagehash
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

from kalkan.domain.enums import Profile

HASH_BITS = 64
# A 1280 x 10,000 px full-page screenshot is 12.8 MP; viewport shots are far smaller.
MAX_PIXELS = 40_000_000
VISUAL_SIMILAR = 0.85  # §12 starting threshold, mirrored by the decision settings (§14)
FAVICON_SIMILAR = 6
# Gaussian blur radius as a share of the width: 6 px on a 1280 px desktop shot, 1 px on mobile.
BLUR_SHARE = 200


class Hashes(NamedTuple):
    phash: str
    dhash: str


def prepare_screenshot(image: Image.Image) -> Image.Image:
    gray = image.convert("L")
    top = gray.crop((0, 0, gray.width, min(gray.height, gray.width)))
    blurred = top.filter(ImageFilter.GaussianBlur(max(1, top.width // BLUR_SHARE)))
    return ImageOps.equalize(blurred)


def image_hashes(data: bytes, *, screenshot: bool = False) -> Hashes | None:
    """pHash and dHash of an image file, or None when it cannot be read safely.

    ``screenshot`` prepares a page screenshot first (:func:`prepare_screenshot`).
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                if width * height > MAX_PIXELS or width == 0 or height == 0:
                    return None
                image.load()
                # Transparent icons: flatten on white so the shape, not the alpha, is hashed.
                if image.mode in ("RGBA", "LA", "P"):
                    rgba = image.convert("RGBA")
                    flat = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    flat.alpha_composite(rgba)
                    picture = flat.convert("RGB")
                else:
                    picture = image.convert("RGB")
                if screenshot:
                    picture = prepare_screenshot(picture)
                return Hashes(str(imagehash.phash(picture)), str(imagehash.dhash(picture)))
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
        SyntaxError,  # Pillow raises it for some malformed files
    ):
        return None


def distance(first: str, second: str) -> int | None:
    """Hamming distance of two stored hashes; None when either is malformed."""
    try:
        return int(imagehash.hex_to_hash(first) - imagehash.hex_to_hash(second))
    except (ValueError, TypeError):
        return None


def similarity(hamming: int) -> float:
    return round(1 - hamming / HASH_BITS, 4)


@dataclass(frozen=True)
class Reference:
    asset_id: str
    phash: str
    dhash: str | None = None


@dataclass(frozen=True)
class Nearest:
    """The closest reference to one hash."""

    distance: int
    asset_id: str
    dhash_distance: int | None


def nearest(phash: str, dhash: str | None, references: Iterable[Reference]) -> Nearest | None:
    """The reference with the smallest pHash distance (ties: the first asset id in order)."""
    best: Nearest | None = None
    for reference in sorted(references, key=lambda item: item.asset_id):
        found = distance(phash, reference.phash)
        if found is None:
            continue
        if best is None or found < best.distance:
            second = (
                distance(dhash, reference.dhash) if dhash is not None and reference.dhash else None
            )
            best = Nearest(found, reference.asset_id, second)
    return best


@dataclass(frozen=True)
class VisualResult:
    similarity: float | None
    distances: dict[Profile, Nearest | None]


def visual_similarity(
    candidates: Mapping[Profile, Hashes], references: Mapping[Profile, list[Reference]]
) -> VisualResult:
    """Per profile the nearest reference of the same profile; the best similarity overall."""
    distances: dict[Profile, Nearest | None] = {}
    for profile, hashes in candidates.items():
        distances[profile] = nearest(hashes.phash, hashes.dhash, references.get(profile, []))
    known = [found.distance for found in distances.values() if found is not None]
    return VisualResult(similarity(min(known)) if known else None, distances)
