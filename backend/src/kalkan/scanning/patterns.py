"""Parking and challenge patterns from ``parking.yaml`` (architecture.md §10)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

PATTERNS_FILE = Path(__file__).with_name("parking.yaml")
# A page this short is a placeholder or a wall, not a site with a footer or an FAQ.
SHORT_PAGE_CHARS = 1_500
WALL_TEXT_CHARS = 500
_RAW_CHARS = 200_000


@dataclass(frozen=True)
class Patterns:
    parking_nameservers: tuple[str, ...]
    parking_texts: tuple[str, ...]
    parking_texts_short_pages: tuple[str, ...]
    challenge_texts: tuple[str, ...]
    challenge_headers: tuple[tuple[str, str], ...]
    captcha_widgets: tuple[str, ...]


@cache
def patterns() -> Patterns:
    data = yaml.safe_load(PATTERNS_FILE.read_text(encoding="utf-8"))
    return Patterns(
        parking_nameservers=tuple(name.lower() for name in data["parking_nameservers"]),
        parking_texts=tuple(text.lower() for text in data["parking_texts"]),
        parking_texts_short_pages=tuple(text.lower() for text in data["parking_texts_short_pages"]),
        challenge_texts=tuple(text.lower() for text in data["challenge_texts"]),
        challenge_headers=tuple(
            (str(key).lower(), str(value).lower())
            for key, value in data["challenge_headers"].items()
        ),
        captcha_widgets=tuple(text.lower() for text in data["captcha_widgets"]),
    )


def parked_by_nameserver(nameservers: Iterable[str]) -> bool:
    known = patterns().parking_nameservers
    for name in nameservers:
        host = name.rstrip(".").lower()
        if any(host == suffix or host.endswith("." + suffix) for suffix in known):
            return True
    return False


def parked_by_text(text: str, title: str = "") -> bool:
    lowered, heading = text.lower(), title.lower()
    known = patterns()
    if any(pattern in lowered or pattern in heading for pattern in known.parking_texts):
        return True
    short = len(text) < SHORT_PAGE_CHARS
    return any(
        pattern in heading or (short and pattern in lowered)
        for pattern in known.parking_texts_short_pages
    )


def challenge(raw_html: str, headers: Mapping[str, str]) -> bool:
    """An interstitial bot challenge (Cloudflare, DDoS-Guard) in the page or the headers."""
    lowered = raw_html[:_RAW_CHARS].lower()
    if any(pattern in lowered for pattern in patterns().challenge_texts):
        return True
    return any(headers.get(key, "").lower() == value for key, value in patterns().challenge_headers)


def captcha_wall(raw_html: str, visible_text: str, password_fields: int) -> bool:
    """A captcha widget standing in front of the site: little else on the page."""
    if password_fields or len(visible_text) >= WALL_TEXT_CHARS:
        return False
    lowered = raw_html[:_RAW_CHARS].lower()
    return any(pattern in lowered for pattern in patterns().captcha_widgets)
