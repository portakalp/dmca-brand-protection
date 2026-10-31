"""Admission of brand matches and whitelist re-checks (architecture.md §9).

``admit`` runs the match through the whitelist and opens a ``detected`` case when nothing
covers it. ``apply_whitelist`` dismisses the open cases a changed whitelist now covers.
Both run as kalkan_worker; the demo workspace is frozen (K46).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalkan.db.case_numbers import next_case_number
from kalkan.db.models import (
    BrandMatch,
    Case,
    CaseEvent,
    Customer,
    Domain,
    DomainSighting,
    OfficialDomain,
    WhitelistEntry,
)
from kalkan.domain.case_state import transition
from kalkan.domain.enums import CaseState, EventType
from kalkan.enrichment.whitelist import WhitelistRule, find_rule
from kalkan.logging import get_logger
from kalkan.notices.retraction import request_retraction

ENRICHMENT_MAX_AGE = timedelta(hours=24)
SYSTEM = "system"
CLOSED_STATES = (CaseState.DISMISSED, CaseState.ARCHIVED)

log = get_logger(__name__)


def whitelist_rules(session: Session, customer_id: uuid.UUID) -> list[WhitelistRule]:
    entries = session.execute(
        select(WhitelistEntry.pattern, WhitelistEntry.brand_id).where(
            WhitelistEntry.customer_id == customer_id
        )
    ).all()
    officials = session.execute(
        select(OfficialDomain.domain, OfficialDomain.brand_id).where(
            OfficialDomain.customer_id == customer_id
        )
    ).all()
    # An official domain covers every brand of the customer: a keyword of brand A may well
    # match the customer's own site for brand B.
    return [WhitelistRule(row.pattern, row.brand_id) for row in entries] + [
        WhitelistRule(row.domain, None, official=True) for row in officials
    ]


def needs_enrichment(domain: Domain, now: datetime) -> bool:
    return domain.enriched_at is None or now - domain.enriched_at >= ENRICHMENT_MAX_AGE


@dataclass(frozen=True)
class Admission:
    case_id: uuid.UUID | None
    domain_id: uuid.UUID | None
    created: bool
    whitelisted: bool
    # The case is detected: enrich_domain has to run (again) for it. It is idempotent and
    # skips the lookups for a fresh domain but still gives the case its evidence and signal.
    follow_up: bool


SKIPPED = Admission(None, None, created=False, whitelisted=False, follow_up=False)


def admit(session: Session, match_id: uuid.UUID, *, now: datetime, prefix: str) -> Admission:
    """Admit one match; running it twice for the same match opens one case."""
    match = session.scalar(select(BrandMatch).where(BrandMatch.id == match_id).with_for_update())
    if match is None:
        return SKIPPED
    customer = session.get(Customer, match.customer_id)
    domain = session.get(Domain, match.domain_id)
    if customer is None or domain is None:  # pragma: no cover - foreign keys
        return SKIPPED
    if customer.is_demo:
        match.checked_at = now
        return SKIPPED

    existing = session.scalar(
        select(Case).where(
            Case.customer_id == match.customer_id,
            Case.brand_id == match.brand_id,
            Case.domain_id == match.domain_id,
        )
    )
    if existing is not None:
        match.admitted = True
        match.checked_at = match.checked_at or now
        # A redelivered message: the first attempt may have failed to queue the follow-up.
        follow_up = existing.state is CaseState.DETECTED
        return Admission(
            existing.id, domain.id, created=False, whitelisted=False, follow_up=follow_up
        )

    match.checked_at = now
    rule = find_rule(domain.name, match.brand_id, whitelist_rules(session, match.customer_id))
    if rule is not None:
        log.info("admission.whitelisted", match_id=str(match.id), pattern=rule.pattern)
        return Admission(None, domain.id, created=False, whitelisted=True, follow_up=False)

    source = session.scalar(
        select(DomainSighting.source)
        .where(DomainSighting.domain_id == domain.id)
        .order_by(DomainSighting.seen_at, DomainSighting.id)
        .limit(1)
    )
    case = Case(
        customer_id=match.customer_id,
        number=next_case_number(session, prefix),
        brand_id=match.brand_id,
        domain_id=domain.id,
        state=CaseState.DETECTED,
        first_seen_at=match.first_matched_at,
        last_action="Detected",
        last_action_at=now,
    )
    session.add(case)
    session.flush()
    session.add(
        CaseEvent(
            customer_id=case.customer_id,
            case_id=case.id,
            at=now,
            type=EventType.DETECTED,
            actor=SYSTEM,
            data={
                "source": str(source) if source else None,
                "reasons": list(match.reasons),
                "score": match.score,
                "domain": domain.name,
            },
        )
    )
    match.admitted = True
    return Admission(case.id, domain.id, created=True, whitelisted=False, follow_up=True)


def apply_whitelist(session: Session, customer_id: uuid.UUID, *, now: datetime) -> list[uuid.UUID]:
    """Dismiss the open cases of ``customer_id`` that the whitelist now covers."""
    customer = session.get(Customer, customer_id)
    if customer is None or customer.is_demo:
        return []
    rules = whitelist_rules(session, customer_id)
    rows = session.execute(
        select(Case, Domain.name)
        .join(Domain, Domain.id == Case.domain_id)
        .where(Case.customer_id == customer_id, Case.state.not_in(CLOSED_STATES))
        .order_by(Case.number)
        .with_for_update(of=Case)
    ).all()
    dismissed = []
    for case, host in rows:
        rule = find_rule(host, case.brand_id, rules)
        if rule is None:
            continue
        noticed = case.first_notice_at is not None
        transition(
            session,
            case,
            CaseState.DISMISSED,
            actor=SYSTEM,
            reason="whitelisted",
            data={"pattern": rule.pattern, "official": rule.official},
            event=EventType.WHITELISTED,
            at=now,
            last_action="Whitelisted",
        )
        if noticed:
            request_retraction(session, case)
        dismissed.append(case.id)
    if dismissed:
        log.info("whitelist.cases_dismissed", customer_id=str(customer_id), cases=len(dismissed))
    return dismissed
