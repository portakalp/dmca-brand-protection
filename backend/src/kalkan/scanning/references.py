"""Reference screenshots of a brand's official site (architecture.md §7, §10, §11).

The browser captures the brand's primary official domain (the oldest ``primary`` one) with
the same actor, proxy and profiles as candidate sites; ``persist_capture`` keeps the ``ok``
viewport screenshots as ``reference_desktop`` and ``reference_mobile`` brand assets. Their
perceptual hashes are computed by the signals stage. Jobs are issued when a primary
official domain is added, weekly, and from the CLI; demo and inactive brands get none
(K46). A reference job has no case, so its failures are only logged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from redis import Redis
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from kalkan.db.models import Brand, Customer, OfficialDomain
from kalkan.db.session import Database
from kalkan.domain.enums import BrandAssetKind, OfficialDomainKind, Profile
from kalkan.logging import get_logger
from kalkan.scanning.capture_contract import CaptureJob, Purpose
from kalkan.scanning.capture_jobs import SendJob, country_for, issue_capture, send_job

ASSET_KIND = {
    Profile.DESKTOP: BrandAssetKind.REFERENCE_DESKTOP,
    Profile.MOBILE: BrandAssetKind.REFERENCE_MOBILE,
}

log = get_logger(__name__)


class NoReferenceError(LookupError):
    """The brand gets no reference job; the message says why."""


@dataclass(frozen=True)
class ReferenceTarget:
    brand_id: uuid.UUID
    customer_id: uuid.UUID
    domain: str
    country: str | None


def _primary_domain(session: Session, brand_id: uuid.UUID) -> str | None:
    return session.scalar(
        select(OfficialDomain.domain)
        .where(
            OfficialDomain.brand_id == brand_id,
            OfficialDomain.kind == OfficialDomainKind.PRIMARY,
        )
        .order_by(OfficialDomain.created_at, OfficialDomain.id)
        .limit(1)
    )


def reference_target(session: Session, brand_id: uuid.UUID) -> ReferenceTarget:
    brand = session.get(Brand, brand_id)
    if brand is None:
        raise NoReferenceError(f"no brand with id {brand_id}")
    customer = session.get(Customer, brand.customer_id)
    if customer is None or customer.is_demo:
        raise NoReferenceError("the demo workspace gets no reference screenshots")
    if not brand.active:
        raise NoReferenceError("the brand is inactive")
    domain = _primary_domain(session, brand_id)
    if domain is None:
        raise NoReferenceError("the brand has no primary official domain")
    return ReferenceTarget(brand.id, brand.customer_id, domain, country_for(brand.target_markets))


def reference_brands(database: Database) -> list[uuid.UUID]:
    """Active, non-demo brands with a primary official domain (the weekly refresh)."""
    primary = exists().where(
        OfficialDomain.brand_id == Brand.id,
        OfficialDomain.kind == OfficialDomainKind.PRIMARY,
    )
    with database.worker_session() as session:
        return list(
            session.scalars(
                select(Brand.id)
                .join(Customer, Customer.id == Brand.customer_id)
                .where(Customer.is_demo.is_(False), Brand.active.is_(True), primary)
                .order_by(Brand.created_at, Brand.id)
            )
        )


def issue_reference_capture(
    database: Database,
    redis: Redis,
    brand_id: uuid.UUID,
    *,
    now: datetime,
    send: SendJob = send_job,
) -> tuple[CaptureJob, str]:
    """Issue one reference job for the brand's primary official domain.

    Raises :class:`NoReferenceError` when the brand gets none.
    """
    with database.worker_session() as session:
        target = reference_target(session, brand_id)
    job = issue_capture(
        redis,
        purpose=Purpose.REFERENCE,
        customer_id=target.customer_id,
        brand_id=target.brand_id,
        case_id=None,
        urls=[f"https://{target.domain}/", f"http://{target.domain}/"],
        country=target.country,
        now=now,
        send=send,
    )
    if job is None:  # pragma: no cover - only case jobs have a pending marker
        raise RuntimeError("a reference job was not issued")
    return job, target.domain
