"""The panel's metrics, pure (product.md §12): cohort, rounding, changes, series and channels."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from kalkan.domain.enums import CaseState, Channel
from kalkan.domain.metrics import (
    CaseFacts,
    PanelStatus,
    Period,
    by_channel,
    change,
    daily,
    distribution,
    kpis,
    live,
    panel_status,
    percent,
    round_half_up,
)

pytestmark = pytest.mark.unit

TZ = ZoneInfo("Europe/Warsaw")
T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def facts(state: CaseState, **values: object) -> CaseFacts:
    return CaseFacts(state=state, first_seen_at=values.pop("first_seen_at", T0), **values)  # type: ignore[arg-type]


def test_the_status_mapping_is_the_one_of_prod_md() -> None:
    assert panel_status(CaseState.DETECTED) is PanelStatus.REVIEWING
    assert panel_status(CaseState.IN_REVIEW) is PanelStatus.REVIEWING
    assert panel_status(CaseState.VERIFIED) is PanelStatus.ACTIVE
    assert panel_status(CaseState.APPROVED) is PanelStatus.ACTIVE  # a dry-run preview too
    assert panel_status(CaseState.NOTIFIED) is PanelStatus.NOTICE_SENT
    assert panel_status(CaseState.ARCHIVED) is PanelStatus.TAKEN_DOWN
    assert live([CaseState.APPROVED, CaseState.NOTIFIED, CaseState.TAKEN_DOWN]) == 2


def test_rounding_is_half_up() -> None:
    assert round_half_up(82.5) == 83
    assert round_half_up(2.5) == 3
    assert round_half_up(-21.5) == -22
    assert percent(39, 47) == 83
    assert percent(1, 0) is None
    assert change(128, 108) == 19
    assert change(47, 43) == 9
    assert change(31, 39.75) == -22
    assert change(3, 0) is None
    assert change(None, 4) is None


def test_kpis_count_the_cohort_and_measure_from_the_first_notice() -> None:
    cohort = [
        facts(
            CaseState.TAKEN_DOWN,
            verified_at=T0,
            first_notice_at=T0,
            offline_since=T0 + timedelta(hours=10),
        ),
        facts(
            CaseState.ARCHIVED,
            verified_at=T0,
            first_notice_at=T0,
            offline_since=T0 + timedelta(hours=30),
        ),
        facts(CaseState.NOTIFIED, verified_at=T0, first_notice_at=T0),
        facts(CaseState.WATCHING),
    ]
    result = kpis(cohort)
    assert (result.detected, result.verified, result.taken_down) == (4, 3, 2)
    assert result.takedown_rate == 67
    assert result.median_takedown_hours == 20
    assert distribution(cohort)[PanelStatus.TAKEN_DOWN] == 2
    assert sum(distribution(cohort).values()) == 4
    assert kpis([]).median_takedown_hours is None


def test_daily_series_use_the_customers_days() -> None:
    period = Period(date(2026, 10, 1), date(2026, 10, 3))
    late = datetime(2026, 10, 1, 22, 30, tzinfo=UTC)  # 00:30 on Oct 2 in Warsaw
    series = daily(
        [
            facts(CaseState.WATCHING, first_seen_at=late),
            facts(CaseState.TAKEN_DOWN, offline_since=late + timedelta(days=1)),
        ],
        period,
        TZ,
    )
    assert [(d.day.day, d.detected, d.taken_down) for d in series] == [
        (1, 1, 0),
        (2, 1, 0),
        (3, 0, 1),
    ]
    assert period.previous() == Period(date(2026, 9, 28), date(2026, 9, 30))


def test_takedowns_by_the_channel_of_the_last_notice_before_offline() -> None:
    def down(channel: Channel, hours: int) -> CaseFacts:
        return facts(
            CaseState.TAKEN_DOWN,
            offline_since=T0 + timedelta(hours=hours),
            closing_channel=channel,
            closing_sent_at=T0,
        )

    rows = by_channel(
        [
            down(Channel.HOSTING, 10),
            down(Channel.HOSTING, 20),
            down(Channel.REGISTRAR, 50),
            facts(CaseState.NOTIFIED, closing_channel=Channel.REGISTRAR, closing_sent_at=T0),
        ]
    )
    assert [(r.channel, r.cases, r.median_hours) for r in rows] == [
        (Channel.HOSTING, 2, 15),
        (Channel.REGISTRAR, 1, 50),
    ]
