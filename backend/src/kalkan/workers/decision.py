"""The ``decision`` queue, consumed by worker-default (architecture.md §7, §14).

- ``decide(case_id)``: sent by worker-ai after a verdict (by name) and by the reconciler;
  applies the rules, then queues ``ai_confirm`` for a ``confirm`` decision; an approval leaves a
  ``plan_notices`` outbox job in the same transaction (``decision/apply.py``). On the last
  attempt any failure is ``job_failed``.
"""

from __future__ import annotations

import uuid

import dramatiq
from dramatiq.middleware import TimeLimitExceeded

from kalkan.ai.queue import DECIDE_ACTOR, DECISION_QUEUE, send_ai_confirm
from kalkan.db.base import utcnow
from kalkan.db.session import get_database
from kalkan.decision.apply import decide as apply_decision
from kalkan.logging import get_logger
from kalkan.signals.compute import record_failure
from kalkan.workers.broker import final_attempt

MAX_RETRIES = 3

log = get_logger(__name__)


@dramatiq.actor(
    actor_name=DECIDE_ACTOR,
    queue_name=DECISION_QUEUE,
    max_retries=MAX_RETRIES,
    time_limit=30_000,
)
def decide(case_id: str) -> None:
    final = final_attempt(MAX_RETRIES)
    try:
        applied = apply_decision(get_database(), uuid.UUID(case_id), now=utcnow())
    except (Exception, TimeLimitExceeded) as exc:
        if not final:
            raise
        log.exception("decision.given_up", case_id=case_id)
        record_failure(
            get_database(),
            uuid.UUID(case_id),
            f"{type(exc).__name__}: {exc}",
            at=utcnow(),
            job=DECIDE_ACTOR,
        )
        return
    if applied is None:
        return
    if applied.queue_confirm:
        send_ai_confirm(case_id)
