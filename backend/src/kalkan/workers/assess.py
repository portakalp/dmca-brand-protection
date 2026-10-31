"""The ``ai`` queue, consumed by worker-ai (architecture.md §7, §13).

- ``ai_triage(case_id)``: sent by ``compute_signals`` and the reconciler; writes the triage
  verdict, then queues ``decide``.
- ``ai_confirm(case_id)``: sent by ``decide`` and the reconciler for a case whose
  decision is ``confirm``; writes the confirmation verdict, then queues ``decide``.

A case without a verdict goes to ``in_review`` (``ai_unavailable``, ``ai_budget_exhausted``);
on the last attempt any other failure, a time-out included, is recorded as ``job_failed``.
The time limit covers 3 API attempts of up to 60 seconds and their back-off (K124).
"""

from __future__ import annotations

import uuid
from functools import cache

import dramatiq
from dramatiq.middleware import TimeLimitExceeded

from kalkan.ai.assess import AssessStatus, Stage, assess
from kalkan.ai.client import AiClient, DatabaseRecorder
from kalkan.ai.queue import AI_CONFIRM_ACTOR, AI_QUEUE, AI_TRIAGE_ACTOR, send_decide
from kalkan.config import ConfigError, get_settings
from kalkan.db.base import utcnow
from kalkan.db.session import get_database
from kalkan.logging import get_logger
from kalkan.signals.compute import record_failure
from kalkan.storage.gcs import ObjectStore
from kalkan.workers.broker import final_attempt

MAX_RETRIES = 3
TIME_LIMIT_MS = 300_000

log = get_logger(__name__)


@cache
def ai_client() -> AiClient:
    settings = get_settings()
    if settings.anthropic_api_key is None:  # pragma: no cover - require_for checked it
        raise ConfigError("ANTHROPIC_API_KEY is required")
    return AiClient.create(
        settings.anthropic_api_key.get_secret_value(),
        prices=settings.ai_prices,
        monthly_budget_usd=settings.ai_monthly_budget_usd,
        recorder=DatabaseRecorder(get_database()),
    )


@cache
def object_store() -> ObjectStore:
    return ObjectStore.from_settings(get_settings())


def run_stage(case_id: str, stage: Stage, job: str) -> None:
    final = final_attempt(MAX_RETRIES)
    try:
        status = assess(
            get_database(), object_store(), ai_client(), get_settings(), uuid.UUID(case_id), stage
        )
    except (Exception, TimeLimitExceeded) as exc:
        if not final:
            raise
        log.exception("ai.given_up", case_id=case_id, stage=stage.value)
        record_failure(
            get_database(), uuid.UUID(case_id), f"{type(exc).__name__}: {exc}", at=utcnow(), job=job
        )
        return
    if status is AssessStatus.DONE:
        send_decide(case_id)


@dramatiq.actor(
    actor_name=AI_TRIAGE_ACTOR,
    queue_name=AI_QUEUE,
    max_retries=MAX_RETRIES,
    time_limit=TIME_LIMIT_MS,
)
def ai_triage(case_id: str) -> None:
    run_stage(case_id, Stage.TRIAGE, AI_TRIAGE_ACTOR)


@dramatiq.actor(
    actor_name=AI_CONFIRM_ACTOR,
    queue_name=AI_QUEUE,
    max_retries=MAX_RETRIES,
    time_limit=TIME_LIMIT_MS,
)
def ai_confirm(case_id: str) -> None:
    run_stage(case_id, Stage.CONFIRM, AI_CONFIRM_ACTOR)
