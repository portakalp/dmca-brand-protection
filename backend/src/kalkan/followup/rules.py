"""The follow-up rules (architecture.md §16, product.md §11 "Follow-up"), pure.

What one check saw (:class:`Observation`) and what it means for the case (:func:`advance`):

- Down: no DNS (NXDOMAIN or SERVFAIL), HTTP 404, 410 or 451, a suspension page of a registrar
  or a host, or the third connection failure in a row. Anything else that is not a page (one
  or two connection failures, an address the scanner must not reach, 403, 5xx) is ``unknown``.
- A warning page (Cloudflare's phishing interstitial, a browser's "deceptive site") is
  ``warning``: the site is up and the warning is shown separately (K19).
- A ``notified`` case whose HTTP check did not see the site up, and a ``taken_down`` case
  whose HTTP check saw it up, are confirmed by the proxied browser from the target market
  (:func:`needs_browser`); a page the browser opens wins over the HTTP check, so a site that
  blocks our country or cloaks is not taken for down. When the browser opened nothing, the
  HTTP check stands.
- The first down check sets ``offline_since``; an up or warning check clears it. A case that
  is down at a check 72 hours or more after ``offline_since`` is ``taken_down``. An
  ``unknown`` check neither clears nor completes the window (K142).
- A ``taken_down`` case is checked daily for 30 days (``monitor_until``), then archived; when it
  is up again it is reopened for a new round of notices.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from kalkan.domain.enums import CaseState, RecheckResult
from kalkan.followup import patterns

NOTIFIED_EVERY = timedelta(hours=6)
TAKEN_DOWN_EVERY = timedelta(days=1)
WATCHING_EVERY = timedelta(days=7)
DISMISSED_EVERY = timedelta(days=30)
TAKEDOWN_AFTER = timedelta(hours=72)
MONITOR_FOR = timedelta(days=30)
CONNECTION_FAILURES = 3  # in a row, this check included
GONE_STATUSES = frozenset({404, 410, 451})

R = RecheckResult


class Reason(StrEnum):
    UP = "up"
    WARNING = "warning_page"
    SUSPENDED = "suspension_page"
    GONE = "gone"  # 404, 410, 451
    NO_DNS = "no_dns"
    CONNECTION = "connection_failed"
    BLOCKED = "blocked_address"
    STATUS = "http_status"  # another status that is no page: 403, 5xx
    CHALLENGE = "bot_challenge"  # the browser met a bot wall: something answers


class Fetch(StrEnum):
    """How the HTTP request ended (``scanning.precheck.Outcome``'s values)."""

    OK = "ok"
    NO_DNS = "no_dns"
    NO_CONNECTION = "no_connection"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass(frozen=True)
class Observation:
    result: RecheckResult
    reason: Reason
    http_status: int | None = None


def read_page(status: int | None, text: str, title: str = "") -> Observation:
    """A page that answered, from either check."""
    if patterns.warning(text, title):
        return Observation(R.WARNING, Reason.WARNING, status)
    if patterns.suspended(text, title):
        return Observation(R.OFFLINE, Reason.SUSPENDED, status)
    if status in GONE_STATUSES:
        return Observation(R.OFFLINE, Reason.GONE, status)
    if status is not None and 200 <= status < 400:
        return Observation(R.ONLINE, Reason.UP, status)
    return Observation(R.UNKNOWN, Reason.STATUS, status)


def classify_http(
    fetch: Fetch,
    *,
    status: int | None = None,
    text: str = "",
    title: str = "",
    failures_before: int = 0,
) -> Observation:
    """``failures_before``: connection failures in a row right before this check."""
    if fetch is Fetch.OK:
        return read_page(status, text, title)
    if fetch is Fetch.NO_DNS:
        return Observation(R.OFFLINE, Reason.NO_DNS)
    if fetch is Fetch.BLOCKED:
        return Observation(R.UNKNOWN, Reason.BLOCKED)
    if failures_before + 1 >= CONNECTION_FAILURES:
        return Observation(R.OFFLINE, Reason.CONNECTION)
    return Observation(R.UNKNOWN, Reason.CONNECTION)


@dataclass(frozen=True)
class BrowserPage:
    """One browser profile's result: ``ok`` and ``challenge`` profiles carry a page."""

    outcome: str  # capture_contract.Outcome value
    http_status: int | None = None
    text: str = ""
    title: str = ""


def classify_browser(pages: Iterable[BrowserPage]) -> Observation | None:
    """The browser's view, or None when no profile opened a page.

    An up profile wins: a site that shows its page to one profile is up.
    """
    seen: list[Observation] = []
    for page in pages:
        if page.outcome == "challenge":
            seen.append(Observation(R.ONLINE, Reason.CHALLENGE, page.http_status))
        elif page.outcome == "ok":
            seen.append(read_page(page.http_status, page.text, page.title))
    for result in (R.ONLINE, R.WARNING, R.OFFLINE, R.UNKNOWN):
        for observation in seen:
            if observation.result is result:
                return observation
    return None


def needs_browser(state: CaseState, http: Observation) -> bool:
    if state is CaseState.NOTIFIED:
        return http.result in (R.OFFLINE, R.UNKNOWN)
    if state is CaseState.TAKEN_DOWN:
        return http.result in (R.ONLINE, R.WARNING)
    return False


def final(http: Observation, browser: Observation | None) -> Observation:
    return browser if browser is not None else http


class Move(StrEnum):
    NONE = "none"
    TAKE_DOWN = "take_down"
    REOPEN = "reopen"
    ARCHIVE = "archive"


@dataclass(frozen=True)
class Advance:
    move: Move
    offline_since: datetime | None
    next_check_at: datetime | None
    offline_detected: bool = False
    online_again: bool = False
    warning_active: bool = False


def _is_up(result: RecheckResult) -> bool:
    return result in (R.ONLINE, R.WARNING)


def advance(
    state: CaseState,
    observation: Observation,
    *,
    offline_since: datetime | None,
    monitor_until: datetime | None,
    previous: RecheckResult | None,
    now: datetime,
) -> Advance:
    """What a ``notified`` or ``taken_down`` case does after a check.

    ``previous`` is the result of the check before this one (for ``warning_active``).
    """
    result = observation.result
    warning = result is R.WARNING and previous is not R.WARNING
    if state is CaseState.TAKEN_DOWN:
        if _is_up(result):
            return Advance(
                Move.REOPEN, None, now + NOTIFIED_EVERY, online_again=True, warning_active=warning
            )
        if monitor_until is not None and now >= monitor_until:
            return Advance(Move.ARCHIVE, offline_since, None)
        return Advance(Move.NONE, offline_since, now + TAKEN_DOWN_EVERY)
    if state is not CaseState.NOTIFIED:
        raise ValueError(f"no follow-up rule for {state}")
    later = now + NOTIFIED_EVERY
    if _is_up(result):
        return Advance(
            Move.NONE,
            None,
            later,
            online_again=offline_since is not None,
            warning_active=warning,
        )
    if result is R.UNKNOWN:
        return Advance(Move.NONE, offline_since, later)
    if offline_since is None:
        return Advance(Move.NONE, now, later, offline_detected=True)
    if now - offline_since >= TAKEDOWN_AFTER:
        return Advance(Move.TAKE_DOWN, offline_since, now + TAKEN_DOWN_EVERY)
    return Advance(Move.NONE, offline_since, later)


@dataclass(frozen=True)
class LightPage:
    """What the light check of a ``watching`` or ``dismissed`` case read from the page."""

    password_fields: int = 0
    card_fields: int = 0
    brand_on_page: bool = False
    parked: bool = False
    empty: bool = True


def reappeared(state: CaseState, page: LightPage) -> str | None:
    """Why the case goes back to ``detected``, or None.

    A dismissed case comes back only for a credential or card form (product.md §11); a watched
    one also for content: the brand on a page that is neither parked nor empty.
    """
    if page.password_fields:
        return "a password field appeared"
    if page.card_fields:
        return "a card form appeared"
    if state is CaseState.WATCHING and page.brand_on_page and not (page.parked or page.empty):
        return "the brand appeared on the page"
    return None


def light_interval(state: CaseState) -> timedelta:
    return WATCHING_EVERY if state is CaseState.WATCHING else DISMISSED_EVERY


def next_check_after_takedown(now: datetime) -> tuple[datetime, datetime]:
    """``monitor_until`` and ``next_check_at`` of a case just taken down."""
    return now + MONITOR_FOR, now + TAKEN_DOWN_EVERY
