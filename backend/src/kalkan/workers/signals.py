"""The ``signals`` queue, consumed by worker-default (architecture.md §7, §12).

- ``compute_signals(case_id)``: sent by persist_capture after a stored capture; writes the
  case's signals and queues ``ai_triage``.
- ``hash_brand_assets(brand_id)``: sent when a brand asset is stored; fills its pHash and dHash.
"""

from __future__ import annotations

import uuid

import dramatiq
from dramatiq.middleware import TimeLimitExceeded

from kalkan.ai.queue import send_ai_triage
from kalkan.db.base import utcnow
from kalkan.db.session import get_database
from kalkan.logging import get_logger
from kalkan.scanning.capture_contract import COMPUTE_SIGNALS_ACTOR, SIGNALS_QUEUE
from kalkan.signals import assets, compute
from kalkan.workers.broker import final_attempt

MAX_RETRIES = 3

log = get_logger(__name__)


@dramatiq.actor(
    actor_name=COMPUTE_SIGNALS_ACTOR,
    queue_name=SIGNALS_QUEUE,
    max_retries=MAX_RETRIES,
    time_limit=60_000,
)
def compute_signals(case_id: str) -> None:
    """Signals of a ``detected`` case with a stored capture (§12), then ``ai_triage``.

    On the last attempt any failure, a time-out included, is recorded as ``job_failed`` (§7).
    """
    from kalkan.workers.persist import evidence_store

    final = final_attempt(MAX_RETRIES)
    try:
        store, _ = evidence_store()
        status = compute.compute_signals(get_database(), store, uuid.UUID(case_id))
    except (Exception, TimeLimitExceeded) as exc:
        if not final:
            raise
        log.exception("signals.given_up", case_id=case_id)
        error = f"{type(exc).__name__}: {exc}"
        compute.record_failure(get_database(), uuid.UUID(case_id), error, at=utcnow())
        return
    if status is compute.ComputeStatus.COMPUTED:
        send_ai_triage(case_id)


@dramatiq.actor(queue_name=SIGNALS_QUEUE, max_retries=3, time_limit=120_000)
def hash_brand_assets(brand_id: str) -> None:
    """pHash and dHash of the brand's assets that have none yet (§5, §12)."""
    from kalkan.workers.persist import evidence_store

    store, _ = evidence_store()
    hashed = assets.hash_brand_assets(get_database(), store, uuid.UUID(brand_id))
    log.info("brand.assets_hashed", brand_id=brand_id, hashed=hashed)
