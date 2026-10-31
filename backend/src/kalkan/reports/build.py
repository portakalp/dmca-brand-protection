"""Opening a monthly report and handing it to the printer (architecture.md §21, §17).

``build_report`` writes the ``reports`` row for (customer, month) once, gives it a new print
token (single use, 60 minutes; only its hash is stored) and hands the token to worker-browser
through the shared volume (``kalkan.reports.handoff``). A report that already has its PDF is
left alone. A failed print comes back as ``report_print_failed``: a new token and another
attempt, at most :data:`MAX_ATTEMPTS` in all.

Who gets a report: every customer but the demo and a suspended one, for a month that has
ended in the customer's time zone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kalkan.api.v1.deps import customer_tz
from kalkan.db.base import new_id
from kalkan.db.models import AuditLog, Customer, Report
from kalkan.db.outbox import BUILD_REPORT, queue_job
from kalkan.db.session import Database
from kalkan.domain.enums import CustomerStatus
from kalkan.logging import get_logger
from kalkan.reports import handoff
from kalkan.reports.content import PERIOD, has_ended, previous_month
from kalkan.security import hash_token, new_token

TOKEN_TTL = timedelta(minutes=60)
MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=5)

log = get_logger(__name__)


class ReportError(ValueError):
    """The report cannot be made (who or which month); retrying does not help."""


@dataclass(frozen=True)
class Built:
    report_id: uuid.UUID
    created: bool


def reportable(customer: Customer) -> str | None:
    """Why ``customer`` gets no report, or ``None``."""
    if customer.is_demo:
        return "the demo account"
    if customer.status is CustomerStatus.SUSPENDED:
        return "suspended"
    return None


def reportable_customers(database: Database) -> list[uuid.UUID]:
    with database.worker_session() as session:
        customers = session.scalars(select(Customer).order_by(Customer.slug)).all()
        return [customer.id for customer in customers if reportable(customer) is None]


def last_month(customer: Customer, at: datetime) -> str:
    return previous_month(at, customer_tz(customer))


def check_period(customer: Customer, period: str, at: datetime) -> None:
    if not PERIOD.match(period):
        raise ReportError("the month is YYYY-MM")
    if not has_ended(period, at, customer_tz(customer)):
        raise ReportError(f"{period} has not ended yet")


def queue_build(
    session: Session, customer: Customer, period: str | None, *, now: datetime, actor: str
) -> str:
    """Check (customer, period) and leave a ``build_report`` job (ops panel, CLI); the month.

    The checks are repeated by the worker; a month that already has its PDF is refused here.
    """
    refusal = reportable(customer)
    if refusal is not None:
        raise ReportError(f"no report for {refusal}")
    period = period or last_month(customer, now)
    check_period(customer, period, now)
    done = session.scalar(
        select(Report.id).where(
            Report.customer_id == customer.id,
            Report.period == period,
            Report.pdf_key.is_not(None),
        )
    )
    if done is not None:
        raise ReportError(f"the {period} report is already made")
    queue_job(session, BUILD_REPORT, customer_id=customer.id, period=period, actor=actor)
    return period


def _row(session: Session, customer: Customer, period: str) -> tuple[Report, bool]:
    inserted = session.scalar(
        insert(Report)
        .values(id=new_id(), customer_id=customer.id, period=period)
        .on_conflict_do_nothing(index_elements=["customer_id", "period"])
        .returning(Report.id)
    )
    report = session.scalars(
        select(Report)
        .where(Report.customer_id == customer.id, Report.period == period)
        .with_for_update()
    ).one()
    return report, inserted is not None


def _arm(report: Report, now: datetime) -> str:
    token = new_token()
    report.print_token_hash = hash_token(token)
    report.print_token_expires_at = now + TOKEN_TTL
    return token


def build_report(
    database: Database,
    customer_id: uuid.UUID,
    period: str | None,
    *,
    now: datetime,
    actor: str | None = None,
    root: Path = handoff.ROOT,
) -> Built | None:
    """Open (customer, period) and send it to the printer; ``None`` if it is already done.

    ``period`` defaults to the customer's last month. ``actor`` (an operator) is audited.
    """
    with database.worker_session() as session:
        customer = session.get(Customer, customer_id)
        if customer is None:
            raise ReportError("no such customer")
        refusal = reportable(customer)
        if refusal is not None:
            raise ReportError(f"no report for {refusal}")
        period = period or last_month(customer, now)
        check_period(customer, period, now)
        report, created = _row(session, customer, period)
        if report.pdf_key is not None:
            log.info("report.already_done", report_id=str(report.id), period=period)
            return None
        token = _arm(report, now)
        if actor is not None:
            session.add(
                AuditLog(
                    actor=actor,
                    action="report.build",
                    customer_id=customer.id,
                    target=period,
                    data={"report_id": str(report.id)},
                )
            )
        report_id = report.id
    handoff.write_token(report_id, token, root)
    handoff.send_print(report_id, 1)
    log.info("report.queued", report_id=str(report_id), period=period, created=created)
    return Built(report_id, created)


def print_failed(
    database: Database,
    report_id: uuid.UUID,
    attempt: int,
    *,
    now: datetime,
    root: Path = handoff.ROOT,
) -> bool:
    """A print attempt failed: try again with a new token; ``False`` once attempts run out."""
    with database.worker_session() as session:
        report = session.get(Report, report_id, with_for_update=True)
        if report is None or report.pdf_key is not None:
            handoff.remove(report_id, root)
            return False
        if attempt >= MAX_ATTEMPTS:
            report.print_token_hash = report.print_token_expires_at = None
            log.error("report.print_given_up", report_id=str(report_id), attempts=attempt)
            handoff.remove(report_id, root)
            return False
        token = _arm(report, now)
    handoff.write_token(report_id, token, root)
    handoff.send_print(report_id, attempt + 1, delay=RETRY_DELAY)
    log.warning("report.print_retry", report_id=str(report_id), attempt=attempt + 1)
    return True
