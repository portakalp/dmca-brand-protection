"""Browser profiles and target markets of the browser stage (architecture.md §10).

Two device profiles: desktop 1280x800 and mobile 390x844 (``is_mobile``, ``has_touch``). Both
send a Chrome User-Agent whose major version is the bundled Chromium's, so the engine, the
User-Agent and its client hints agree; mobile is Chrome on Android (cloaking kits key on
"Android"/"Mobile"), desktop Chrome on Windows. Nothing hides automation (no stealth, §24).

Locale, time zone and Accept-Language follow the country whose proxy the job actually uses,
so the exit address, the clock and the language agree (K93).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kalkan.domain.enums import Profile

# Chromium of Playwright 1.63 (the worker-browser image and `playwright install`); the browser
# stage reads the real version at launch, stage 1 (no browser) uses this one.
CHROMIUM_MAJOR = 153


@dataclass(frozen=True)
class Market:
    locale: str
    timezone_id: str
    accept_language: str


MARKETS: dict[str, Market] = {
    "PL": Market("pl-PL", "Europe/Warsaw", "pl-PL,pl;q=0.9,en;q=0.8"),
    "TR": Market("tr-TR", "Europe/Istanbul", "tr-TR,tr;q=0.9,en;q=0.8"),
    "RU": Market("ru-RU", "Europe/Moscow", "ru-RU,ru;q=0.9,en;q=0.8"),
    "KZ": Market("ru-KZ", "Asia/Almaty", "ru-KZ,ru;q=0.9,kk;q=0.8,en;q=0.7"),
    "DE": Market("de-DE", "Europe/Berlin", "de-DE,de;q=0.9,en;q=0.8"),
}
DEFAULT_MARKET = Market("en-US", "UTC", "en-US,en;q=0.9")


def market_for(country: str | None) -> Market:
    return MARKETS.get(country or "", DEFAULT_MARKET)


@dataclass(frozen=True)
class Device:
    width: int
    height: int
    device_scale_factor: int
    is_mobile: bool
    user_agent: str  # {major} is the Chromium major version


DEVICES: dict[Profile, Device] = {
    Profile.DESKTOP: Device(
        1280,
        800,
        1,
        False,
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
    ),
    # DPR 3 renders like a phone; screenshots are taken at CSS scale (390x844 PNG).
    Profile.MOBILE: Device(
        390,
        844,
        3,
        True,
        "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{major}.0.0.0 Mobile Safari/537.36",
    ),
}


def user_agent(profile: Profile, major: int = CHROMIUM_MAJOR) -> str:
    """The reduced Chrome User-Agent of ``profile`` (chromium.org UA reduction)."""
    return DEVICES[profile].user_agent.format(major=major)


def chromium_major(version: str) -> int:
    """``153`` from a browser version such as ``153.0.8010.12``."""
    head = version.split(".", 1)[0]
    return int(head) if head.isdigit() else CHROMIUM_MAJOR


def context_options(profile: Profile, market: Market, major: int) -> dict[str, Any]:
    """Device, locale and time zone options of ``Browser.new_context`` for one profile."""
    device = DEVICES[profile]
    size = {"width": device.width, "height": device.height}
    return {
        "viewport": size,
        "screen": size,
        "device_scale_factor": device.device_scale_factor,
        "is_mobile": device.is_mobile,
        "has_touch": device.is_mobile,
        "user_agent": user_agent(profile, major),
        "locale": market.locale,
        "timezone_id": market.timezone_id,
        "extra_http_headers": {"Accept-Language": market.accept_language},
    }
