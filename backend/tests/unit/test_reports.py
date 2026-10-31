"""The monthly report's months, CSV and the printer's address rule (architecture.md §21)."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from kalkan.api.v1.queries import ReportCase
from kalkan.domain.enums import CaseState, Category, Channel
from kalkan.domain.metrics import CaseFacts
from kalkan.reports.content import (
    CSV_COLUMNS,
    csv_text,
    has_ended,
    month_period,
    previous_month,
)
from kalkan.reports.printing import AppOrigin, PrintError

WARSAW = ZoneInfo("Europe/Warsaw")


def test_months() -> None:
    assert month_period("2026-02").end == date(2026, 2, 28)
    assert month_period("2028-02").end == date(2028, 2, 29)
    assert month_period("2026-12").start == date(2026, 12, 1)
    with pytest.raises(ValueError, match="YYYY-MM"):
        month_period("2026-1")
    first = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    assert previous_month(first, WARSAW) == "2026-10"
    assert previous_month(datetime(2026, 1, 1, 7, tzinfo=UTC), WARSAW) == "2025-12"
    # 23:30 UTC on 31 October is already November in Warsaw.
    assert has_ended("2026-10", datetime(2026, 10, 31, 23, 30, tzinfo=UTC), WARSAW)
    assert not has_ended("2026-10", datetime(2026, 10, 31, 22, 30, tzinfo=UTC), WARSAW)


def _row(number: str, domain: str, state: CaseState, **facts: object) -> ReportCase:
    return ReportCase(
        number=number,
        domain=domain,
        category=Category.PHISHING_CLONE,
        facts=CaseFacts(
            state=state,
            first_seen_at=datetime(2026, 10, 2, 8, tzinfo=UTC),
            **facts,  # type: ignore[arg-type]
        ),
    )


def test_the_csv_columns_follow_the_metric_definitions() -> None:
    down = _row(
        "KLK-0001",
        "mintera-login.com",
        CaseState.ARCHIVED,  # Taken down on the panel
        first_notice_at=datetime(2026, 10, 2, 9, tzinfo=UTC),
        offline_since=datetime(2026, 10, 3, 15, 30, tzinfo=UTC),
        closing_channel=Channel.REGISTRAR,
        closing_sent_at=datetime(2026, 10, 2, 12, tzinfo=UTC),
    )
    # Offline once, but back online and notified again: not taken down, no takedown fields.
    back = _row(
        "KLK-0002",
        "=HYPERLINK(1)",
        CaseState.NOTIFIED,
        first_notice_at=datetime(2026, 10, 2, 9, tzinfo=UTC),
        offline_since=datetime(2026, 10, 3, 15, 30, tzinfo=UTC),
        closing_channel=Channel.HOSTING,
    )
    rows = list(csv.reader(io.StringIO(csv_text([down, back], WARSAW))))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert rows[1] == [
        "KLK-0001",
        "mintera-login.com",
        "phishing_clone",
        "taken_down",
        "2026-10-02T10:00+02:00",
        "2026-10-02T11:00+02:00",
        "2026-10-03T17:30+02:00",
        "30.5",  # first notice to takedown, as the median (product.md §12)
        "registrar",  # the last notice before the takedown
    ]
    assert rows[2][1] == "'=HYPERLINK(1)"  # no spreadsheet formula (K150)
    assert rows[2][3] == "notice_sent"
    assert rows[2][6:] == ["", "", ""]


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://app.example.com/print/report/1", True),
        ("https://app.example.com/_next/static/chunks/a.js", True),
        ("https://APP.example.com:443/print", True),
        ("wss://app.example.com/socket", True),
        ("http://app.example.com/print", False),  # another scheme
        ("https://app.example.com:8443/print", False),  # another port
        ("https://app.example.com.evil.test/print", False),
        ("https://app.example.com@evil.test/print", False),  # userinfo trick
        ("https://evil.test/https://app.example.com/", False),
        ("https://sub.app.example.com/", False),
        ("data:text/html,hi", False),
        ("https://[::1/", False),  # unparsable
    ],
)
def test_the_printer_may_reach_the_app_only(url: str, allowed: bool) -> None:
    assert AppOrigin.parse("https://app.example.com/").allows(url) is allowed


def test_a_base_path_limits_the_printer_to_it() -> None:
    origin = AppOrigin.parse("http://127.0.0.1:3000/panel")
    assert origin.allows("http://127.0.0.1:3000/panel/print/report/1")
    assert not origin.allows("http://127.0.0.1:3000/panelx")
    assert not origin.allows("http://127.0.0.1:3000/api/v1/me")
    assert origin.url("/print/report/1") == "http://127.0.0.1:3000/panel/print/report/1"
    assert AppOrigin.parse("https://app.example.com").url("/x") == "https://app.example.com/x"


@pytest.mark.parametrize("base", ["ftp://app.example.com", "app.example.com", "https://u:p@a.test"])
def test_a_bad_base_url_is_refused(base: str) -> None:
    with pytest.raises(PrintError):
        AppOrigin.parse(base)


def test_only_the_app_host_resolves() -> None:
    args = AppOrigin.parse("https://app.example.com").launch_args()
    assert "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE app.example.com" in args
    assert "--dns-prefetch-disable" in args
