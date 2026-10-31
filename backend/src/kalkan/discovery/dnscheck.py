"""Daily DNS check of brand permutations (architecture.md §7, §8).

A permutation counts as registered when it has an NS or an A record. Registered ones become
candidates (``domain_sightings`` source=dnstwist and ``brand_matches``). The check runs 20
lookups at a time and at most ``RATE_PER_SECOND`` lookups per second (K59).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import dns.asyncresolver
import dns.exception
import dns.resolver
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from kalkan.db.base import utcnow
from kalkan.db.models import BrandPermutation
from kalkan.db.session import Database
from kalkan.discovery.catalog import MatcherCache
from kalkan.discovery.matcher import Matcher
from kalkan.discovery.metrics import metrics
from kalkan.discovery.normalize import view_host
from kalkan.discovery.permutations import watched_brands
from kalkan.discovery.record import Sighting, record_discovery
from kalkan.domain.enums import SightingSource
from kalkan.domain.hostnames import InvalidHostnameError
from kalkan.logging import get_logger

CONCURRENCY = 20
RATE_PER_SECOND = 50.0
TIMEOUT_SECONDS = 3.0
WRITE_BATCH = 500

log = get_logger(__name__)


class Resolver(Protocol):
    async def registered(self, domain: str) -> bool | None:
        """True with an NS or A record, False when the name does not exist, None if unknown."""
        ...


class DnsResolver:
    def __init__(self, timeout: float = TIMEOUT_SECONDS) -> None:
        self._resolver = dns.asyncresolver.Resolver()
        self._resolver.lifetime = timeout

    async def registered(self, domain: str) -> bool | None:
        unknown = False
        for rdtype in ("NS", "A"):
            try:
                await self._resolver.resolve(domain, rdtype)
            except dns.resolver.NXDOMAIN:
                return False
            except dns.resolver.NoAnswer:
                continue
            except (dns.resolver.NoNameservers, dns.exception.Timeout):
                unknown = True
                continue
            return True
        return None if unknown else False


class RateLimiter:
    """Spaces calls evenly at ``rate`` per second."""

    def __init__(self, rate: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = 1.0 / rate
        self._clock = clock
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            delay = self._next - now
            self._next = max(now, self._next) + self._interval
        if delay > 0:
            await asyncio.sleep(delay)


async def resolve_all(
    domains: Sequence[str],
    resolver: Resolver,
    *,
    concurrency: int = CONCURRENCY,
    rate: float = RATE_PER_SECOND,
) -> dict[str, bool | None]:
    semaphore = asyncio.Semaphore(concurrency)
    limiter = RateLimiter(rate)

    async def one(domain: str) -> tuple[str, bool | None]:
        async with semaphore:
            await limiter.wait()
            try:
                return domain, await resolver.registered(domain)
            except Exception:
                log.warning("dnscheck.lookup_failed", domain=domain, exc_info=True)
                return domain, None

    return dict(await asyncio.gather(*(one(domain) for domain in domains)))


@dataclass(frozen=True)
class CheckResult:
    checked: int
    registered: int
    unknown: int
    new_match_ids: list[uuid.UUID]


def _pending(session: Session) -> list[tuple[uuid.UUID, str, bool | None]]:
    brands = watched_brands(session)
    if not brands:
        return []
    return [
        (row.id, row.domain, row.registered)
        for row in session.execute(
            select(BrandPermutation.id, BrandPermutation.domain, BrandPermutation.registered)
            .where(BrandPermutation.brand_id.in_(brands))
            .order_by(BrandPermutation.checked_at.asc().nulls_first(), BrandPermutation.domain)
        )
    ]


def check_permutations(
    database: Database,
    resolver: Resolver,
    matchers: MatcherCache,
    queue_admit: Callable[[Sequence[uuid.UUID]], None],
    *,
    now: Callable[[], datetime] = utcnow,
    resolve: Callable[..., Coroutine[Any, Any, dict[str, bool | None]]] = resolve_all,
    chunk: int = WRITE_BATCH,
) -> CheckResult:
    """Check every watched permutation, least recently checked first.

    Each chunk is resolved, written and committed before the next one starts, so a run cut
    short by the time limit keeps its progress and the next run continues with older rows.
    """
    with database.worker_session() as session:
        pending = _pending(session)
    matcher = matchers.get()

    observed = metrics()
    registered = unknown = 0
    new_ids: list[uuid.UUID] = []
    for start in range(0, len(pending), chunk):
        batch = pending[start : start + chunk]
        results = asyncio.run(resolve(sorted({domain for _, domain, _ in batch}), resolver))
        checked_at = now()
        newly_registered: set[str] = set()
        with database.worker_session() as session:
            for permutation_id, domain, previous in batch:
                result = results.get(domain)
                observed.permutation_checks.labels(
                    result={True: "registered", False: "unregistered", None: "unknown"}[result]
                ).inc()
                if result is None:
                    unknown += 1
                    continue
                if result:
                    registered += 1
                session.execute(
                    update(BrandPermutation)
                    .where(BrandPermutation.id == permutation_id)
                    .values(registered=result, checked_at=checked_at)
                )
                if result and previous is not True:
                    newly_registered.add(domain)
            batch_ids = _record_registered(session, newly_registered, matcher, checked_at)
        queue_admit(batch_ids)
        new_ids += batch_ids
    log.info(
        "dnscheck.done",
        checked=len(pending),
        registered=registered,
        unknown=unknown,
        new_matches=len(new_ids),
    )
    return CheckResult(len(pending), registered, unknown, new_ids)


def _record_registered(
    session: Session, domains: set[str], matcher: Matcher, seen_at: datetime
) -> list[uuid.UUID]:
    new_ids: list[uuid.UUID] = []
    for domain in sorted(domains):
        try:
            view = view_host(domain)
        except InvalidHostnameError:
            continue
        matches = matcher.match(view)
        if not matches:
            continue
        recorded = record_discovery(
            session,
            view,
            matches,
            Sighting(SightingSource.DNSTWIST, seen_at, {"check": "ns_or_a"}),
            sight_known=False,
        )
        new_ids += recorded.new_match_ids
    return new_ids
