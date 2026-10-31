"""Storing a printed report and telling the customer (architecture.md §21).

``persist_report`` (worker-default) trusts only the report id of the browser's message and its
own row: it reads ``report.pdf`` from the hand-off folder (no links, size limit, ``%PDF-``),
writes the month's CSV, uploads both to the reports bucket, completes the row (the print token
is cleared) and e-mails the customer's owners once. A broken PDF counts as a failed print.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalkan.api.v1.deps import customer_tz
from kalkan.api.v1.queries import report_cases
from kalkan.config import Settings
from kalkan.db.models import Customer, Report, User
from kalkan.db.session import Database
from kalkan.domain import metrics
from kalkan.domain.enums import UserRole
from kalkan.email.messages import message
from kalkan.email.provider import EmailProvider
from kalkan.logging import get_logger
from kalkan.reports import handoff
from kalkan.reports.content import csv_text, month_period
from kalkan.storage import paths
from kalkan.storage.gcs import ObjectExistsError, ObjectStore, make_key

TEMPLATE = "monthly_report"
PDF_TYPE = "application/pdf"
CSV_TYPE = "text/csv; charset=utf-8"

ProviderFactory = Callable[[], EmailProvider]

log = get_logger(__name__)


class Persisted(StrEnum):
    STORED = "stored"
    ALREADY = "already"  # stored before (a redelivery)
    UNKNOWN = "unknown"  # no such report
    BAD_PDF = "bad_pdf"  # missing or not a PDF: the print failed


@dataclass(frozen=True)
class Result:
    status: Persisted
    emailed: int = 0


def owner_emails(session: Session, customer_id: uuid.UUID) -> list[str]:
    """The customer's active owners, who receive the monthly report."""
    return list(
        session.scalars(
            select(User.email)
            .where(
                User.customer_id == customer_id,
                User.role == UserRole.OWNER,
                User.disabled_at.is_(None),
            )
            .order_by(User.email)
        )
    )


def _put(store: ObjectStore, bucket: str, path: str, data: bytes, content_type: str) -> str:
    """Upload once; a retry after an upload finds the object already there."""
    try:
        return store.put(bucket, path, data, content_type).key
    except ObjectExistsError:
        return make_key(bucket, path)


def summary(session: Session, customer: Customer, period: str) -> tuple[str, metrics.Kpis]:
    """The month's CSV and headline numbers."""
    tz = customer_tz(customer)
    start, after = month_period(period).bounds(tz)
    rows = report_cases(session, customer.id, start, after)
    return csv_text(rows, tz), metrics.kpis([row.facts for row in rows])


def _email(
    session: Session,
    settings: Settings,
    provider: ProviderFactory,
    customer: Customer,
    period: str,
    kpis: metrics.Kpis,
) -> int:
    recipients = owner_emails(session, customer.id)
    if not recipients:
        log.warning("report.no_owner", customer_id=str(customer.id))
        return 0
    if not settings.app_base_url:
        raise RuntimeError("APP_BASE_URL is required for the report e-mail")
    context = {
        "customer": customer.name,
        "month": f"{month_period(period).start:%B %Y}",
        "detected": kpis.detected,
        "verified": kpis.verified,
        "taken_down": kpis.taken_down,
        "median": "—"
        if kpis.median_takedown_hours is None
        else f"{kpis.median_takedown_hours:.0f} h",
        "url": f"{settings.app_base_url.rstrip('/')}/reports",
    }
    sender = provider()
    for to in recipients:
        sender.send(message(TEMPLATE, to, **context))
    return len(recipients)


def persist_report(
    database: Database,
    settings: Settings,
    store: ObjectStore,
    bucket: str,
    provider: ProviderFactory,
    report_id: uuid.UUID,
    *,
    now: datetime,
    root: Path = handoff.ROOT,
) -> Result:
    with database.worker_session() as session:
        report = session.get(Report, report_id, with_for_update=True)
        if report is None:
            handoff.remove(report_id, root)
            log.warning("report.unknown", report_id=str(report_id))
            return Result(Persisted.UNKNOWN)
        if report.generated_at is not None:
            handoff.remove(report_id, root)
            return Result(Persisted.ALREADY)
        try:
            pdf = handoff.read_pdf(report_id, root)
        except handoff.HandoffError as exc:
            log.warning("report.bad_pdf", report_id=str(report_id), error=str(exc))
            return Result(Persisted.BAD_PDF)
        customer = session.get_one(Customer, report.customer_id)
        csv, kpis = summary(session, customer, report.period)
        report.pdf_key = _put(
            store, bucket, paths.report_file(customer.id, report.period, "pdf"), pdf, PDF_TYPE
        )
        report.csv_key = _put(
            store,
            bucket,
            paths.report_file(customer.id, report.period, "csv"),
            csv.encode(),
            CSV_TYPE,
        )
        report.generated_at = now
        report.print_token_hash = report.print_token_expires_at = None
        emailed = _email(session, settings, provider, customer, report.period, kpis)
        period = report.period
    handoff.remove(report_id, root)
    log.info(
        "report.stored", report_id=str(report_id), period=period, pdf_bytes=len(pdf), to=emailed
    )
    return Result(Persisted.STORED, emailed)
