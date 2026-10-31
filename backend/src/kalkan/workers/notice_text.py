"""``ai_notice_text(notice_id)`` on the ``ai`` queue, consumed by worker-ai (§13, §15).

Writes a notice's explanation paragraph (``notices.text``), then sends ``send_notice`` by name
to worker-default. The AI never decides anything here: an unavailable AI or a rejected answer
falls back to the fixed paragraph. On the last attempt any failure leaves the notice as a draft
for the reconciler.
"""

from __future__ import annotations

import uuid

import dramatiq

from kalkan.ai.queue import AI_NOTICE_TEXT_ACTOR, AI_QUEUE, send_notice
from kalkan.config import get_settings
from kalkan.db.base import utcnow
from kalkan.db.session import get_database
from kalkan.logging import get_logger
from kalkan.notices.text import write_explanation
from kalkan.workers.assess import ai_client

log = get_logger(__name__)


@dramatiq.actor(
    actor_name=AI_NOTICE_TEXT_ACTOR, queue_name=AI_QUEUE, max_retries=3, time_limit=300_000
)
def ai_notice_text(notice_id: str, customer_id: str | None = None) -> None:
    result = write_explanation(
        get_database(), ai_client(), get_settings(), uuid.UUID(notice_id), now=utcnow()
    )
    if result is not None and result.queued:
        send_notice(str(result.notice_id), str(result.customer_id))
