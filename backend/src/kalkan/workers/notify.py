"""The ``notify`` queue, consumed by worker-default (architecture.md §7, §15).

- ``plan_notices(case_id)``: the first wave of an approved case (outbox after an approval or a
  lifted gate; the reconciler).
- ``send_notice(notice_id)``: one queued notice through the gates (sent by name by worker-ai
  after its text, through the outbox for retractions).
- ``customer_alert(case_id)``: the customer's phishing alert, once.
- ``clear_blocked_cases(customer_id?)``: sent through the outbox when a sending gate is lifted.

``customer_id`` rides along in outbox payloads for the demo filter; the actors read it from the
rows again.
"""

from __future__ import annotations

import uuid
from functools import cache

import dramatiq

from kalkan.ai.queue import NOTIFY_QUEUE, SEND_NOTICE_ACTOR
from kalkan.config import get_settings
from kalkan.db.base import utcnow
from kalkan.db.session import get_database
from kalkan.email.provider import EmailProvider, provider_for
from kalkan.notices import blocks
from kalkan.notices.alert import send_customer_alert
from kalkan.notices.planning import plan_notices as plan_case_notices
from kalkan.notices.sender import send_notice as send_one

QUEUE = NOTIFY_QUEUE


@cache
def email_provider() -> EmailProvider:
    return provider_for(get_settings())


@dramatiq.actor(queue_name=QUEUE, max_retries=5, time_limit=60_000)
def clear_blocked_cases(customer_id: str | None = None) -> None:
    blocks.clear_blocked_cases(get_database(), uuid.UUID(customer_id) if customer_id else None)


@dramatiq.actor(queue_name=QUEUE, max_retries=5, time_limit=60_000)
def plan_notices(case_id: str, customer_id: str | None = None) -> None:
    plan_case_notices(get_database(), get_settings(), uuid.UUID(case_id), now=utcnow())


@dramatiq.actor(actor_name=SEND_NOTICE_ACTOR, queue_name=QUEUE, max_retries=5, time_limit=60_000)
def send_notice(notice_id: str, customer_id: str | None = None) -> None:
    send_one(get_database(), get_settings(), email_provider, uuid.UUID(notice_id), now=utcnow())


@dramatiq.actor(queue_name=QUEUE, max_retries=5, time_limit=60_000)
def customer_alert(case_id: str, customer_id: str | None = None) -> None:
    send_customer_alert(
        get_database(), get_settings(), email_provider, uuid.UUID(case_id), now=utcnow()
    )
