"""Actors of the ``discovery`` queue, consumed by worker-default (architecture.md §7, §8).

Other processes (ct-consumer, scheduler, CLI) send to these actors after
:func:`kalkan.workers.broker.setup_broker`; only worker-default consumes them.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import dramatiq
from redis import Redis
from sqlalchemy import select

from kalkan.config import ConfigError, get_settings
from kalkan.db.base import utcnow
from kalkan.db.models import Brand
from kalkan.db.session import get_database
from kalkan.discovery.catalog import MatcherCache
from kalkan.discovery.czds import run_czds_scan
from kalkan.discovery.dnscheck import DnsResolver, check_permutations
from kalkan.discovery.permutations import refresh_brand_permutations, watched_brands
from kalkan.enrichment.admission import admit, apply_whitelist
from kalkan.logging import get_logger
from kalkan.workers.enrichment import enrich_domain

QUEUE = "discovery"
HOUR_MS = 3_600_000

log = get_logger(__name__)


@dramatiq.actor(queue_name=QUEUE, max_retries=3, time_limit=30_000)
def admit_match(match_id: str) -> None:
    """Whitelist check, then a ``detected`` case; enrichment or the pre-check comes next (§9)."""
    with get_database().worker_session() as session:
        result = admit(
            session, uuid.UUID(match_id), now=utcnow(), prefix=get_settings().case_prefix
        )
    if result.follow_up and result.domain_id is not None:
        # enrich_domain queues the HTTP pre-check of the domain's detected cases.
        enrich_domain.send(str(result.domain_id))


@dramatiq.actor(queue_name=QUEUE, max_retries=5, time_limit=60_000)
def whitelist_changed(customer_id: str) -> None:
    """Dismiss the customer's open cases that the whitelist now covers (§9)."""
    with get_database().worker_session() as session:
        apply_whitelist(session, uuid.UUID(customer_id), now=utcnow())


def queue_admit(match_ids: Sequence[uuid.UUID]) -> None:
    """Queue ``admit_match`` for new matches; call only after their transaction committed."""
    for match_id in match_ids:
        admit_match.send(str(match_id))


@dramatiq.actor(queue_name=QUEUE, max_retries=3, time_limit=HOUR_MS)
def generate_permutations(brand_id: str | None = None) -> None:
    """Regenerate dnstwist permutations of one brand, or of every watched brand."""
    database = get_database()
    with database.worker_session() as session:
        ids = [uuid.UUID(brand_id)] if brand_id else watched_brands(session)
    for current in ids:
        with database.worker_session() as session:
            brand = session.scalar(select(Brand).where(Brand.id == current))
            if brand is None:
                continue
            result = refresh_brand_permutations(session, brand)
        log.info(
            "discovery.permutations_refreshed",
            brand_id=str(current),
            added=result.added,
            removed=result.removed,
            total=result.total,
        )


@dramatiq.actor(queue_name=QUEUE, max_retries=0, time_limit=4 * HOUR_MS)
def check_permutation_dns() -> None:
    """The daily DNS check of permutations; registered ones become candidates."""
    database = get_database()
    check_permutations(database, DnsResolver(), MatcherCache(database), queue_admit)


@dramatiq.actor(queue_name=QUEUE, max_retries=0, time_limit=8 * HOUR_MS)
def czds_scan() -> None:
    """The daily CZDS zone scan. No retries: a zone may be downloaded once a day."""
    settings = get_settings()
    if settings.redis_url is None:  # pragma: no cover - worker-default requires REDIS_URL
        raise ConfigError("REDIS_URL is required")
    ledger = Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5)
    try:
        run_czds_scan(settings, get_database(), queue_admit, ledger)
    finally:
        ledger.close()
