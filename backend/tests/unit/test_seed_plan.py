"""The demo plan without a database: counts, timing rules and determinism."""

from __future__ import annotations

import struct
import zlib
from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from kalkan.domain.enums import CaseState, Category, Channel
from kalkan.seed import design
from kalkan.seed.demo import (
    Clock,
    PlannedCase,
    Planner,
    plan_current_period,
    plan_previous_period,
)
from kalkan.seed.placeholders import html_page, json_document, png

TZ = ZoneInfo(design.TIMEZONE)
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=TZ).astimezone(UTC)


def _plan(now: datetime = NOW) -> tuple[Clock, list[PlannedCase], list[PlannedCase]]:
    clock = Clock.starting_at(now)
    planner = Planner(clock)
    return clock, plan_current_period(planner), plan_previous_period(planner)


def test_current_period_counts() -> None:
    clock, current, _ = _plan()
    states = Counter(case.state for case in current)
    assert len(current) == 128
    assert states[CaseState.APPROVED] == 2
    assert states[CaseState.NOTIFIED] == 6
    assert states[CaseState.TAKEN_DOWN] + states[CaseState.ARCHIVED] == 39
    assert states[CaseState.WATCHING] == 69
    assert states[CaseState.DISMISSED] == 12
    assert all(0 <= clock.day_of(case.first_seen) < design.PERIOD_DAYS for case in current)


def test_every_event_is_in_the_past_and_ordered() -> None:
    for now in (NOW, datetime(2026, 10, 8, 4, 0, tzinfo=TZ).astimezone(UTC)):
        clock, current, previous = _plan(now)
        for case in current + previous:
            assert case.first_seen <= clock.reference <= now
            for notice in case.notices:
                assert case.first_seen < notice.sent_at <= clock.reference
            if case.offline_since is not None:
                assert case.taken_down_at is not None
                assert case.notices[0].sent_at < case.offline_since <= case.taken_down_at
                assert case.taken_down_at <= clock.reference


def test_registrar_notices_follow_the_first_wave_by_48_hours() -> None:
    _, current, previous = _plan()
    for case in current + previous:
        registrar = [n for n in case.notices if n.channel is Channel.REGISTRAR]
        if registrar:
            assert registrar[0].sent_at - case.notices[0].sent_at == timedelta(hours=48)
            assert registrar[0].escalation_level == 1


def test_plan_is_deterministic() -> None:
    _, first, _ = _plan()
    _, second, _ = _plan()
    assert [(c.domain, c.state, c.first_seen) for c in first] == [
        (c.domain, c.state, c.first_seen) for c in second
    ]


def test_domains_are_unique_and_avoid_the_whitelist() -> None:
    _, current, previous = _plan()
    names = [case.domain for case in current + previous]
    assert len(names) == len(set(names))
    listed = {pattern for pattern, *_ in design.WHITELIST_RECENT} | {
        pattern for pattern, _ in design.WHITELIST_OLDER
    }
    assert not listed & set(names)


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


@pytest.mark.parametrize(("size", "width", "height"), [(1_200_000, 1280, 800), (None, 390, 844)])
def test_png_placeholders(size: int | None, width: int, height: int) -> None:
    data = png(width, height, (27, 18, 64), size=size)
    assert _png_size(data) == (width, height)
    if size is not None:
        assert len(data) == size
    # every chunk has a valid CRC
    offset = 8
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        offset += 12 + length


def test_text_placeholders_have_exact_sizes() -> None:
    assert len(html_page("t", "<p>x</p>", size=84_000)) == 84_000
    assert len(json_document({"log": {}}, size=2_300_000)) == 2_300_000
    with pytest.raises(ValueError, match="larger"):
        json_document({"x": "y" * 100}, size=10)


@pytest.mark.parametrize(
    "local_now",
    [
        datetime(2026, 3, 29, 3, 0, tzinfo=TZ),  # spring forward, the hour after 02:00 is skipped
        datetime(2026, 3, 29, 3, 30, tzinfo=TZ),
        datetime(2026, 10, 25, 2, 30, tzinfo=TZ),  # fall back, 02:00-03:00 happens twice
        datetime(2026, 10, 25, 4, 0, tzinfo=TZ),
        datetime(2026, 10, 8, 0, 20, tzinfo=TZ),
        datetime(2026, 10, 8, 23, 59, tzinfo=TZ),
    ],
)
def test_no_event_after_now_on_dst_days_and_at_night(local_now: datetime) -> None:
    now = local_now.astimezone(UTC)
    clock, current, previous = _plan(now)
    assert clock.reference <= now
    for case in current + previous:
        moments = [case.first_seen, *(n.sent_at for n in case.notices)]
        moments += [m for m in (case.offline_since, case.taken_down_at, case.last_action_at) if m]
        assert max(moments) <= now, case.domain


@pytest.mark.parametrize(
    "local_now",
    [
        datetime(2026, 10, 8, 4, 31, tzinfo=TZ),
        datetime(2026, 10, 8, 15, 0, tzinfo=TZ),
        datetime(2026, 3, 29, 5, 31, tzinfo=TZ),  # the spring DST night is an hour shorter
        datetime(2026, 3, 29, 22, 0, tzinfo=TZ),
        datetime(2026, 10, 25, 4, 31, tzinfo=TZ),
    ],
)
def test_daily_series_are_exact_from_half_past_four(local_now: datetime) -> None:
    now = local_now.astimezone(UTC)
    clock, current, _ = _plan(now)
    detected = Counter(clock.day_of(case.first_seen) for case in current)
    closed = Counter(
        clock.day_of(case.offline_since) for case in current if case.offline_since is not None
    )
    assert tuple(detected[day] for day in range(design.PERIOD_DAYS)) == design.DETECTED_PER_DAY
    assert tuple(closed[day] for day in range(design.PERIOD_DAYS)) == design.TAKEN_DOWN_PER_DAY


def test_phishing_sites_open_beyond_48_hours_were_escalated() -> None:
    _, current, previous = _plan()
    for case in current + previous:
        if case.offline_since is None or case.category is not Category.PHISHING_CLONE:
            continue
        if case.offline_since - case.notices[0].sent_at > timedelta(hours=48):
            assert any(n.channel is Channel.REGISTRAR for n in case.notices), case.domain
