"""The ``persist`` queue, consumed by worker-default (architecture.md §7, §10, §11).

``persist_capture(job_id)`` is sent by worker-browser, which is not trusted (K82): the job
id is the only thing taken from the message, and a job without a record is dropped. Only
worker-default mounts the hand-off volume, so the janitor that clears old job folders runs
here too (sent daily by the scheduler).
"""

from __future__ import annotations

import time
import uuid
from functools import cache

import dramatiq
from dramatiq.middleware import TimeLimitExceeded

from kalkan.config import ConfigError, get_settings
from kalkan.db.base import utcnow
from kalkan.db.session import get_database
from kalkan.logging import get_logger
from kalkan.reports import handoff
from kalkan.scanning import persist
from kalkan.scanning.capture_contract import ARTIFACTS_ROOT, PERSIST_ACTOR, PERSIST_QUEUE
from kalkan.storage.gcs import ObjectStore
from kalkan.workers.broker import final_attempt
from kalkan.workers.scanning import capture_redis
from kalkan.workers.signals import compute_signals, hash_brand_assets

MAX_RETRIES = 5
# The hand-off folder; tests point it elsewhere.
ROOT = ARTIFACTS_ROOT

log = get_logger(__name__)


@cache
def evidence_store() -> tuple[ObjectStore, str]:
    settings = get_settings()
    if settings.gcs_evidence_bucket is None:
        raise ConfigError("GCS_EVIDENCE_BUCKET is required to store captures")
    store = ObjectStore.from_settings(settings)
    if settings.storage_emulator_host:  # local only: the emulator starts empty
        store.ensure_buckets(settings, settings.gcs_evidence_bucket)
    return store, settings.gcs_evidence_bucket


@dramatiq.actor(
    actor_name=PERSIST_ACTOR, queue_name=PERSIST_QUEUE, max_retries=MAX_RETRIES, time_limit=120_000
)
def persist_capture(job_id: str) -> None:
    """Store a finished capture (§10, §11); on the last failed attempt the case goes to review.

    A configuration error is not the capture's fault: it is raised, never given up on.
    """
    from kalkan.workers.discovery import queue_admit

    try:
        job = uuid.UUID(str(job_id))
    except ValueError:
        log.warning("persist.bad_job_id")
        return
    final = final_attempt(MAX_RETRIES)
    try:
        store, bucket = evidence_store()
        result = persist.persist_capture(
            get_database(), capture_redis(), store, bucket, job, now=utcnow, root=ROOT
        )
    except ConfigError:
        raise
    except (Exception, TimeLimitExceeded) as exc:
        if not final:
            raise
        log.exception("persist.given_up", job_id=str(job))
        error = f"{type(exc).__name__}: {exc}"
        persist.give_up(get_database(), capture_redis(), job, error, at=utcnow(), root=ROOT)
        return
    if result.compute_signals and result.case_id is not None:
        compute_signals.send(str(result.case_id))
    if result.hashed_brand_id is not None:
        hash_brand_assets.send(str(result.hashed_brand_id))
    queue_admit(result.new_match_ids)


@dramatiq.actor(queue_name=PERSIST_QUEUE, max_retries=0, time_limit=600_000)
def clean_capture_folders() -> None:
    """Delete job and report folders older than a day that nothing removed."""
    removed = persist.remove_stale_job_dirs(ROOT, now=time.time())
    reports = handoff.remove_stale(ROOT / handoff.ROOT.name, now=time.time())
    log.info("persist.folders_cleaned", removed=removed, reports=reports)
