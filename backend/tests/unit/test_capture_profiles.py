from __future__ import annotations

import pytest

from kalkan.domain.enums import Profile
from kalkan.scanning.profiles import (
    CHROMIUM_MAJOR,
    DEFAULT_MARKET,
    chromium_major,
    context_options,
    market_for,
    user_agent,
)


@pytest.mark.parametrize(
    ("country", "locale", "timezone"),
    [
        ("PL", "pl-PL", "Europe/Warsaw"),
        ("TR", "tr-TR", "Europe/Istanbul"),
        ("RU", "ru-RU", "Europe/Moscow"),
        ("KZ", "ru-KZ", "Asia/Almaty"),
        ("DE", "de-DE", "Europe/Berlin"),
    ],
)
def test_markets(country: str, locale: str, timezone: str) -> None:
    market = market_for(country)
    assert (market.locale, market.timezone_id) == (locale, timezone)
    assert market.accept_language.startswith(locale)


@pytest.mark.parametrize("country", [None, "", "US", "XX"])
def test_unknown_market_is_english_and_utc(country: str | None) -> None:
    assert market_for(country) == DEFAULT_MARKET
    assert (DEFAULT_MARKET.locale, DEFAULT_MARKET.timezone_id) == ("en-US", "UTC")


def test_desktop_options() -> None:
    options = context_options(Profile.DESKTOP, market_for("TR"), 153)
    assert options["viewport"] == {"width": 1280, "height": 800}
    assert options["device_scale_factor"] == 1
    assert options["is_mobile"] is False
    assert options["has_touch"] is False
    assert options["locale"] == "tr-TR"
    assert options["timezone_id"] == "Europe/Istanbul"
    assert options["extra_http_headers"] == {"Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"}
    assert "Windows NT 10.0" in options["user_agent"]
    assert "Chrome/153.0.0.0" in options["user_agent"]
    assert "Headless" not in options["user_agent"]


def test_mobile_options() -> None:
    options = context_options(Profile.MOBILE, market_for("PL"), 153)
    assert options["viewport"] == {"width": 390, "height": 844}
    assert options["screen"] == {"width": 390, "height": 844}
    assert options["device_scale_factor"] == 3
    assert options["is_mobile"] is True
    assert options["has_touch"] is True
    # The fixture's cloaking rule (and real kits) look for these words.
    assert "Android" in options["user_agent"]
    assert "Mobile" in options["user_agent"]


def test_user_agent_major_follows_the_browser() -> None:
    assert chromium_major("153.0.8010.12") == 153
    assert chromium_major("garbage") == CHROMIUM_MAJOR
    assert "Chrome/160.0.0.0" in user_agent(Profile.DESKTOP, 160)
    assert f"Chrome/{CHROMIUM_MAJOR}.0.0.0" in user_agent(Profile.MOBILE)
