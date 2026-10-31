"""What a monthly report holds (architecture.md §21, product.md §12).

A report covers one calendar month in the customer's time zone: the same cohort, metrics and
status mapping as the panel's overview for that month. The CSV lists the month's threats.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta, tzinfo

from kalkan.api.v1.queries import ReportCase
from kalkan.api.v1.schemas import csv_cell
from kalkan.domain.metrics import Period, panel_status, taken_down

PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
CSV_COLUMNS = (
    "number",
    "domain",
    "category",
    "status",
    "first_seen_at",
    "first_notice_at",
    "taken_down_at",
    "takedown_hours",
    "channel",
)


def month_period(value: str) -> Period:
    """``YYYY-MM`` as a :class:`Period` from its first to its last day."""
    if not PERIOD.match(value):
        raise ValueError(f"not a YYYY-MM month: {value!r}")
    year, month = (int(part) for part in value.split("-"))
    first = date(year, month, 1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return Period(first, last)


def month_of(day: date) -> str:
    return f"{day:%Y-%m}"


def previous_month(at: datetime, tz: tzinfo) -> str:
    """The month before the one ``at`` falls in, in the customer's time zone."""
    return month_of(at.astimezone(tz).date().replace(day=1) - timedelta(days=1))


def has_ended(period: str, at: datetime, tz: tzinfo) -> bool:
    return month_period(period).end < at.astimezone(tz).date()


def _time(value: datetime | None, tz: tzinfo) -> str:
    return value.astimezone(tz).isoformat(timespec="minutes") if value else ""


def takedown_hours(row: ReportCase) -> float | None:
    """First notice to takedown, as in the median (product.md §12); only for taken-down threats."""
    facts = row.facts
    if not taken_down(facts) or facts.offline_since is None or facts.first_notice_at is None:
        return None
    return round((facts.offline_since - facts.first_notice_at).total_seconds() / 3600, 1)


def csv_text(rows: Iterable[ReportCase], tz: tzinfo) -> str:
    """The month's threats; spreadsheet formulas are defused like every export (K150)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        facts = row.facts
        down = taken_down(facts)
        hours = takedown_hours(row)
        writer.writerow(
            [
                csv_cell(value)
                for value in (
                    row.number,
                    row.domain,
                    row.category.value if row.category else "",
                    panel_status(facts.state).value,
                    _time(facts.first_seen_at, tz),
                    _time(facts.first_notice_at, tz),
                    _time(facts.offline_since, tz) if down else "",
                    "" if hours is None else f"{hours:.1f}",
                    facts.closing_channel.value if down and facts.closing_channel else "",
                )
            ]
        )
    return out.getvalue()
