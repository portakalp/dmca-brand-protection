"""Issuing browser capture jobs (architecture.md §7, §10, §24; contract in ``capture_contract``).

Only trusted processes issue jobs: worker-default (after the pre-check, and for reference
screenshots) and the scheduler (the reconciler). :func:`issue_capture` fixes the scan ids,
keeps the :class:`CaptureRecord` in Redis where the browser cannot reach it, holds one
pending marker per case so a job is never issued twice, and only then sends the message.
The message goes to ``browser_capture`` by name: the issuing processes do not declare the
browser's actor (K30).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

import dramatiq
from pydantic import ValidationError
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from kalkan.db.base import new_id
from kalkan.db.models import Brand, Case, Customer, Domain, DomainSighting, Scan
from kalkan.db.session import Database
from kalkan.domain.enums import CaseState, Profile, ScanStage, SightingSource
from kalkan.logging import get_logger
from kalkan.scanning.capture_contract import (
    CAPTURE_QUEUE,
    JOB_KEY,
    JOB_TTL_SECONDS,
    MAX_URL,
    MAX_URLS,
    PENDING_KEY,
    PENDING_TTL_SECONDS,
    CaptureJob,
    CaptureRecord,
    Purpose,
)
from kalkan.scanning.precheck import clean_url, entry_urls, host_of

CAPTURE_ACTOR = "browser_capture"
PROFILES = (Profile.DESKTOP, Profile.MOBILE)
_COUNTRY = re.compile(r"^[A-Za-z]{2}$")

SendJob = Callable[[CaptureJob], None]

log = get_logger(__name__)


def send_job(job: CaptureJob) -> None:
    """Queue ``browser_capture(job)`` on the process's broker."""
    message: dramatiq.Message[None] = dramatiq.Message(
        queue_name=CAPTURE_QUEUE,
        actor_name=CAPTURE_ACTOR,
        args=(job.model_dump(mode="json"),),
        kwargs={},
        options={},
    )
    dramatiq.get_broker().enqueue(message)


def country_for(markets: Sequence[str]) -> str | None:
    """The brand's first target market (§10); the browser falls back to the default country."""
    if not markets or not _COUNTRY.match(markets[0].strip()):
        return None
    return markets[0].strip().upper()


def issue_capture(
    redis: Redis,
    *,
    purpose: Purpose,
    customer_id: uuid.UUID,
    brand_id: uuid.UUID,
    case_id: uuid.UUID | None,
    urls: Sequence[str],
    country: str | None,
    now: datetime,
    profiles: Sequence[Profile] = PROFILES,
    send: SendJob = send_job,
) -> CaptureJob | None:
    """Record and send one capture job; ``None`` when the case already has a live job.

    A failed send releases the case's marker, so the reconciler can issue the job again.
    """
    if purpose is not Purpose.REFERENCE and case_id is None:
        raise ValueError("a case job needs a case id")
    entries = [url for url in dict.fromkeys(urls) if url and len(url) <= MAX_URL][:MAX_URLS]
    job_id = new_id()
    record = CaptureRecord(
        job_id=job_id,
        purpose=purpose,
        customer_id=customer_id,
        brand_id=brand_id,
        case_id=case_id,
        scan_ids={profile: new_id() for profile in profiles},
        urls=entries,
        issued_at=now,
    )
    job = CaptureJob(
        job_id=job_id,
        purpose=purpose,
        urls=entries,
        country=country,
        profiles=list(profiles),
        issued_at=now,
    )
    pending = PENDING_KEY.format(case_id=case_id) if case_id is not None else None
    if pending is not None and not redis.set(pending, str(job_id), nx=True, ex=PENDING_TTL_SECONDS):
        log.info("capture.already_pending", case_id=str(case_id))
        return None
    try:
        redis.set(JOB_KEY.format(job_id=job_id), record.model_dump_json(), ex=JOB_TTL_SECONDS)
        send(job)
    except BaseException:
        if pending is not None:
            redis.delete(pending)
        raise
    log.info(
        "capture.issued",
        job_id=str(job_id),
        purpose=purpose.value,
        case_id=str(case_id) if case_id else None,
        brand_id=str(brand_id),
        country=country,
    )
    return job


def load_record(redis: Redis, job_id: uuid.UUID) -> CaptureRecord | None:
    """The trusted record of a job; ``None`` when it expired, never existed or is unreadable."""
    raw = redis.get(JOB_KEY.format(job_id=job_id))
    if raw is None:
        return None
    try:
        record = CaptureRecord.model_validate_json(raw)
    except ValidationError:
        log.error("capture.bad_record", job_id=str(job_id))
        return None
    return record if record.job_id == job_id else None


def release_pending(redis: Redis, case_id: uuid.UUID, job_id: uuid.UUID) -> None:
    """Drop the case's marker if it is still this job's (a later job keeps its own)."""
    key = PENDING_KEY.format(case_id=case_id)
    current = redis.get(key)
    if isinstance(current, bytes):
        current = current.decode()
    if current == str(job_id):
        redis.delete(key)


# --- case jobs --------------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseTarget:
    case_id: uuid.UUID
    customer_id: uuid.UUID
    brand_id: uuid.UUID
    urls: list[str]
    country: str | None


def case_target(session: Session, case_id: uuid.UUID) -> CaseTarget | None:
    """What a capture of a ``detected``, non-demo case needs; ``None`` otherwise.

    The entry URLs are the pre-check's (K71), led by the one that answered it.
    """
    case = session.get(Case, case_id)
    if case is None or case.state is not CaseState.DETECTED:
        return None
    customer = session.get(Customer, case.customer_id)
    brand = session.get(Brand, case.brand_id)
    domain = session.get(Domain, case.domain_id)
    if customer is None or customer.is_demo or brand is None or domain is None:
        return None
    manual = session.scalar(
        select(DomainSighting.detail)
        .where(
            DomainSighting.domain_id == domain.id,
            DomainSighting.source == SightingSource.MANUAL,
        )
        .order_by(DomainSighting.seen_at.desc())
        .limit(1)
    )
    scan = session.scalar(
        select(Scan)
        .where(Scan.case_id == case.id, Scan.stage == ScanStage.HTTP)
        .order_by(Scan.started_at.desc())
        .limit(1)
    )
    urls = entry_urls(domain.name, (manual or {}).get("url"))
    if scan is not None:
        answered = clean_url(scan.redirect_chain[0] if scan.redirect_chain else scan.final_url)
        if answered and host_of(answered) in (domain.name, f"www.{domain.name}"):
            urls = [answered, *urls]
    return CaseTarget(
        case_id=case.id,
        customer_id=case.customer_id,
        brand_id=brand.id,
        urls=list(dict.fromkeys(urls)),
        country=country_for(brand.target_markets),
    )


def issue_case_capture(
    database: Database,
    redis: Redis,
    case_id: uuid.UUID,
    *,
    now: datetime,
    send: SendJob = send_job,
) -> CaptureJob | None:
    """Issue the capture of a ``detected`` case; ``None`` when it needs none or has one."""
    with database.worker_session() as session:
        target = case_target(session, case_id)
    if target is None:
        log.info("capture.not_issued", case_id=str(case_id), reason="not a detected case")
        return None
    return issue_capture(
        redis,
        purpose=Purpose.CASE,
        customer_id=target.customer_id,
        brand_id=target.brand_id,
        case_id=target.case_id,
        urls=target.urls,
        country=target.country,
        now=now,
        send=send,
    )
