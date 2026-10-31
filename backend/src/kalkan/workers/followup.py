"""The ``followup`` and ``webhooks`` queues, consumed by worker-default (§7, §16).

- ``recheck(case_id)``: one follow-up check (``followup.recheck``); a contradicting HTTP check
  issues a ``recheck`` browser job (sent by name to worker-browser; K30).
- ``schedule_rechecks()``: every 15 minutes; leases the due cases and sends ``recheck``.
- ``check_escalations()``: hourly; escalation levels that are due (``followup.escalation``).
- ``process_webhook_event(event_id)``: a stored Postmark event
  (``followup.webhooks``); a matched reply goes on to ``ai_classify_reply`` on worker-ai, sent by
  name.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from functools import cache

import dramatiq

from kalkan.ai.queue import send_classify_reply
from kalkan.config import get_settings
from kalkan.db.base import utcnow
from kalkan.db.session import get_database
from kalkan.enrichment.webrisk import WebRiskClient
from kalkan.followup import escalation, webhooks
from kalkan.followup import recheck as follow_up
from kalkan.followup.webhooks import Outcome
from kalkan.logging import get_logger
from kalkan.scanning.capture_contract import Purpose
from kalkan.scanning.capture_jobs import issue_capture
from kalkan.workers.persist import evidence_store
from kalkan.workers.scanning import capture_redis, http_client

FOLLOWUP_QUEUE = "followup"
WEBHOOKS_QUEUE = "webhooks"

log = get_logger(__name__)


@cache
def webrisk_client() -> WebRiskClient | None:
    key = get_settings().webrisk_api_key
    return WebRiskClient(key.get_secret_value()) if key is not None else None


def issue_browser(target: follow_up.Target, now: datetime) -> uuid.UUID | None:
    job = issue_capture(
        capture_redis(),
        purpose=Purpose.RECHECK,
        customer_id=target.customer_id,
        brand_id=target.brand_id,
        case_id=target.case_id,
        urls=target.urls,
        country=target.country,
        now=now,
    )
    return job.job_id if job is not None else None


@dramatiq.actor(queue_name=FOLLOWUP_QUEUE, max_retries=3, time_limit=60_000)
def recheck(case_id: str, customer_id: str | None = None) -> None:
    follow_up.run_recheck(
        get_database(),
        http_client(),
        uuid.UUID(case_id),
        now=utcnow,
        issue_browser=issue_browser,
        webrisk=webrisk_client(),
    )


@dramatiq.actor(queue_name=FOLLOWUP_QUEUE, max_retries=0, time_limit=120_000)
def schedule_rechecks() -> None:
    due = follow_up.lease_due(get_database(), utcnow())
    for case_id in due:
        recheck.send(str(case_id))
    log.info("recheck.scheduled", cases=len(due))


@dramatiq.actor(queue_name=FOLLOWUP_QUEUE, max_retries=0, time_limit=600_000)
def check_escalations() -> None:
    done = escalation.check_escalations(get_database(), get_settings(), now=utcnow())
    log.info("escalation.checked", escalated=len(done))


@dramatiq.actor(queue_name=WEBHOOKS_QUEUE, max_retries=5, time_limit=60_000)
def process_webhook_event(event_id: str) -> None:
    result = webhooks.process_webhook_event(
        get_database(), get_settings(), evidence_store, uuid.UUID(event_id), now=utcnow()
    )
    if result.outcome is Outcome.CLASSIFY and result.message_id is not None:
        send_classify_reply(str(result.message_id))
