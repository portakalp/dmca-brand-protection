"""Reads behind the panel's numbers and lists (§17), as kalkan_api under row level security.

Every query also filters on the customer (the application filter of §24). The cohort reads
``cases (customer_id, first_seen_at)``. Dry-run previews never count: the closing notice of a
takedown is the last real notice sent before the site went down, and whitelisted cases are
not detections (product.md §12).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, exists, func, select
from sqlalchemy.orm import Session

from kalkan.db.models import Case, CaseEvent, Domain, Notice
from kalkan.domain.enums import CaseState, Category, EventType, NoticeTemplate
from kalkan.domain.metrics import LIVE, PANEL_STATUS, CaseFacts

CLOSING_LIMIT = 1


def whitelisted() -> ColumnElement[bool]:
    return exists().where(
        CaseEvent.case_id == Case.id,
        CaseEvent.customer_id == Case.customer_id,
        CaseEvent.type == EventType.WHITELISTED,
    )


def real_notice(*conditions: ColumnElement[bool]) -> list[ColumnElement[bool]]:
    return [
        Notice.case_id == Case.id,
        Notice.customer_id == Case.customer_id,
        Notice.dry_run.is_(False),
        Notice.template != NoticeTemplate.RETRACTION,
        Notice.sent_at.is_not(None),
        *conditions,
    ]


def cohort_query(
    customer_id: uuid.UUID, start: datetime, after: datetime, brand_id: uuid.UUID | None
) -> Select[*tuple[Any, ...]]:
    closing = (
        select(Notice.channel, Notice.sent_at)
        .where(*real_notice(Notice.sent_at <= Case.offline_since))
        .order_by(Notice.sent_at.desc())
        .limit(CLOSING_LIMIT)
        .lateral("closing")
    )
    query = (
        select(
            Case.state,
            Case.first_seen_at,
            Case.verified_at,
            Case.first_notice_at,
            Case.offline_since,
            closing.c.channel,
            closing.c.sent_at,
        )
        .outerjoin(closing, Case.offline_since.is_not(None))
        .where(
            Case.customer_id == customer_id,
            Case.first_seen_at >= start,
            Case.first_seen_at < after,
            ~whitelisted(),
        )
    )
    if brand_id is not None:
        query = query.where(Case.brand_id == brand_id)
    return query


def cohort(
    session: Session,
    customer_id: uuid.UUID,
    start: datetime,
    after: datetime,
    brand_id: uuid.UUID | None,
) -> list[CaseFacts]:
    rows = session.execute(cohort_query(customer_id, start, after, brand_id)).all()
    return [
        CaseFacts(
            state=row[0],
            first_seen_at=row[1],
            verified_at=row[2],
            first_notice_at=row[3],
            offline_since=row[4],
            closing_channel=row[5],
            closing_sent_at=row[6],
        )
        for row in rows
    ]


@dataclass(frozen=True)
class ReportCase:
    """One row of the monthly report's threat list (architecture.md §21)."""

    number: str
    domain: str
    category: Category | None
    facts: CaseFacts


def report_cases(
    session: Session, customer_id: uuid.UUID, start: datetime, after: datetime
) -> list[ReportCase]:
    """The cohort of ``[start, after)`` with what the report lists, oldest first."""
    query = (
        cohort_query(customer_id, start, after, None)
        .add_columns(Case.number, Domain.name, Case.category)
        .join(Domain, Domain.id == Case.domain_id)
        .order_by(Case.first_seen_at, Case.number)
    )
    return [
        ReportCase(
            number=row[7],
            domain=row[8],
            category=row[9],
            facts=CaseFacts(
                state=row[0],
                first_seen_at=row[1],
                verified_at=row[2],
                first_notice_at=row[3],
                offline_since=row[4],
                closing_channel=row[5],
                closing_sent_at=row[6],
            ),
        )
        for row in session.execute(query)
    ]


def live_states() -> Sequence[CaseState]:
    return [state for state, status in PANEL_STATUS.items() if status in LIVE]


def live_count(session: Session, customer_id: uuid.UUID, brand_id: uuid.UUID | None) -> int:
    query = select(func.count()).where(
        Case.customer_id == customer_id, Case.state.in_(live_states())
    )
    if brand_id is not None:
        query = query.where(Case.brand_id == brand_id)
    return int(session.scalar(query) or 0)
