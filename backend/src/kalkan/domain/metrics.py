"""The panel's numbers (product.md §12), pure.

Period metrics are computed over a cohort: the cases first seen in the period (in the
customer's time zone, both ends included), whitelisted ones left out. So the status
distribution always adds up to Detected.

- Detected: cases in the cohort. Verified threats: those verified by now. Taken down: those
  taken down or archived now. Takedown rate: taken down / verified.
- Median takedown time: over the taken-down cases, from the first notice to the start of the
  72-hour window (``offline_since``).
- Change: against the previous period of the same length; the median's change is negative when
  takedowns got faster.
- Daily series: detections by first-seen day, takedowns by the day the site went down.
- By channel: the channel of the last notice sent before the site went down, and the time from
  that notice to it.
- Live threats: Active or Notice sent now, whatever the period.

Percentages round to the nearest integer, halves up. The panel status of a system state is
:func:`panel_status`, the one mapping of product.md §12.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from statistics import median
from zoneinfo import ZoneInfo

from kalkan.domain.enums import CaseState, Channel

S = CaseState


class PanelStatus(StrEnum):
    REVIEWING = "reviewing"
    ACTIVE = "active"
    NOTICE_SENT = "notice_sent"
    TAKEN_DOWN = "taken_down"
    WATCHING = "watching"
    DISMISSED = "dismissed"


PANEL_STATUS: dict[CaseState, PanelStatus] = {
    S.DETECTED: PanelStatus.REVIEWING,
    S.IN_REVIEW: PanelStatus.REVIEWING,
    S.VERIFIED: PanelStatus.ACTIVE,
    S.APPROVED: PanelStatus.ACTIVE,
    S.NOTIFIED: PanelStatus.NOTICE_SENT,
    S.TAKEN_DOWN: PanelStatus.TAKEN_DOWN,
    S.ARCHIVED: PanelStatus.TAKEN_DOWN,
    S.WATCHING: PanelStatus.WATCHING,
    S.DISMISSED: PanelStatus.DISMISSED,
}
LIVE = frozenset({PanelStatus.ACTIVE, PanelStatus.NOTICE_SENT})


def panel_status(state: CaseState) -> PanelStatus:
    return PANEL_STATUS[state]


def states_for(status: PanelStatus) -> list[CaseState]:
    return [state for state, shown in PANEL_STATUS.items() if shown is status]


def round_half_up(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def percent(part: int, whole: int) -> int | None:
    return round_half_up(part / whole * 100) if whole else None


def change(current: float | None, previous: float | None) -> int | None:
    """Percent change against the previous period; None without a base."""
    if current is None or not previous:
        return None
    return round_half_up((current - previous) / previous * 100)


@dataclass(frozen=True)
class CaseFacts:
    """What the metrics need of one case; ``closing_*`` is the last real notice sent before
    ``offline_since``."""

    state: CaseState
    first_seen_at: datetime
    verified_at: datetime | None = None
    first_notice_at: datetime | None = None
    offline_since: datetime | None = None
    closing_channel: Channel | None = None
    closing_sent_at: datetime | None = None


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # included

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous(self) -> Period:
        return Period(self.start - timedelta(days=self.days), self.start - timedelta(days=1))

    def bounds(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
        """Its first instant and the first instant after it, in UTC."""
        first = datetime.combine(self.start, datetime.min.time(), tzinfo=tz)
        after = datetime.combine(self.end + timedelta(days=1), datetime.min.time(), tzinfo=tz)
        return first, after


def default_period(today: date, days: int = 30) -> Period:
    return Period(today - timedelta(days=days - 1), today)


def _hours(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 3600


def taken_down(case: CaseFacts) -> bool:
    return panel_status(case.state) is PanelStatus.TAKEN_DOWN


@dataclass(frozen=True)
class Kpis:
    detected: int
    verified: int
    taken_down: int
    takedown_rate: int | None
    median_takedown_hours: float | None


def kpis(cohort: Sequence[CaseFacts]) -> Kpis:
    verified = sum(1 for case in cohort if case.verified_at is not None)
    down = [case for case in cohort if taken_down(case)]
    durations = [
        _hours(case.offline_since, case.first_notice_at)
        for case in down
        if case.offline_since is not None and case.first_notice_at is not None
    ]
    return Kpis(
        detected=len(cohort),
        verified=verified,
        taken_down=len(down),
        takedown_rate=percent(len(down), verified),
        median_takedown_hours=round(median(durations), 2) if durations else None,
    )


@dataclass(frozen=True)
class Changes:
    detected: int | None
    verified: int | None
    taken_down: int | None
    median_takedown_hours: int | None  # negative: faster


def changes(current: Kpis, previous: Kpis) -> Changes:
    return Changes(
        detected=change(current.detected, previous.detected),
        verified=change(current.verified, previous.verified),
        taken_down=change(current.taken_down, previous.taken_down),
        median_takedown_hours=change(current.median_takedown_hours, previous.median_takedown_hours),
    )


@dataclass(frozen=True)
class Day:
    day: date
    detected: int
    taken_down: int


def _local(at: datetime, tz: ZoneInfo) -> date:
    return at.astimezone(tz).date()


def daily(cohort: Iterable[CaseFacts], period: Period, tz: ZoneInfo) -> list[Day]:
    detected: Counter[date] = Counter()
    down: Counter[date] = Counter()
    for case in cohort:
        detected[_local(case.first_seen_at, tz)] += 1
        if taken_down(case) and case.offline_since is not None:
            down[_local(case.offline_since, tz)] += 1
    return [
        Day(day, detected[day], down[day])
        for day in (period.start + timedelta(days=n) for n in range(period.days))
    ]


def distribution(cohort: Iterable[CaseFacts]) -> dict[PanelStatus, int]:
    counts = Counter(panel_status(case.state) for case in cohort)
    return {status: counts[status] for status in PanelStatus}


@dataclass(frozen=True)
class ChannelTakedowns:
    channel: Channel
    cases: int
    median_hours: float | None


def by_channel(cohort: Iterable[CaseFacts]) -> list[ChannelTakedowns]:
    hours: dict[Channel, list[float]] = defaultdict(list)
    for case in cohort:
        if not taken_down(case) or case.closing_channel is None:
            continue
        if case.closing_sent_at is not None and case.offline_since is not None:
            hours[case.closing_channel].append(_hours(case.offline_since, case.closing_sent_at))
    rows = [
        ChannelTakedowns(channel, len(values), round(median(values), 2) if values else None)
        for channel, values in hours.items()
    ]
    return sorted(rows, key=lambda row: (-row.cases, row.channel.value))


def live(states: Iterable[CaseState]) -> int:
    return sum(1 for state in states if panel_status(state) in LIVE)


@dataclass(frozen=True)
class Overview:
    period: Period
    previous: Period
    kpis: Kpis
    previous_kpis: Kpis
    changes: Changes
    daily: list[Day]
    distribution: dict[PanelStatus, int]
    by_channel: list[ChannelTakedowns]
    live: int


def overview(
    current: Sequence[CaseFacts],
    earlier: Sequence[CaseFacts],
    *,
    period: Period,
    tz: ZoneInfo,
    live_count: int,
) -> Overview:
    now_kpis, then_kpis = kpis(current), kpis(earlier)
    return Overview(
        period=period,
        previous=period.previous(),
        kpis=now_kpis,
        previous_kpis=then_kpis,
        changes=changes(now_kpis, then_kpis),
        daily=daily(current, period, tz),
        distribution=distribution(current),
        by_channel=by_channel(current),
        live=live_count,
    )
