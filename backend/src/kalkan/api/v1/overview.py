"""``/me``, ``/overview`` and ``/brands`` (architecture.md §17, product.md §9, §12)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalkan.api.problems import ProblemError
from kalkan.api.v1.deps import Context, Member, customer_tz
from kalkan.api.v1.queries import cohort, live_count, whitelisted
from kalkan.api.v1.schemas import ThreatItem, WhitelistItem, threat_item, whitelist_item
from kalkan.db.models import Brand, Case, Customer, Domain, OfficialDomain, WhitelistEntry
from kalkan.domain import metrics
from kalkan.domain.enums import Channel, CustomerStatus, OfficialDomainKind, UserRole
from kalkan.domain.metrics import PanelStatus, Period

router = APIRouter(tags=["overview"])
MAX_DAYS = 366
RECENT = 8
LAST_WHITELISTED = 5


# --- /me and /brands -------------------------------------------------------------------------


class BrandOut(BaseModel):
    id: uuid.UUID
    name: str
    official_domains: list[str]
    primary_domain: str | None
    target_markets: list[str]
    active: bool


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    role: UserRole
    totp_enabled: bool


class CustomerOut(BaseModel):
    id: uuid.UUID
    name: str
    status: CustomerStatus
    is_demo: bool
    read_only: bool
    timezone: str
    alert_email: str | None


class Permissions(BaseModel):
    write_whitelist: bool
    mark_ours: bool
    manage_users: bool
    settings: bool


class Me(BaseModel):
    user: UserOut
    customer: CustomerOut
    brands: list[BrandOut]
    permissions: Permissions


def brands_of(context: Context) -> list[BrandOut]:
    session = context.session
    brands = session.scalars(
        select(Brand).where(Brand.customer_id == context.customer_id).order_by(Brand.name)
    ).all()
    domains = session.scalars(
        select(OfficialDomain)
        .where(OfficialDomain.customer_id == context.customer_id)
        .order_by(OfficialDomain.created_at)
    ).all()
    out = []
    for brand in brands:
        own = [d for d in domains if d.brand_id == brand.id]
        primary = next((d.domain for d in own if d.kind is OfficialDomainKind.PRIMARY), None)
        out.append(
            BrandOut(
                id=brand.id,
                name=brand.name,
                official_domains=[d.domain for d in own],
                primary_domain=primary or (own[0].domain if own else None),
                target_markets=list(brand.target_markets),
                active=brand.active,
            )
        )
    return out


@router.get("/me", response_model=Me)
def me(context: Member) -> Me:
    user, customer = context.user, context.customer
    owner = context.is_owner
    writes = owner and not context.read_only
    return Me(
        user=UserOut(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            totp_enabled=user.totp_enabled_at is not None,
        ),
        customer=CustomerOut(
            id=customer.id,
            name=customer.name,
            status=customer.status,
            is_demo=customer.is_demo,
            read_only=context.read_only,
            timezone=customer.timezone,
            alert_email=customer.alert_email,
        ),
        brands=brands_of(context),
        permissions=Permissions(
            write_whitelist=writes,
            mark_ours=writes,
            manage_users=writes,
            settings=writes,
        ),
    )


@router.get("/brands", response_model=list[BrandOut])
def brands(context: Member) -> list[BrandOut]:
    return brands_of(context)


# --- /overview -------------------------------------------------------------------------------


class PeriodOut(BaseModel):
    start: date
    end: date


class KpisOut(BaseModel):
    detected: int
    verified: int
    taken_down: int
    takedown_rate: int | None
    median_takedown_hours: float | None


class ChangesOut(BaseModel):
    detected: int | None
    verified: int | None
    taken_down: int | None
    median_takedown_hours: int | None


class DayOut(BaseModel):
    day: date
    detected: int
    taken_down: int


class ChannelOut(BaseModel):
    channel: Channel
    cases: int
    median_hours: float | None


class WhitelistSummary(BaseModel):
    total: int
    added_this_month: int
    latest: list[WhitelistItem]


class OverviewOut(BaseModel):
    period: PeriodOut
    previous_period: PeriodOut
    kpis: KpisOut
    previous_kpis: KpisOut
    changes: ChangesOut
    daily: list[DayOut]
    status_distribution: dict[PanelStatus, int]
    takedowns_by_channel: list[ChannelOut]
    live_threats: int
    recent_threats: list[ThreatItem]
    whitelist: WhitelistSummary


def period_of(context: Context, start: date | None, end: date | None) -> Period:
    today = context.at.astimezone(context.tz).date()
    end = end or today
    start = start or end - timedelta(days=29)
    if start > end:
        raise ProblemError(422, title="'from' is after 'to'", code="period")
    if (end - start).days + 1 > MAX_DAYS:
        raise ProblemError(422, title="The period is longer than a year", code="period")
    return Period(start, end)


def check_brand(context: Context, brand_id: uuid.UUID | None) -> None:
    if brand_id is not None and context.session.get(Brand, brand_id) is None:
        raise ProblemError(404, title="No such brand", code="not_found")


def _kpis(value: metrics.Kpis) -> KpisOut:
    return KpisOut(
        detected=value.detected,
        verified=value.verified,
        taken_down=value.taken_down,
        takedown_rate=value.takedown_rate,
        median_takedown_hours=value.median_takedown_hours,
    )


def whitelist_summary(session: Session, customer: Customer, at: datetime) -> WhitelistSummary:
    tz = customer_tz(customer)
    month_start = at.astimezone(tz).date().replace(day=1)
    since = datetime.combine(month_start, datetime.min.time(), tzinfo=tz)
    where = WhitelistEntry.customer_id == customer.id
    total = session.scalar(select(func.count()).select_from(WhitelistEntry).where(where))
    added = session.scalar(
        select(func.count())
        .select_from(WhitelistEntry)
        .where(where, WhitelistEntry.created_at >= since)
    )
    latest = session.scalars(
        select(WhitelistEntry)
        .where(where)
        .order_by(WhitelistEntry.created_at.desc())
        .limit(LAST_WHITELISTED)
    ).all()
    return WhitelistSummary(
        total=int(total or 0),
        added_this_month=int(added or 0),
        latest=[whitelist_item(entry) for entry in latest],
    )


def overview_for(
    session: Session,
    customer: Customer,
    at: datetime,
    period: Period,
    brand_id: uuid.UUID | None,
) -> OverviewOut:
    """The overview of ``customer`` for ``period``; ``session`` is the customer's own."""
    tz, customer_id = customer_tz(customer), customer.id
    start, after = period.bounds(tz)
    previous = period.previous()
    before, before_after = previous.bounds(tz)
    result = metrics.overview(
        cohort(session, customer_id, start, after, brand_id),
        cohort(session, customer_id, before, before_after, brand_id),
        period=period,
        tz=tz,
        live_count=live_count(session, customer_id, brand_id),
    )
    recent_query = (
        select(Case, Domain)
        .join(Domain, Domain.id == Case.domain_id)
        .where(
            Case.customer_id == customer_id,
            Case.first_seen_at >= start,
            Case.first_seen_at < after,
            ~whitelisted(),
        )
        .order_by(Case.first_seen_at.desc())
        .limit(RECENT)
    )
    if brand_id is not None:
        recent_query = recent_query.where(Case.brand_id == brand_id)
    recent = [threat_item(case, domain) for case, domain in session.execute(recent_query)]
    return OverviewOut(
        period=PeriodOut(start=period.start, end=period.end),
        previous_period=PeriodOut(start=previous.start, end=previous.end),
        kpis=_kpis(result.kpis),
        previous_kpis=_kpis(result.previous_kpis),
        changes=ChangesOut(
            detected=result.changes.detected,
            verified=result.changes.verified,
            taken_down=result.changes.taken_down,
            median_takedown_hours=result.changes.median_takedown_hours,
        ),
        daily=[
            DayOut(day=d.day, detected=d.detected, taken_down=d.taken_down) for d in result.daily
        ],
        status_distribution=result.distribution,
        takedowns_by_channel=[
            ChannelOut(channel=row.channel, cases=row.cases, median_hours=row.median_hours)
            for row in result.by_channel
        ],
        live_threats=result.live,
        recent_threats=recent,
        whitelist=whitelist_summary(session, customer, at),
    )


@router.get("/overview", response_model=OverviewOut)
def overview(
    context: Member,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    brand_id: uuid.UUID | None = None,
) -> OverviewOut:
    check_brand(context, brand_id)
    period = period_of(context, start, end)
    return overview_for(context.session, context.customer, context.at, period, brand_id)
