"""worker-default's stand-ins for the AI actors when AI is off (architecture.md §7, §13).

``kalkan.ai.queue`` sends these instead of the worker-ai actors when ``Settings.use_ai`` is
false, with the same arguments, so the pipeline completes without Anthropic and nothing waits on
the ``ai`` queue:

- ``triage_without_ai(case_id)`` / ``confirm_without_ai(case_id)`` (``decision`` queue): the
  case goes to the review queue with the reason ``ai_disabled`` (``decision/without_ai.py``).
- ``notice_text_without_ai(notice_id)`` (``notify``): the fixed explanation paragraph, the same
  fallback an unavailable AI gets (``notices/text.py``), then ``send_notice``.
- ``classify_reply_without_ai(message_id)`` (``webhooks``): the reply is left to the operator,
  as when the AI is unavailable (``followup/classify.py``).

``customer_id`` rides along in outbox payloads for the demo filter; it is not used here.
"""

from __future__ import annotations

import uuid

import dramatiq

from kalkan.ai.queue import (
    CLASSIFY_REPLY_WITHOUT_AI_ACTOR,
    CONFIRM_WITHOUT_AI_ACTOR,
    DECISION_QUEUE,
    NOTICE_TEXT_WITHOUT_AI_ACTOR,
    NOTIFY_QUEUE,
    TRIAGE_WITHOUT_AI_ACTOR,
    WEBHOOKS_QUEUE,
    send_notice,
)
from kalkan.config import get_settings
from kalkan.db.base import utcnow
from kalkan.db.session import get_database
from kalkan.decision.apply import Stage
from kalkan.decision.without_ai import review_without_ai
from kalkan.followup.classify import classify_reply
from kalkan.notices.text import write_explanation


@dramatiq.actor(
    actor_name=TRIAGE_WITHOUT_AI_ACTOR, queue_name=DECISION_QUEUE, max_retries=3, time_limit=30_000
)
def triage_without_ai(case_id: str, customer_id: str | None = None) -> None:
    review_without_ai(get_database(), uuid.UUID(case_id), Stage.TRIAGE, now=utcnow())


@dramatiq.actor(
    actor_name=CONFIRM_WITHOUT_AI_ACTOR, queue_name=DECISION_QUEUE, max_retries=3, time_limit=30_000
)
def confirm_without_ai(case_id: str, customer_id: str | None = None) -> None:
    review_without_ai(get_database(), uuid.UUID(case_id), Stage.CONFIRM, now=utcnow())


@dramatiq.actor(
    actor_name=NOTICE_TEXT_WITHOUT_AI_ACTOR,
    queue_name=NOTIFY_QUEUE,
    max_retries=5,
    time_limit=60_000,
)
def notice_text_without_ai(notice_id: str, customer_id: str | None = None) -> None:
    result = write_explanation(
        get_database(), None, get_settings(), uuid.UUID(notice_id), now=utcnow()
    )
    if result is not None and result.queued:
        send_notice(str(result.notice_id), str(result.customer_id))


@dramatiq.actor(
    actor_name=CLASSIFY_REPLY_WITHOUT_AI_ACTOR,
    queue_name=WEBHOOKS_QUEUE,
    max_retries=5,
    time_limit=60_000,
)
def classify_reply_without_ai(message_id: str, customer_id: str | None = None) -> None:
    classify_reply(get_database(), None, get_settings(), uuid.UUID(message_id), now=utcnow())
