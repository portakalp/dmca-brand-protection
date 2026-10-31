"""Retraction of sent notices (architecture.md §15; K141).

When the whitelist (or "This is our site") dismisses a case that already had real
notices, :func:`request_retraction` runs inside that same transaction: for every real notice
that went out (or was handed to the operator) it writes a ``retraction`` notice to the same
channel, recipient and language and hands it to ``send_notice`` through the outbox. No AI text
is written for it. A retraction of a web-form report becomes a ``retraction`` task; a blocked
retraction does too (``notices.sender``). Sending it marks the original ``retracted``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kalkan.config import Settings, get_settings
from kalkan.db.models import Case, Customer, Notice
from kalkan.db.outbox import SEND_NOTICE, queue_job
from kalkan.domain.enums import NoticeStatus, NoticeTemplate
from kalkan.logging import get_logger
from kalkan.notices.gates import effective_dry_run

RETRACTABLE = (NoticeStatus.SENT, NoticeStatus.ACKNOWLEDGED, NoticeStatus.MANUAL_PENDING)

log = get_logger(__name__)


def request_retraction(session: Session, case: Case, settings: Settings | None = None) -> int:
    """Write the retractions of ``case``'s real notices; returns how many were written."""
    customer = session.get_one(Customer, case.customer_id)
    if customer.is_demo:
        return 0
    dry = effective_dry_run(settings or get_settings(), customer)
    originals = session.scalars(
        select(Notice).where(
            Notice.case_id == case.id,
            Notice.dry_run.is_(False),
            Notice.template != NoticeTemplate.RETRACTION,
            Notice.status.in_(RETRACTABLE),
        )
    ).all()
    written = 0
    for original in originals:
        notice_id = session.scalar(
            insert(Notice)
            .values(
                id=uuid.uuid4(),
                customer_id=case.customer_id,
                case_id=case.id,
                channel=original.channel,
                method=original.method,
                recipient=original.recipient,
                template=NoticeTemplate.RETRACTION,
                language=original.language,
                escalation_level=original.escalation_level,
                round=original.round,
                dry_run=dry,
                status=NoticeStatus.QUEUED,
            )
            .on_conflict_do_nothing()
            .returning(Notice.id)
        )
        if notice_id is not None:
            queue_job(session, SEND_NOTICE, notice_id=notice_id, customer_id=case.customer_id)
            written += 1
    log.info("retraction.queued", case_id=str(case.id), number=case.number, notices=written)
    return written
