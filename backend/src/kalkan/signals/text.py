"""Text, redirect and domain signals (architecture.md §12).

- ``text_similarity``: rapidfuzz ``token_set_ratio`` (0-100) between a candidate page's text
  and the official reference text of the same profile (the other profile's when that one is
  missing); the highest value across profiles.
- ``brand_in_title``, ``brand_in_text``: a brand keyword on the page, the site's own host names
  removed first (as the pre-check does).
- ``redirects_to_official``: the browser ended on an official domain.
- ``cloaking_suspected``: the desktop and mobile pages share few words (token Jaccard below
  :data:`CLOAKING_MAX_OVERLAP`) and the brand shows on only one of them.
- ``domain_age_days``: the registration age when the case was detected; ``webrisk_listed``:
  Google Web Risk lists the domain. Both are None when the source has no answer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rapidfuzz import fuzz, utils

from kalkan.domain.enums import Profile
from kalkan.scanning.precheck import contains_brand, host_of, is_official, without_hosts

CLOAKING_MAX_OVERLAP = 0.5
_TOKEN = re.compile(r"\w+", re.UNICODE)


def tokens(text: str) -> set[str]:
    return {token for token in _TOKEN.findall(text.casefold()) if len(token) > 1}


def text_ratio(first: str, second: str) -> float | None:
    """``token_set_ratio`` of two texts, None when either has no words."""
    if not tokens(first) or not tokens(second):
        return None
    return round(fuzz.token_set_ratio(first, second, processor=utils.default_process), 1)


@dataclass(frozen=True)
class TextMatch:
    ratio: float
    reference_asset_id: str


def text_similarity(
    candidates: Mapping[Profile, str], references: Mapping[Profile, Sequence[tuple[str, str]]]
) -> tuple[float | None, dict[Profile, TextMatch | None]]:
    """``references`` maps a profile to ``(asset id, text)`` pairs, newest first."""
    per_profile: dict[Profile, TextMatch | None] = {}
    for profile, text in candidates.items():
        pool = list(references.get(profile, ())) or [
            item for other, items in references.items() if other != profile for item in items
        ]
        best: TextMatch | None = None
        for asset_id, reference in pool[:1]:  # the newest reference of the profile
            ratio = text_ratio(text, reference)
            if ratio is not None:
                best = TextMatch(ratio, asset_id)
        per_profile[profile] = best
    known = [match.ratio for match in per_profile.values() if match is not None]
    return (max(known) if known else None), per_profile


def brand_on_page(
    title: str | None, text: str | None, keywords: Iterable[str], own_hosts: Iterable[str]
) -> tuple[bool, bool]:
    """``(brand_in_title, brand_in_text)`` with the site's own host names removed first."""
    hosts = list(own_hosts)
    words = list(keywords)
    return (
        contains_brand(without_hosts(title or "", hosts), words),
        contains_brand(without_hosts(text or "", hosts), words),
    )


def redirects_to_official(final_url: str | None, officials: Iterable[str]) -> bool:
    return is_official(host_of(final_url), officials)


@dataclass(frozen=True)
class PageView:
    """One profile's page as far as cloaking is concerned."""

    text: str
    brand: bool  # brand in the title or text


def cloaking(pages: Mapping[Profile, PageView]) -> tuple[bool | None, dict[str, Any]]:
    """None unless both profiles were captured."""
    desktop, mobile = pages.get(Profile.DESKTOP), pages.get(Profile.MOBILE)
    if desktop is None or mobile is None:
        return None, {}
    first, second = tokens(desktop.text), tokens(mobile.text)
    union = first | second
    overlap = round(len(first & second) / len(union), 3) if union else 1.0
    suspected = overlap < CLOAKING_MAX_OVERLAP and desktop.brand != mobile.brand
    return suspected, {
        "overlap": overlap,
        "brand_desktop": desktop.brand,
        "brand_mobile": mobile.brand,
    }


def domain_age_days(registered_at: datetime | None, detected_at: datetime) -> int | None:
    if registered_at is None:
        return None
    return max((detected_at - registered_at).days, 0)


def webrisk_listed(webrisk: Mapping[str, Any] | None) -> bool | None:
    if webrisk is None:
        return None
    return bool(webrisk.get("threat_types"))
