"""Writes what discovery found: ``domains``, ``domain_sightings`` and ``brand_matches`` (§8).

Runs as ``kalkan_worker``. The caller commits and only then queues ``admit_match`` for the
returned match ids, so a worker never sees a match that was rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kalkan.db.base import new_id
from kalkan.db.models import BrandMatch, Domain, DomainSighting
from kalkan.discovery.matcher import MatchResult
from kalkan.discovery.metrics import metrics
from kalkan.discovery.normalize import HostView
from kalkan.domain.enums import SightingSource

# Score of a candidate someone pointed at: an operator, or a fake login page's button
# (K59, K71).
MANUAL_SCORE = 100


@dataclass(frozen=True)
class Sighting:
    source: SightingSource
    seen_at: datetime
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Recorded:
    domain_id: uuid.UUID
    new_domain: bool
    new_match_ids: list[uuid.UUID]


def upsert_domain(session: Session, view: HostView, seen_at: datetime) -> tuple[uuid.UUID, bool]:
    """The domain's id, and whether this call created it."""
    created = session.scalar(
        insert(Domain)
        .values(
            id=new_id(),
            name=view.host,
            unicode_name=view.unicode_host,
            registered_domain=view.registered_domain,
            # The top-level domain itself; the public suffix may be longer (com.pl, pages.dev).
            tld=view.host.rsplit(".", 1)[-1],
            first_seen_at=seen_at,
        )
        .on_conflict_do_nothing(index_elements=[Domain.name])
        .returning(Domain.id)
    )
    if created is not None:
        return created, True
    existing = session.scalar(select(Domain.id).where(Domain.name == view.host))
    if existing is None:  # pragma: no cover - the row exists, the insert just lost a race
        raise RuntimeError(f"domain {view.host} vanished")
    return existing, False


def record_discovery(
    session: Session,
    view: HostView,
    matches: Sequence[MatchResult],
    sighting: Sighting,
    *,
    sight_known: bool = True,
) -> Recorded:
    """Record ``matches`` of ``view``; returns the ids of matches that are new.

    ``sight_known=False`` skips the sighting when neither the domain nor any match is new,
    for sources that see the same names every day (dnstwist, CZDS).
    """
    domain_id, new_domain = upsert_domain(session, view, sighting.seen_at)
    new_ids: list[uuid.UUID] = []
    for match in matches:
        match_id = session.scalar(
            insert(BrandMatch)
            .values(
                id=new_id(),
                customer_id=match.customer_id,
                brand_id=match.brand_id,
                domain_id=domain_id,
                score=match.score,
                reasons=[str(reason) for reason in match.reasons],
                first_matched_at=sighting.seen_at,
            )
            .on_conflict_do_nothing(index_elements=[BrandMatch.brand_id, BrandMatch.domain_id])
            .returning(BrandMatch.id)
        )
        if match_id is not None:
            new_ids.append(match_id)
            observed = metrics()
            observed.candidates.labels(source=sighting.source).inc()
            for reason in match.reasons:
                observed.matches.labels(source=sighting.source, reason=reason).inc()
    if sight_known or new_domain or new_ids:
        session.add(
            DomainSighting(
                domain_id=domain_id,
                source=sighting.source,
                seen_at=sighting.seen_at,
                detail=sighting.detail,
            )
        )
    return Recorded(domain_id=domain_id, new_domain=new_domain, new_match_ids=new_ids)
