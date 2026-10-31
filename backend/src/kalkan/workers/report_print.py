"""``print_report`` on the ``capture`` queue, consumed by worker-browser (architecture.md §21).

worker-default sends it by name (``kalkan.reports.handoff``) with the report id and attempt;
the token waits in the hand-off folder and is deleted once read. The printer has no database,
no cloud credentials and no proxy (K6): it writes the PDF next to the token's old place and
sends ``persist_report`` by name, or ``report_print_failed`` so worker-default tries again with
a new token. Nothing is retried here, and no error message can carry the token.

The page is opened at ``REPORT_PRINT_BASE_URL`` when set, else at ``APP_BASE_URL``: on a
self-hosted server the public address goes through the reverse proxy, which worker-browser's
internal network cannot reach, so it prints from the web service directly (``http://web:3000``).

Playwright is imported only when a page is printed, so this module loads without it.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import dramatiq
from dramatiq.middleware import TimeLimitExceeded

from kalkan.config import Settings, get_settings
from kalkan.logging import get_logger
from kalkan.reports import handoff

# The hand-off folder; tests point it elsewhere.
ROOT = handoff.ROOT

log = get_logger(__name__)


@dramatiq.actor(
    actor_name=handoff.PRINT_ACTOR,
    queue_name=handoff.PRINT_QUEUE,
    max_retries=0,
    time_limit=120_000,
)
def print_report(report_id: str, attempt: str = "1") -> None:
    handle(report_id, attempt, base_url=print_base_url(get_settings()), root=ROOT)


def print_base_url(settings: Settings) -> str | None:
    """Where the print page is opened: ``REPORT_PRINT_BASE_URL``, else ``APP_BASE_URL``."""
    return settings.report_print_base_url or settings.app_base_url


def handle(report_id: str, attempt: str, *, base_url: str | None, root: Path) -> None:
    try:
        report = uuid.UUID(report_id)
        tries = int(attempt)
    except ValueError:
        log.warning("report.bad_print_message")
        return
    try:
        token = handoff.take_token(report, root)
    except handoff.HandoffError as exc:
        # No token: a redelivery after the first read, or a folder that was cleaned up.
        log.warning("report.no_token", report_id=report_id, error=str(exc))
        handoff.send_failed(report, tries)
        return
    try:
        if not base_url:
            raise RuntimeError("neither REPORT_PRINT_BASE_URL nor APP_BASE_URL is set")
        from kalkan.reports.printing import print_pdf

        pdf, _ = print_pdf(str(report), token, base_url)
        handoff.write_pdf(report, pdf, root)
    except (Exception, TimeLimitExceeded) as exc:  # noqa: BLE001 - any failure is a failed print
        # The type and Playwright's message only: neither holds the token.
        log.warning(
            "report.print_failed",
            report_id=report_id,
            attempt=tries,
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )
        handoff.send_failed(report, tries)
        return
    handoff.send_persist(report)
    log.info("report.printed", report_id=report_id, attempt=tries, pdf_bytes=len(pdf))
