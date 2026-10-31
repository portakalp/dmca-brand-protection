"""``ai_classify_reply(message_id)`` on the ``ai`` queue, consumed by worker-ai (§13, §16).

Sent by name by ``process_webhook_event`` and the ops panel. Haiku sorts the reply; the action
is code (``followup.replies``) and never sends anything.
"""

from __future__ import annotations

import uuid

import dramatiq

from kalkan.ai.queue import AI_CLASSIFY_REPLY_ACTOR, AI_QUEUE
from kalkan.config import get_settings
from kalkan.db.base import utcnow
from kalkan.db.session import get_database
from kalkan.followup.classify import classify_reply
from kalkan.workers.assess import ai_client


@dramatiq.actor(
    actor_name=AI_CLASSIFY_REPLY_ACTOR, queue_name=AI_QUEUE, max_retries=3, time_limit=300_000
)
def ai_classify_reply(message_id: str, customer_id: str | None = None) -> None:
    # customer_id rides along in the ops panel's outbox payload for the demo filter.
    classify_reply(get_database(), ai_client(), get_settings(), uuid.UUID(message_id), now=utcnow())
