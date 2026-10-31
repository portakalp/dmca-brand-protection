"""The outbox dispatcher: ``jobs_outbox`` rows become Dramatiq messages (architecture.md §7).

Runs every 5 seconds in the scheduler. Rows are locked with ``SKIP LOCKED`` so two
dispatchers never send the same row; a row is marked dispatched after its send, so a crash
in between can send it twice: every job is idempotent. A broker outage uses up no attempts;
a row that is itself broken (unknown kind, bad payload) is counted and stays in the table for
the ops panel after ``MAX_ATTEMPTS``. Jobs for the demo workspace are marked dispatched
without being sent (K46).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalkan.db.models import Customer, JobOutbox
from kalkan.db.outbox import (
    AI_NOTICE_TEXT,
    BUILD_REPORT,
    CLASSIFY_REPLY,
    CLEAR_BLOCKED_CASES,
    COMPUTE_SIGNALS,
    CUSTOMER_ALERT,
    ENRICH_DOMAIN,
    HASH_BRAND_ASSETS,
    HTTP_PRECHECK,
    PLAN_NOTICES,
    PROCESS_WEBHOOK_EVENT,
    RECHECK,
    REFRESH_BRAND_FAVICONS,
    REFRESH_BRAND_REFERENCES,
    SEND_NOTICE,
    WHITELIST_CHANGED,
)
from kalkan.db.session import Database
from kalkan.logging import get_logger

BATCH = 100
MAX_ATTEMPTS = 10
# Outbox kind -> actor name; payload keys are the actor's keyword arguments.
ACTORS = {
    WHITELIST_CHANGED: "whitelist_changed",
    ENRICH_DOMAIN: "enrich_domain",
    REFRESH_BRAND_FAVICONS: "refresh_brand_favicons",
    REFRESH_BRAND_REFERENCES: "refresh_brand_references",
    HASH_BRAND_ASSETS: "hash_brand_assets",
    HTTP_PRECHECK: "http_precheck",
    COMPUTE_SIGNALS: "compute_signals",
    CLEAR_BLOCKED_CASES: "clear_blocked_cases",
    PLAN_NOTICES: "plan_notices",
    AI_NOTICE_TEXT: "ai_notice_text",  # worker-ai; sent by name (kalkan.ai.queue.BY_NAME)
    SEND_NOTICE: "send_notice",
    CUSTOMER_ALERT: "customer_alert",
    RECHECK: "recheck",
    PROCESS_WEBHOOK_EVENT: "process_webhook_event",
    CLASSIFY_REPLY: "ai_classify_reply",  # worker-ai; sent by name
    BUILD_REPORT: "build_report",
}

Send = Callable[..., None]

log = get_logger(__name__)


def _is_demo(session: Session, payload: dict[str, str]) -> bool:
    customer_id = payload.get("customer_id")
    if customer_id is None:
        return False
    return bool(
        session.scalar(select(Customer.is_demo).where(Customer.id == uuid.UUID(customer_id)))
    )


def dispatch(database: Database, send: Send, now: Callable[[], datetime]) -> int:
    """Send pending rows; returns how many were dispatched."""
    dispatched = 0
    with database.worker_session() as session:
        rows = session.scalars(
            select(JobOutbox)
            .where(JobOutbox.dispatched_at.is_(None), JobOutbox.attempts < MAX_ATTEMPTS)
            .order_by(JobOutbox.created_at, JobOutbox.id)
            .limit(BATCH)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            actor = ACTORS.get(row.kind)
            try:
                if actor is None:
                    raise ValueError(f"unknown outbox kind {row.kind!r}")
                payload = {str(key): str(value) for key, value in dict(row.payload).items()}
                demo = _is_demo(session, payload)
            except (ValueError, TypeError) as exc:
                # Something is wrong with this row itself: count it and go on with the others.
                row.attempts += 1
                log.error(
                    "outbox.bad_row", job_id=str(row.id), error=str(exc), attempts=row.attempts
                )
                continue
            if demo:
                row.dispatched_at = now()
                continue
            try:
                send(actor, **payload)
            except Exception:
                # The broker is down: no attempt is used up; the next run tries again.
                log.exception("outbox.send_failed", kind=row.kind, job_id=str(row.id))
                break
            row.attempts += 1
            row.dispatched_at = now()
            dispatched += 1
    return dispatched
