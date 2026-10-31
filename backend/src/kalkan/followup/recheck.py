"""``recheck(case_id)``: the follow-up checks of a case (architecture.md §16).

``notified`` and ``taken_down`` cases: one HTTP request through ``net/safe_http.py`` without a
proxy, read by ``followup.rules``. When the HTTP check contradicts what the case believes (a
notified site that seems down, a taken-down site that seems up), the browser stage
confirms it from the target market through the proxy: a ``recheck`` capture job, whose
``persist_capture`` stores the screenshots as new evidence and calls :func:`apply_browser`. When a
job for the case is already waiting, the browser did not answer within a whole interval and the
HTTP check is applied on its own (K142).

A site that is up and on Google's Web Risk lists is ``warning``, like a warning page (K19).

``watching`` and ``dismissed`` cases get a light HTTP check every 7 and 30 days: a password or
card form (and, for a watched case, the brand on a real page) sends the case back to
``detected`` and to the pre-check. A whitelisted case is never checked again.

Every check writes a ``rechecks`` row. The follow-up clock is ``cases.next_check_at``; the
scheduler leases it before sending the actor, and this module sets the next one.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalkan.db.models import Brand, Case, CaseEvent, Customer, Domain, DomainSighting, Recheck
from kalkan.db.outbox import HTTP_PRECHECK, PLAN_NOTICES, queue_job
from kalkan.db.session import Database
from kalkan.domain.case_state import transition
from kalkan.domain.enums import (
    CaseState,
    EventType,
    RecheckMethod,
    RecheckResult,
    SightingSource,
)
from kalkan.enrichment.admission import whitelist_rules
from kalkan.enrichment.webrisk import WebRiskClient, WebRiskError
from kalkan.enrichment.whitelist import find_rule
from kalkan.followup import rules
from kalkan.followup.rules import Advance, BrowserPage, Fetch, LightPage, Move, Observation, Reason
from kalkan.logging import get_logger
from kalkan.net.safe_http import SafeHttpClient
from kalkan.scanning import patterns as page_patterns
from kalkan.scanning.capture_jobs import country_for
from kalkan.scanning.form_patterns import input_kind
from kalkan.scanning.html import decode, parse_page
from kalkan.scanning.precheck import (
    EMPTY_TEXT_CHARS,
    contains_brand,
    entry_urls,
    fetch_first,
    request_headers,
    without_hosts,
)

SYSTEM = "system"
RECHECK_SECONDS = 40.0
FOLLOWED = frozenset({CaseState.NOTIFIED, CaseState.TAKEN_DOWN})
LIGHT = frozenset({CaseState.WATCHING, CaseState.DISMISSED})
CARD_KINDS = frozenset({"card_number", "card_cvv", "card_expiry"})
MAX_DETAIL = 500

log = get_logger(__name__)


@dataclass(frozen=True)
class Target:
    case_id: uuid.UUID
    customer_id: uuid.UUID
    brand_id: uuid.UUID
    state: CaseState
    host: str
    urls: list[str]
    country: str | None
    markets: list[str]
    keywords: list[str]
    failures_before: int


@dataclass(frozen=True)
class RecheckOutcome:
    case_id: uuid.UUID
    skipped: bool = False
    observation: Observation | None = None
    move: Move = Move.NONE
    browser_job: uuid.UUID | None = None
    reappeared: str | None = None


# The browser job for a target; None when one is already waiting for the case.
IssueBrowser = Callable[[Target, datetime], uuid.UUID | None]


def _connection_failures(session: Session, case_id: uuid.UUID) -> int:
    """HTTP checks that failed to connect, in a row, right before now."""
    recent = session.scalars(
        select(Recheck.detail)
        .where(Recheck.case_id == case_id, Recheck.method == RecheckMethod.HTTP)
        .order_by(Recheck.checked_at.desc())
        .limit(rules.CONNECTION_FAILURES)
    ).all()
    count = 0
    for detail in recent:
        if detail.get("reason") != Reason.CONNECTION.value:
            break
        count += 1
    return count


def previous_result(session: Session, case_id: uuid.UUID) -> RecheckResult | None:
    return session.scalar(
        select(Recheck.result)
        .where(Recheck.case_id == case_id)
        .order_by(Recheck.checked_at.desc())
        .limit(1)
    )


def _whitelisted(session: Session, case: Case, host: str) -> bool:
    last = session.scalar(
        select(CaseEvent.type)
        .where(
            CaseEvent.case_id == case.id,
            CaseEvent.type.in_([EventType.DISMISSED, EventType.WHITELISTED]),
        )
        .order_by(CaseEvent.at.desc())
        .limit(1)
    )
    if last is EventType.WHITELISTED:
        return True
    return find_rule(host, case.brand_id, whitelist_rules(session, case.customer_id)) is not None


def _target(session: Session, case_id: uuid.UUID) -> Target | None:
    case = session.get(Case, case_id)
    if case is None or case.state not in FOLLOWED | LIGHT:
        return None
    customer = session.get_one(Customer, case.customer_id)
    if customer.is_demo:
        return None
    brand = session.get_one(Brand, case.brand_id)
    domain = session.get_one(Domain, case.domain_id)
    manual = session.scalar(
        select(DomainSighting.detail)
        .where(
            DomainSighting.domain_id == domain.id,
            DomainSighting.source == SightingSource.MANUAL,
        )
        .order_by(DomainSighting.seen_at.desc())
        .limit(1)
    )
    return Target(
        case_id=case.id,
        customer_id=case.customer_id,
        brand_id=brand.id,
        state=case.state,
        host=domain.name,
        urls=entry_urls(domain.name, (manual or {}).get("url")),
        country=country_for(brand.target_markets),
        markets=list(brand.target_markets),
        keywords=list(brand.keywords),
        failures_before=_connection_failures(session, case.id),
    )


def _event(
    session: Session, case: Case, kind: EventType, at: datetime, data: dict[str, Any]
) -> None:
    session.add(
        CaseEvent(
            customer_id=case.customer_id,
            case_id=case.id,
            at=at,
            type=kind,
            actor=SYSTEM,
            data=data,
        )
    )


def _record(
    session: Session,
    case: Case,
    method: RecheckMethod,
    observation: Observation | None,
    at: datetime,
    detail: dict[str, Any],
) -> None:
    result = observation.result if observation is not None else RecheckResult.UNKNOWN
    data = dict(detail)
    if observation is not None:
        data.update(reason=observation.reason.value, http_status=observation.http_status)
    session.add(
        Recheck(
            customer_id=case.customer_id,
            case_id=case.id,
            checked_at=at,
            method=method,
            result=result,
            detail=data,
        )
    )


def apply_observation(
    session: Session,
    case: Case,
    observation: Observation,
    *,
    previous: RecheckResult | None,
    method: RecheckMethod,
    now: datetime,
) -> Advance:
    """Move a locked ``notified`` or ``taken_down`` case on after its final observation."""
    step = rules.advance(
        case.state,
        observation,
        offline_since=case.offline_since,
        monitor_until=case.monitor_until,
        previous=previous,
        now=now,
    )
    data: dict[str, Any] = {"method": method.value, "reason": observation.reason.value}
    if observation.http_status is not None:
        data["http_status"] = observation.http_status
    if step.offline_detected:
        _event(session, case, EventType.OFFLINE_DETECTED, now, data)
        case.last_action, case.last_action_at = "Site offline, monitoring", now
    if step.online_again and step.move is not Move.REOPEN:
        _event(session, case, EventType.ONLINE_AGAIN, now, data)
        case.last_action, case.last_action_at = "Site online again", now
    if step.warning_active:
        _event(session, case, EventType.WARNING_ACTIVE, now, data)
    case.offline_since = step.offline_since
    if step.move is Move.TAKE_DOWN:
        since = case.offline_since
        transition(
            session,
            case,
            CaseState.TAKEN_DOWN,
            actor=SYSTEM,
            data={**data, "offline_since": since.isoformat() if since else None},
            at=now,
            last_action="Taken down",
        )
    elif step.move is Move.REOPEN:
        transition(
            session,
            case,
            CaseState.NOTIFIED,
            actor=SYSTEM,
            data=data,
            at=now,
            last_action="Site back online, reported again",
        )
        # A new round of notices with the new evidence (§16, K144).
        queue_job(session, PLAN_NOTICES, case_id=case.id, customer_id=case.customer_id)
    elif step.move is Move.ARCHIVE:
        transition(session, case, CaseState.ARCHIVED, actor=SYSTEM, at=now, last_action="Archived")
    else:
        case.next_check_at = step.next_check_at
    log.info(
        "recheck.applied",
        case_id=str(case.id),
        result=observation.result.value,
        reason=observation.reason.value,
        move=step.move.value,
        method=method.value,
    )
    return step


def _web_risk(webrisk: WebRiskClient | None, host: str, observation: Observation) -> Observation:
    if webrisk is None or observation.result is not RecheckResult.ONLINE:
        return observation
    try:
        listed = webrisk.lookup(host).get("threat_types")
    except (WebRiskError, OSError) as exc:
        log.warning("recheck.webrisk_failed", host=host, error=str(exc))
        return observation
    if listed:
        return Observation(RecheckResult.WARNING, Reason.WARNING, observation.http_status)
    return observation


def _light_page(raw: str, url: str, host: str, keywords: Sequence[str]) -> LightPage:
    page = parse_page(raw, url)
    cards = 0
    for form in page.forms:
        for item in form.inputs:
            kind = input_kind(
                input_type=item.get("type", ""),
                autocomplete=item.get("autocomplete", ""),
                texts=(item.get("name", ""), item.get("id", "")),
                placeholder=item.get("placeholder", ""),
            )
            cards += kind in CARD_KINDS
    own = (host,)
    return LightPage(
        password_fields=page.password_fields,
        card_fields=cards,
        brand_on_page=contains_brand(without_hosts(page.title or "", own), keywords)
        or contains_brand(without_hosts(page.text, own), keywords),
        parked=page_patterns.parked_by_text(page.text, page.title or ""),
        empty=len(page.text) < EMPTY_TEXT_CHARS and not any(f.inputs for f in page.forms),
    )


def run_recheck(
    database: Database,
    client: SafeHttpClient,
    case_id: uuid.UUID,
    *,
    now: Callable[[], datetime],
    issue_browser: IssueBrowser,
    webrisk: WebRiskClient | None = None,
) -> RecheckOutcome:
    with database.worker_session() as session:
        target = _target(session, case_id)
        if target is not None and target.state is CaseState.DISMISSED:
            dismissed = session.get_one(Case, case_id)
            if _whitelisted(session, dismissed, target.host):
                dismissed.next_check_at = None
                return RecheckOutcome(case_id, skipped=True)
    if target is None:
        return RecheckOutcome(case_id, skipped=True)

    deadline = client.deadline(RECHECK_SECONDS)
    fetched = fetch_first(client, target.urls, request_headers(target.markets), deadline)
    response = fetched.response
    raw, title, text = "", "", ""
    if response is not None:
        raw = decode(response.body, response.headers.get("content-type"))
        page = parse_page(raw, response.url)
        title, text = page.title or "", page.text
    http = rules.classify_http(
        Fetch(fetched.outcome.value),
        status=response.status if response is not None else None,
        text=text,
        title=title,
        failures_before=target.failures_before,
    )
    detail: dict[str, Any] = {
        "url": response.url[:MAX_DETAIL] if response is not None else None,
        "errors": "; ".join(fetched.errors)[:MAX_DETAIL] or None,
    }
    finished = now()

    if target.state in LIGHT:
        return _light(
            database, target, http, raw, response.url if response else "", detail, finished
        )

    http = _web_risk(webrisk, target.host, http)
    job_id: uuid.UUID | None = None
    if rules.needs_browser(target.state, http):
        job_id = issue_browser(target, finished)
        detail["browser_job"] = str(job_id) if job_id else None
    with database.worker_session() as session:
        case = session.get(Case, case_id, with_for_update=True)
        if case is None or case.state is not target.state:
            return RecheckOutcome(case_id, skipped=True)
        previous = previous_result(session, case.id)
        _record(session, case, RecheckMethod.HTTP, http, finished, detail)
        if job_id is not None:
            # The browser decides; until it answers the next check waits a full interval.
            interval = (
                rules.NOTIFIED_EVERY if case.state is CaseState.NOTIFIED else rules.TAKEN_DOWN_EVERY
            )
            case.next_check_at = finished + interval
            log.info("recheck.confirming", case_id=str(case_id), job_id=str(job_id))
            return RecheckOutcome(case_id, observation=http, browser_job=job_id)
        step = apply_observation(
            session, case, http, previous=previous, method=RecheckMethod.HTTP, now=finished
        )
    return RecheckOutcome(case_id, observation=http, move=step.move)


def _light(
    database: Database,
    target: Target,
    http: Observation,
    raw: str,
    url: str,
    detail: dict[str, Any],
    at: datetime,
) -> RecheckOutcome:
    reason = None
    if http.result is RecheckResult.ONLINE and raw:
        reason = rules.reappeared(target.state, _light_page(raw, url, target.host, target.keywords))
    with database.worker_session() as session:
        case = session.get(Case, target.case_id, with_for_update=True)
        if case is None or case.state is not target.state:
            return RecheckOutcome(target.case_id, skipped=True)
        _record(session, case, RecheckMethod.HTTP, http, at, {**detail, "reappeared": reason})
        if reason is None:
            case.next_check_at = at + rules.light_interval(case.state)
            return RecheckOutcome(target.case_id, observation=http)
        transition(session, case, CaseState.DETECTED, actor=SYSTEM, reason=reason, at=at)
        queue_job(session, HTTP_PRECHECK, case_id=case.id, customer_id=case.customer_id)
    log.info("recheck.reappeared", case_id=str(target.case_id), reason=reason)
    return RecheckOutcome(target.case_id, observation=http, reappeared=reason)


def apply_browser(
    session: Session,
    case: Case,
    pages: Sequence[BrowserPage],
    *,
    job_id: uuid.UUID,
    now: datetime,
) -> Advance | None:
    """The browser's answer to a ``recheck`` job, inside ``persist_capture``'s transaction.

    The HTTP check that asked for it is the latest HTTP row of the case; a page the browser
    opened wins over it, otherwise it stands.
    """
    if case.state not in FOLLOWED:
        return None
    http_row = session.scalars(
        select(Recheck)
        .where(Recheck.case_id == case.id, Recheck.method == RecheckMethod.HTTP)
        .order_by(Recheck.checked_at.desc())
        .limit(1)
    ).first()
    previous = previous_result(session, case.id)
    browser = rules.classify_browser(pages)
    _record(session, case, RecheckMethod.BROWSER, browser, now, {"job_id": str(job_id)})
    if http_row is None:
        http = Observation(RecheckResult.UNKNOWN, Reason.STATUS)
    else:
        http = Observation(
            http_row.result,
            Reason(http_row.detail.get("reason", Reason.STATUS.value)),
            http_row.detail.get("http_status"),
        )
    observation = rules.final(http, browser)
    return apply_observation(
        session, case, observation, previous=previous, method=RecheckMethod.BROWSER, now=now
    )


# --- the schedule ------------------------------------------------------------------------------

LEASE = timedelta(hours=1)
BATCH = 500


def lease_due(database: Database, now: datetime, *, limit: int = BATCH) -> list[uuid.UUID]:
    """Cases whose check is due; their clock moves an hour on so the next run skips them."""
    with database.worker_session() as session:
        cases = session.scalars(
            select(Case)
            .join(Customer, Customer.id == Case.customer_id)
            .where(
                Case.state.in_(FOLLOWED | LIGHT),
                Case.next_check_at <= now,
                Customer.is_demo.is_(False),
            )
            .order_by(Case.next_check_at)
            .limit(limit)
            .with_for_update(of=Case, skip_locked=True)
        ).all()
        for case in cases:
            case.next_check_at = now + LEASE
        return [case.id for case in cases]
