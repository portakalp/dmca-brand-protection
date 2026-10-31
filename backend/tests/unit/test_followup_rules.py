"""The follow-up rules (architecture.md §16: the 72-hour window, a check
in between resets it, a warning never counts as down, 30 days of monitoring)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalkan.domain.enums import CaseState, RecheckResult
from kalkan.followup import patterns
from kalkan.followup.rules import (
    BrowserPage,
    Fetch,
    LightPage,
    Move,
    Observation,
    Reason,
    advance,
    classify_browser,
    classify_http,
    final,
    needs_browser,
    reappeared,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 12, 8, 9, 0, tzinfo=UTC)
R = RecheckResult
UP = Observation(R.ONLINE, Reason.UP, 200)
DOWN = Observation(R.OFFLINE, Reason.GONE, 404)
WARN = Observation(R.WARNING, Reason.WARNING, 200)
UNKNOWN = Observation(R.UNKNOWN, Reason.STATUS, 403)


@pytest.mark.parametrize(
    ("fetch", "status", "text", "failures", "expected"),
    [
        (Fetch.NO_DNS, None, "", 0, (R.OFFLINE, Reason.NO_DNS)),
        (Fetch.OK, 404, "Not Found", 0, (R.OFFLINE, Reason.GONE)),
        (Fetch.OK, 410, "", 0, (R.OFFLINE, Reason.GONE)),
        (Fetch.OK, 451, "", 0, (R.OFFLINE, Reason.GONE)),
        (Fetch.OK, 200, "This Account has been suspended.", 0, (R.OFFLINE, Reason.SUSPENDED)),
        (Fetch.OK, 200, "Welcome to Mintera Bank, log in", 0, (R.ONLINE, Reason.UP)),
        (Fetch.OK, 302, "", 0, (R.ONLINE, Reason.UP)),
        (Fetch.OK, 403, "Forbidden", 0, (R.UNKNOWN, Reason.STATUS)),
        (Fetch.OK, 503, "", 0, (R.UNKNOWN, Reason.STATUS)),
        (Fetch.NO_CONNECTION, None, "", 0, (R.UNKNOWN, Reason.CONNECTION)),
        (Fetch.NO_CONNECTION, None, "", 1, (R.UNKNOWN, Reason.CONNECTION)),
        (Fetch.NO_CONNECTION, None, "", 2, (R.OFFLINE, Reason.CONNECTION)),
        (Fetch.ERROR, None, "", 2, (R.OFFLINE, Reason.CONNECTION)),
        (Fetch.BLOCKED, None, "", 5, (R.UNKNOWN, Reason.BLOCKED)),
        (
            Fetch.OK,
            200,
            "Warning: Suspected Phishing Site Ahead! Dismiss this warning",
            0,
            (R.WARNING, Reason.WARNING),
        ),
    ],
)
def test_the_http_check(
    fetch: Fetch, status: int | None, text: str, failures: int, expected: tuple[R, Reason]
) -> None:
    seen = classify_http(fetch, status=status, text=text, failures_before=failures)
    assert (seen.result, seen.reason) == expected


def test_a_warning_page_is_not_a_suspension_even_with_both_texts() -> None:
    text = "Suspected phishing site. This domain has been suspended."
    assert patterns.warning(text)
    assert classify_http(Fetch.OK, status=200, text=text).result is R.WARNING


def test_the_browser_wins_when_it_opens_a_page() -> None:
    # The geo-block scenario: HTTP 403 from our country, the page from the target market.
    pages = [
        BrowserPage("error"),
        BrowserPage("ok", 200, "Mintera Bank login", "Mintera Bank"),
    ]
    assert classify_browser(pages) == Observation(R.ONLINE, Reason.UP, 200)
    assert final(UNKNOWN, classify_browser(pages)).result is R.ONLINE
    assert classify_browser([BrowserPage("challenge")]) == Observation(R.ONLINE, Reason.CHALLENGE)
    assert classify_browser([BrowserPage("ok", 404, "Not Found")]) == Observation(
        R.OFFLINE, Reason.GONE, 404
    )
    # Nothing opened: the HTTP check stands.
    assert classify_browser([BrowserPage("error"), BrowserPage("error")]) is None
    assert final(DOWN, None) is DOWN


def test_which_checks_the_browser_confirms() -> None:
    assert needs_browser(CaseState.NOTIFIED, DOWN)
    assert needs_browser(CaseState.NOTIFIED, UNKNOWN)
    assert not needs_browser(CaseState.NOTIFIED, UP)
    assert not needs_browser(CaseState.NOTIFIED, WARN)
    assert needs_browser(CaseState.TAKEN_DOWN, UP)
    assert needs_browser(CaseState.TAKEN_DOWN, WARN)
    assert not needs_browser(CaseState.TAKEN_DOWN, DOWN)
    assert not needs_browser(CaseState.WATCHING, DOWN)


def _notified(
    observation: Observation,
    since: datetime | None,
    now: datetime,
    previous: R | None = None,
) -> tuple[Move, datetime | None, datetime | None]:
    step = advance(
        CaseState.NOTIFIED,
        observation,
        offline_since=since,
        monitor_until=None,
        previous=previous,
        now=now,
    )
    return step.move, step.offline_since, step.next_check_at


def test_a_site_up_is_checked_every_six_hours() -> None:
    assert _notified(UP, None, NOW) == (Move.NONE, None, NOW + timedelta(hours=6))


def test_seventy_two_hours_down_without_a_break_is_a_takedown() -> None:
    since = None
    at = NOW
    moves = []
    for _ in range(13):  # every six hours, 0 to 72 hours
        move, since, _ = _notified(DOWN, since, at)
        moves.append(move)
        at += timedelta(hours=6)
    assert since == NOW
    assert moves[:-1] == [Move.NONE] * 12
    assert moves[-1] is Move.TAKE_DOWN


def test_a_check_up_in_between_resets_the_window() -> None:
    _, since, _ = _notified(DOWN, None, NOW)
    step = advance(
        CaseState.NOTIFIED,
        UP,
        offline_since=since,
        monitor_until=None,
        previous=R.OFFLINE,
        now=NOW + timedelta(hours=48),
    )
    assert (step.offline_since, step.online_again) == (None, True)
    _, since, _ = _notified(DOWN, None, NOW + timedelta(hours=54))
    assert since == NOW + timedelta(hours=54)
    assert _notified(DOWN, since, NOW + timedelta(hours=78))[0] is Move.NONE
    assert _notified(DOWN, since, NOW + timedelta(hours=126))[0] is Move.TAKE_DOWN


def test_a_warning_keeps_the_site_up_and_is_reported_once() -> None:
    step = advance(
        CaseState.NOTIFIED, WARN, offline_since=None, monitor_until=None, previous=R.ONLINE, now=NOW
    )
    assert (step.move, step.offline_since, step.warning_active) == (Move.NONE, None, True)
    again = advance(
        CaseState.NOTIFIED,
        WARN,
        offline_since=None,
        monitor_until=None,
        previous=R.WARNING,
        now=NOW + timedelta(hours=96),
    )
    assert (again.move, again.warning_active) == (Move.NONE, False)


def test_an_unknown_check_neither_resets_nor_completes_the_window() -> None:
    since = NOW - timedelta(hours=80)
    assert _notified(UNKNOWN, since, NOW) == (Move.NONE, since, NOW + timedelta(hours=6))
    assert _notified(UNKNOWN, None, NOW)[1] is None


def test_thirty_days_of_monitoring_then_the_archive() -> None:
    until = NOW + timedelta(days=30)

    def taken_down(observation: Observation, now: datetime) -> Move:
        return advance(
            CaseState.TAKEN_DOWN,
            observation,
            offline_since=NOW - timedelta(hours=72),
            monitor_until=until,
            previous=R.OFFLINE,
            now=now,
        ).move

    assert taken_down(DOWN, NOW + timedelta(days=29)) is Move.NONE
    assert taken_down(UNKNOWN, NOW + timedelta(days=29)) is Move.NONE
    assert taken_down(DOWN, until) is Move.ARCHIVE
    assert taken_down(UP, NOW + timedelta(days=3)) is Move.REOPEN
    assert taken_down(WARN, NOW + timedelta(days=3)) is Move.REOPEN


def test_what_brings_a_watched_or_dismissed_case_back() -> None:
    brand = LightPage(brand_on_page=True, empty=False)
    assert reappeared(CaseState.WATCHING, brand) == "the brand appeared on the page"
    assert reappeared(CaseState.DISMISSED, brand) is None
    assert reappeared(CaseState.DISMISSED, LightPage(password_fields=1)) is not None
    assert reappeared(CaseState.DISMISSED, LightPage(card_fields=2)) is not None
    parked = LightPage(brand_on_page=True, parked=True, empty=False)
    assert reappeared(CaseState.WATCHING, parked) is None
    assert reappeared(CaseState.WATCHING, LightPage()) is None
