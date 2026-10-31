"""Actors of the ``enrich`` queue, consumed by worker-default (architecture.md §7, §9)."""

from __future__ import annotations

import uuid
from functools import cache

import dramatiq
from dramatiq.middleware import TimeLimitExceeded
from redis import Redis

from kalkan.config import ConfigError, get_settings
from kalkan.db.base import utcnow
from kalkan.db.session import get_database
from kalkan.enrichment import service
from kalkan.enrichment.cloudflare import load_ranges, refresh_ranges
from kalkan.enrichment.dns import DnsPythonResolver
from kalkan.enrichment.rdap import RdapClient, RedisTokenBucket
from kalkan.enrichment.webrisk import WebRiskClient
from kalkan.logging import get_logger
from kalkan.storage.gcs import ObjectStore
from kalkan.workers.broker import final_attempt
from kalkan.workers.scanning import http_precheck

QUEUE = "enrich"
MAX_RETRIES = 5
# DNS (5 x 3 s), Team Cymru, three RDAP queries (6 s each, up to 10 s waiting for a slot)
# and Web Risk fit; a slower run is cut off and retried (K67).
TIME_LIMIT_MS = 120_000

log = get_logger(__name__)


@cache
def _redis() -> Redis:
    url = get_settings().redis_url
    if url is None:  # pragma: no cover - worker-default requires REDIS_URL
        raise ConfigError("REDIS_URL is required")
    return Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)


@cache
def sources() -> service.Sources:
    """The enrichment sources of this worker process."""
    settings = get_settings()
    redis = _redis()
    store = None
    if settings.gcs_evidence_bucket is not None:
        store = ObjectStore.from_settings(settings)
        if settings.storage_emulator_host:  # local only: the emulator starts empty
            store.ensure_buckets(settings, settings.gcs_evidence_bucket)
    webrisk = None
    if settings.webrisk_api_key is not None:
        webrisk = WebRiskClient(settings.webrisk_api_key.get_secret_value())
    return service.Sources(
        resolver=DnsPythonResolver(),
        rdap=RdapClient(redis, RedisTokenBucket(redis)),
        cloudflare_ranges=lambda: load_ranges(redis),
        webrisk=webrisk,
        store=store,
        evidence_bucket=settings.gcs_evidence_bucket,
        cache=redis,
    )


@dramatiq.actor(queue_name=QUEUE, max_retries=MAX_RETRIES, time_limit=TIME_LIMIT_MS)
def enrich_domain(domain_id: str) -> None:
    """Enrich the domain, then start the HTTP pre-check of its ``detected`` cases.

    On the last attempt any failure, a time-out included, is recorded as ``job_failed`` and
    the detected cases go on to the pre-check with what is known.
    """
    final = final_attempt(MAX_RETRIES)
    database = get_database()
    try:
        result = service.enrich_domain(
            database, uuid.UUID(domain_id), sources(), now=utcnow, final_attempt=final
        )
        case_ids = result.precheck_case_ids
    except (Exception, TimeLimitExceeded) as exc:
        if not final:
            raise
        log.exception("enrich.given_up", domain_id=domain_id)
        error = {"error": f"{type(exc).__name__}: {exc}"}
        case_ids = service.give_up(database, uuid.UUID(domain_id), error, now=utcnow())
    for case_id in case_ids:
        http_precheck.send(str(case_id))


@dramatiq.actor(queue_name=QUEUE, max_retries=3, time_limit=60_000)
def refresh_cloudflare_ranges() -> None:
    count = refresh_ranges(_redis())
    log.info("enrich.cloudflare_ranges_refreshed", ranges=count)
