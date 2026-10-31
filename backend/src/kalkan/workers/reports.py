"""Monthly reports in worker-default (architecture.md §7, §21).

- ``build_reports()``: the 1st of each month, 09:00 UTC; ``build_report`` for every customer
  due one (not the demo, not a suspended customer).
- ``build_report(customer_id, period, actor)``: open the month's report and send it to the
  printer; ``period`` (``YYYY-MM``) defaults to the customer's last month. A refusal is logged.
- ``persist_report(report_id)`` and ``report_print_failed(report_id, attempt)`` come from
  worker-browser on the ``persist`` queue (its Redis user may write no other).
"""

from __future__ import annotations

import uuid
from functools import cache

import dramatiq

from kalkan.config import ConfigError, get_settings
from kalkan.db.base import utcnow
from kalkan.db.session import get_database
from kalkan.logging import get_logger
from kalkan.reports import build, handoff, persist
from kalkan.storage.gcs import ObjectStore
from kalkan.workers.notify import email_provider

REPORTS_QUEUE = "reports"
# The hand-off folder; tests point it elsewhere.
ROOT = handoff.ROOT

log = get_logger(__name__)


@cache
def reports_store() -> tuple[ObjectStore, str]:
    settings = get_settings()
    if settings.gcs_reports_bucket is None:
        raise ConfigError("GCS_REPORTS_BUCKET is required to store reports")
    store = ObjectStore.from_settings(settings)
    if settings.storage_emulator_host:  # local only: the emulator starts empty
        store.ensure_buckets(settings, settings.gcs_reports_bucket)
    return store, settings.gcs_reports_bucket


@dramatiq.actor(queue_name=REPORTS_QUEUE, max_retries=0, time_limit=120_000)
def build_reports() -> None:
    customers = build.reportable_customers(get_database())
    for customer_id in customers:
        build_report.send(str(customer_id))
    log.info("report.run", customers=len(customers))


@dramatiq.actor(queue_name=REPORTS_QUEUE, max_retries=3, time_limit=60_000)
def build_report(customer_id: str, period: str | None = None, actor: str | None = None) -> None:
    try:
        build.build_report(
            get_database(),
            uuid.UUID(customer_id),
            period or None,
            now=utcnow(),
            actor=actor or None,
            root=ROOT,
        )
    except build.ReportError as exc:
        log.error("report.build_refused", customer_id=customer_id, error=str(exc))


@dramatiq.actor(
    actor_name=handoff.PERSIST_ACTOR,
    queue_name=handoff.REPORT_PERSIST_QUEUE,
    max_retries=5,
    time_limit=120_000,
)
def persist_report(report_id: str) -> None:
    try:
        report = uuid.UUID(report_id)
    except ValueError:
        log.warning("report.bad_persist_message")
        return
    store, bucket = reports_store()
    result = persist.persist_report(
        get_database(),
        get_settings(),
        store,
        bucket,
        email_provider,
        report,
        now=utcnow(),
        root=ROOT,
    )
    if result.status is persist.Persisted.BAD_PDF:
        # The attempt is not in this message: a missing or broken PDF gets one last print.
        build.print_failed(get_database(), report, build.MAX_ATTEMPTS - 1, now=utcnow(), root=ROOT)


@dramatiq.actor(
    actor_name=handoff.FAILED_ACTOR,
    queue_name=handoff.REPORT_PERSIST_QUEUE,
    max_retries=0,
    time_limit=60_000,
)
def report_print_failed(report_id: str, attempt: str = "1") -> None:
    try:
        report, tries = uuid.UUID(report_id), int(attempt)
    except ValueError:
        log.warning("report.bad_failed_message")
        return
    build.print_failed(get_database(), report, tries, now=utcnow(), root=ROOT)
