"""Clearing ``blocked_reason`` after a gate is lifted (architecture.md §7, §15).

A change that removes a sending gate (the customer's sending or dry-run, the LOA, the global
send switch, lifting a suspension and legal review) leaves a ``clear_blocked_cases`` job. It empties
``blocked_reason`` on the affected ``approved`` cases (demo cases never) and plans their notices
again in the same transaction, so ``send_notice`` checks every gate anew: a gate that still
holds sets the reason back. Clearing is therefore always safe.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, update

from kalkan.db.models import Case, Customer
from kalkan.db.outbox import PLAN_NOTICES, queue_job
from kalkan.db.session import Database
from kalkan.domain.enums import CaseState
from kalkan.logging import get_logger

log = get_logger(__name__)


def clear_blocked_cases(database: Database, customer_id: uuid.UUID | None) -> int:
    """``None`` clears every customer's cases (the global send switch)."""
    real = select(Customer.id).where(Customer.is_demo.is_(False))
    statement = update(Case).where(
        Case.state == CaseState.APPROVED,
        Case.blocked_reason.is_not(None),
        Case.customer_id.in_(real),
    )
    if customer_id is not None:
        statement = statement.where(Case.customer_id == customer_id)
    with database.worker_session() as session:
        rows = session.execute(
            statement.values(blocked_reason=None).returning(Case.id, Case.customer_id)
        ).all()
        # Plan again: blocked notices are re-queued and every gate is checked anew (K137).
        for case_id, owner in rows:
            queue_job(session, PLAN_NOTICES, case_id=case_id, customer_id=owner)
    cleared = len(rows)
    log.info(
        "notices.blocks_cleared",
        customer_id=str(customer_id) if customer_id else None,
        cases=cleared,
    )
    return cleared
