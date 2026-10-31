"""Loads the brands the matcher watches and keeps a compiled matcher per process (§8).

Demo customers are never matched (K46); suspended customers are not watched.
The compiled matcher is reused for 60 seconds; after that a fingerprint query (a hash of the
brand, customer and permutation rows that matter) decides whether a rebuild is needed. If the
database is unreachable, the last matcher keeps serving until the next check.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from kalkan.db.models import Brand, BrandPermutation, Customer
from kalkan.db.session import Database
from kalkan.discovery.matcher import BrandProfile, Matcher
from kalkan.domain.enums import CustomerStatus
from kalkan.logging import get_logger

CACHE_SECONDS = 60.0

log = get_logger(__name__)

# Hashes of the rows, so changes made outside the ORM (which would not touch updated_at) count.
_FINGERPRINT = text(
    """
    SELECT
      (SELECT md5(coalesce(string_agg(
          concat_ws('|', id, active, name, array_to_string(keywords, ','),
                    array_to_string(variants, ',')), ';' ORDER BY id), ''))
       FROM brands),
      (SELECT md5(coalesce(string_agg(concat_ws('|', id, is_demo, status), ';' ORDER BY id), ''))
       FROM customers),
      (SELECT count(*) || ':' || coalesce(sum(hashtext(brand_id::text || domain)), 0)
       FROM brand_permutations)
    """
)
# Without any matcher yet, a failed load is retried after this many seconds.
RETRY_SECONDS = 5.0


class MatcherUnavailableError(RuntimeError):
    """The brand catalogue could not be loaded and there is no earlier matcher."""


def fingerprint(session: Session) -> tuple[Any, ...]:
    return tuple(session.execute(_FINGERPRINT).one())


def load_profiles(session: Session) -> list[BrandProfile]:
    brands = session.execute(
        select(Brand.id, Brand.customer_id, Brand.name, Brand.keywords, Brand.variants)
        .join(Customer, Customer.id == Brand.customer_id)
        .where(
            Brand.active.is_(True),
            Customer.is_demo.is_(False),
            Customer.status != CustomerStatus.SUSPENDED,
        )
        .order_by(Brand.id)
    ).all()
    permutations: dict[Any, set[str]] = defaultdict(set)
    if brands:
        rows = session.execute(
            select(BrandPermutation.brand_id, BrandPermutation.domain).where(
                BrandPermutation.brand_id.in_([brand.id for brand in brands])
            )
        )
        for brand_id, domain in rows:
            permutations[brand_id].add(domain)
    return [
        BrandProfile(
            brand_id=brand.id,
            customer_id=brand.customer_id,
            name=brand.name,
            keywords=tuple(brand.keywords),
            variants=tuple(brand.variants),
            permutations=frozenset(permutations[brand.id]),
        )
        for brand in brands
    ]


def profile_for(session: Session, brand: Brand) -> BrandProfile:
    """One brand's matcher profile, with its dnstwist permutations."""
    permutations = frozenset(
        session.scalars(
            select(BrandPermutation.domain).where(BrandPermutation.brand_id == brand.id)
        )
    )
    return BrandProfile(
        brand_id=brand.id,
        customer_id=brand.customer_id,
        name=brand.name,
        keywords=tuple(brand.keywords),
        variants=tuple(brand.variants),
        permutations=permutations,
    )


class MatcherCache:
    """A compiled :class:`Matcher` that is rebuilt when the brand catalogue changes."""

    def __init__(
        self,
        database: Database,
        *,
        ttl: float = CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._database = database
        self._ttl = ttl
        self._clock = clock
        self._matcher: Matcher | None = None
        self._fingerprint: tuple[Any, ...] | None = None
        self._checked_at = 0.0
        self._failed_at: float | None = None

    def get(self) -> Matcher:
        now = self._clock()
        if self._matcher is not None and now - self._checked_at < self._ttl:
            return self._matcher
        if (
            self._matcher is None
            and self._failed_at is not None
            and now - self._failed_at < RETRY_SECONDS
        ):
            raise MatcherUnavailableError("the brand catalogue is not loaded yet")
        try:
            with self._database.worker_session() as session:
                current = fingerprint(session)
                if self._matcher is None or current != self._fingerprint:
                    profiles = load_profiles(session)
                    self._matcher = Matcher(profiles)
                    self._fingerprint = current
                    log.info("matcher.compiled", brands=len(profiles))
        except SQLAlchemyError as exc:
            if self._matcher is None:
                self._failed_at = now
                raise MatcherUnavailableError("cannot load the brand catalogue") from exc
            log.warning("matcher.refresh_failed", error=type(exc).__name__)
        self._checked_at = now
        return self._matcher
