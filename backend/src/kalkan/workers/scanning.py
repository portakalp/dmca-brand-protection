"""Actors of the ``precheck`` queue, consumed by worker-default (architecture.md §7, §10).

Besides the pre-check: the brand's official favicons, and its reference screenshots, which
the browser stage captures (``browser_capture`` is sent by name; K30).
"""

from __future__ import annotations

import uuid
from functools import cache

import dramatiq
from dramatiq.middleware import TimeLimitExceeded
from redis import Redis
from sqlalchemy import select

from kalkan.config import ConfigError, get_settings
from kalkan.db.base import utcnow
from kalkan.db.models import OfficialDomain
from kalkan.db.session import get_database
from kalkan.logging import get_logger
from kalkan.net.safe_http import SafeHttpClient
from kalkan.scanning.brand_assets import UnknownBrandError
from kalkan.scanning.brand_assets import refresh_brand_favicons as refresh_favicons
from kalkan.scanning.capture_jobs import issue_case_capture
from kalkan.scanning.precheck import Route, record_failure, run_precheck
from kalkan.scanning.references import (
    NoReferenceError,
    issue_reference_capture,
    reference_brands,
)
from kalkan.storage.gcs import ObjectStore
from kalkan.workers.broker import final_attempt

QUEUE = "precheck"
MAX_RETRIES = 3

log = get_logger(__name__)


@cache
def http_client() -> SafeHttpClient:
    """This worker process's SSRF-safe client (§10)."""
    return SafeHttpClient(allow_cidrs=get_settings().ssrf_allow_cidrs)


@cache
def capture_redis() -> Redis:
    """Holds the capture job records and pending markers (``capture_contract``)."""
    url = get_settings().redis_url
    if url is None:  # pragma: no cover - worker-default requires REDIS_URL
        raise ConfigError("REDIS_URL is required")
    return Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)


@dramatiq.actor(queue_name=QUEUE, max_retries=MAX_RETRIES, time_limit=45_000)
def http_precheck(case_id: str) -> None:
    """Stage 1 for a ``detected`` case; suspicious ones go on to the browser stage.

    On the last attempt any failure, a time-out included, is recorded as ``job_failed`` so the
    reconciler leaves the case to the ops panel (§7).
    """
    from kalkan.workers.discovery import queue_admit

    final = final_attempt(MAX_RETRIES)
    try:
        result = run_precheck(get_database(), http_client(), uuid.UUID(case_id), now=utcnow)
    except (Exception, TimeLimitExceeded) as exc:
        if not final:
            raise
        log.exception("precheck.given_up", case_id=case_id)
        error = f"{type(exc).__name__}: {exc}"
        record_failure(get_database(), uuid.UUID(case_id), error, at=utcnow())
        return
    if result.skipped or result.decision is None:
        return
    if result.decision.route is Route.CAPTURE:
        try:
            issue_case_capture(get_database(), capture_redis(), uuid.UUID(case_id), now=utcnow())
        except Exception:
            # The pre-check is stored and a retry would skip it; the reconciler issues the
            # capture after 2 hours (§7).
            log.exception("precheck.capture_not_issued", case_id=case_id)
    queue_admit(result.new_match_ids)


@dramatiq.actor(queue_name=QUEUE, max_retries=3, time_limit=60_000)
def refresh_brand_favicons(brand_id: str, domain: str | None = None) -> None:
    """Fetch and store a brand's official favicons (§10, §12): one domain per message.

    Without ``domain`` the job queues one message per official domain of the brand.
    """
    if domain is None:
        with get_database().worker_session() as session:
            domains = sorted(
                set(
                    session.scalars(
                        select(OfficialDomain.domain).where(
                            OfficialDomain.brand_id == uuid.UUID(brand_id)
                        )
                    )
                )
            )
        for name in domains:
            refresh_brand_favicons.send(brand_id, name)
        return
    settings = get_settings()
    if settings.gcs_evidence_bucket is None:
        raise ConfigError("GCS_EVIDENCE_BUCKET is required to store brand favicons")
    try:
        added = refresh_favicons(
            get_database(),
            http_client(),
            ObjectStore.from_settings(settings),
            settings.gcs_evidence_bucket,
            uuid.UUID(brand_id),
            now=utcnow,
            domains=[domain],
        )
    except UnknownBrandError:
        log.info("brand.favicons_skipped", brand_id=brand_id, reason="no such brand")
        return
    log.info("brand.favicons_refreshed", brand_id=brand_id, domain=domain, added=added)
    if added:
        from kalkan.workers.signals import hash_brand_assets

        hash_brand_assets.send(brand_id)


@dramatiq.actor(queue_name=QUEUE, max_retries=3, time_limit=60_000)
def refresh_brand_references(brand_id: str | None = None) -> None:
    """Issue a reference capture of the brand's primary official domain (§10).

    Without ``brand_id`` (the weekly job) one message per active, non-demo brand.
    """
    if brand_id is None:
        for brand in reference_brands(get_database()):
            refresh_brand_references.send(str(brand))
        return
    try:
        job, domain = issue_reference_capture(
            get_database(), capture_redis(), uuid.UUID(brand_id), now=utcnow()
        )
    except NoReferenceError as exc:
        log.info("brand.references_skipped", brand_id=brand_id, reason=str(exc))
        return
    log.info("brand.references_queued", brand_id=brand_id, domain=domain, job_id=str(job.job_id))
