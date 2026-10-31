"""Threats, their files and "This is our site" (architecture.md §11, §17, product.md §9, §11).

What the customer sees of a case leaves out the internals: dry-run previews (§15), the
operator's notes, failed jobs, review tasks and pre-checks. A case whose notices are only
previews shows as Active ("notice being prepared"), which is its panel status anyway.

"This is our site" adds a whitelist entry for the case's host (kind chosen by the owner,
``other`` by default, source ``this_is_ours``) and leaves a ``whitelist_changed`` outbox job;
the worker dismisses the case and sends the retractions (§15, K141, K150).
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, func, select

from kalkan.api.problems import ProblemError
from kalkan.api.v1.deps import Context, Member, Writer
from kalkan.api.v1.files import ZipEntry, evidence_zip, stream_file
from kalkan.api.v1.overview import check_brand
from kalkan.api.v1.queries import whitelisted
from kalkan.api.v1.schemas import (
    Page,
    ThreatItem,
    WhitelistItem,
    csv_cell,
    hosting_of,
    threat_item,
    whitelist_item,
)
from kalkan.api.v1.writes import add_whitelist_entry, audit
from kalkan.db.models import (
    Artifact,
    Brand,
    BrandAsset,
    Case,
    CaseEvent,
    Decision,
    Domain,
    Notice,
    OfficialDomain,
    Scan,
    Signal,
    Verdict,
)
from kalkan.db.outbox import WHITELIST_CHANGED, queue_job
from kalkan.domain.enums import (
    ArtifactKind,
    BrandAssetKind,
    CaseState,
    Category,
    Channel,
    DecisionOutcome,
    EventType,
    NoticeMethod,
    NoticeStatus,
    NoticeTemplate,
    OfficialDomainKind,
    Profile,
    ScanStage,
    VerdictSource,
    WhitelistKind,
    WhitelistSource,
)
from kalkan.domain.hostnames import InvalidHostnameError
from kalkan.domain.metrics import PanelStatus, Period, states_for
from kalkan.enrichment.whitelist import validate_pattern
from kalkan.followup.escalation import next_escalation

router = APIRouter(tags=["threats"])
MAX_PAGE_SIZE = 100
CSV_LIMIT = 10_000
RETENTION = timedelta(days=180)
# The timeline the customer sees (§6, K48).
VISIBLE_EVENTS = (
    EventType.DETECTED,
    EventType.CAPTURED,
    EventType.VERIFIED,
    EventType.APPROVED,
    EventType.NOTICE_SENT,
    EventType.REGISTRAR_ESCALATED,
    EventType.REPLY_RECEIVED,
    EventType.OFFLINE_DETECTED,
    EventType.ONLINE_AGAIN,
    EventType.WARNING_ACTIVE,
    EventType.TAKEN_DOWN,
    EventType.REOPENED,
    EventType.WATCHING,
    EventType.DISMISSED,
    EventType.WHITELISTED,
    EventType.ARCHIVED,
)
EVENT_DETAIL = ("channel", "method", "level", "classification")


class Sort(StrEnum):
    NEWEST = "-first_seen_at"
    OLDEST = "first_seen_at"
    DOMAIN = "domain"
    LAST_ACTION = "-last_action_at"


def _case(context: Context, case_id: uuid.UUID) -> tuple[Case, Domain]:
    case = context.session.get(Case, case_id)
    if case is None or case.customer_id != context.customer_id:
        raise ProblemError(404, title="No such threat", code="not_found")
    return case, context.session.get_one(Domain, case.domain_id)


# --- the list --------------------------------------------------------------------------------


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def threats_query(
    context: Context,
    *,
    status: PanelStatus | None,
    category: Category | None,
    brand_id: uuid.UUID | None,
    q: str | None,
    start: date | None,
    end: date | None,
    sort: Sort,
) -> Select[Case, Domain]:
    query = (
        select(Case, Domain)
        .join(Domain, Domain.id == Case.domain_id)
        .where(Case.customer_id == context.customer_id, ~whitelisted())
    )
    if status is not None:
        query = query.where(Case.state.in_(states_for(status)))
    if category is not None:
        query = query.where(Case.category == category)
    if brand_id is not None:
        query = query.where(Case.brand_id == brand_id)
    if q:
        pattern = f"%{_escape_like(q.strip().lower())}%"
        query = query.where(Domain.name.ilike(pattern, escape="\\"))
    if start is not None or end is not None:
        period = Period(start or date(2000, 1, 1), end or context.at.astimezone(context.tz).date())
        first, after = period.bounds(context.tz)
        query = query.where(Case.first_seen_at >= first, Case.first_seen_at < after)
    order: tuple[Any, ...] = {
        Sort.NEWEST: (Case.first_seen_at.desc(), Case.id),
        Sort.OLDEST: (Case.first_seen_at.asc(), Case.id),
        Sort.DOMAIN: (Domain.name.asc(), Case.id),
        Sort.LAST_ACTION: (Case.last_action_at.desc().nulls_last(), Case.id),
    }[sort]
    return query.order_by(*order)


def threats_csv(rows: list[ThreatItem]) -> Response:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["number", "domain", "status", "category", "first_seen_at", "hosting", "last_action"]
    )
    for item in rows:
        writer.writerow(
            [
                csv_cell(value)
                for value in (
                    item.number,
                    item.domain,
                    item.status.value,
                    item.category.value if item.category else "",
                    item.first_seen_at.isoformat(),
                    item.hosting,
                    item.last_action,
                )
            ]
        )
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="threats.csv"'},
    )


@router.get(
    "/threats", response_model=Page[ThreatItem], responses={200: {"content": {"text/csv": {}}}}
)
def threats(
    context: Member,
    status: PanelStatus | None = None,
    category: Category | None = None,
    brand_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    sort: Sort = Sort.NEWEST,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
    format: Annotated[str | None, Query(pattern="^(json|csv)$")] = None,
) -> Page[ThreatItem] | Response:
    check_brand(context, brand_id)
    query = threats_query(
        context,
        status=status,
        category=category,
        brand_id=brand_id,
        q=q,
        start=start,
        end=end,
        sort=sort,
    )
    session = context.session
    if format == "csv":
        rows = session.execute(query.limit(CSV_LIMIT)).all()
        return threats_csv([threat_item(case, domain) for case, domain in rows])
    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = session.execute(query.offset((page - 1) * page_size).limit(page_size)).all()
    return Page[ThreatItem](
        items=[threat_item(case, domain) for case, domain in rows],
        total=int(total or 0),
        page=page,
        page_size=page_size,
    )


# --- the detail ------------------------------------------------------------------------------


class Image(BaseModel):
    profile: Profile
    url: str


class Comparison(BaseModel):
    official: list[Image]
    clone: list[Image]
    visual_similarity: float | None


class SignalOut(BaseModel):
    name: str
    value: bool | float | int | None


class ReviewOut(BaseModel):
    source: VerdictSource
    category: Category
    confidence: float
    short_explanation: str
    created_at: datetime


class RuleOut(BaseModel):
    outcome: DecisionOutcome
    rule: str
    created_at: datetime


class AiReview(BaseModel):
    reviews: list[ReviewOut]
    rule: RuleOut | None


class Infrastructure(BaseModel):
    registered_at: datetime | None
    registrar: str | None
    nameservers: list[str]
    proxy: str | None
    hosting: str | None
    hosting_country: str | None
    origin_host: str | None
    origin_country: str | None
    ips: list[str]
    tls: dict[str, str] | None = None


class EventOut(BaseModel):
    type: EventType
    at: datetime
    detail: dict[str, str | int | None]


class NoticeOut(BaseModel):
    id: uuid.UUID
    channel: Channel
    method: NoticeMethod
    recipient: str | None
    template: NoticeTemplate
    language: str
    status: NoticeStatus
    escalation_level: int
    round: int
    sent_at: datetime | None


class FileOut(BaseModel):
    id: uuid.UUID
    kind: ArtifactKind
    profile: Profile | None
    bytes: int
    sha256: str
    content_type: str
    captured_at: datetime
    retained_until: datetime
    url: str


class NextSteps(BaseModel):
    next_check_at: datetime | None = Field(description="The next follow-up check, while followed.")
    escalation_at: datetime | None = Field(description="When the next escalation level is due.")
    escalation_channel: Channel | None = Field(
        description="The next level's channel; null with a time is an operator task."
    )


class ThreatDetail(BaseModel):
    threat: ThreatItem
    brand: str
    official_domain: str | None
    source: str | None
    comparison: Comparison
    signals: list[SignalOut]
    ai_review: AiReview
    infrastructure: Infrastructure
    timeline: list[EventOut]
    notices: list[NoticeOut]
    files: list[FileOut]
    next_steps: NextSteps
    evidence_zip_url: str


def _official_domain(context: Context, brand_id: uuid.UUID) -> str | None:
    domains = context.session.scalars(
        select(OfficialDomain)
        .where(OfficialDomain.brand_id == brand_id)
        .order_by(OfficialDomain.created_at)
    ).all()
    primary = next((d.domain for d in domains if d.kind is OfficialDomainKind.PRIMARY), None)
    return primary or (domains[0].domain if domains else None)


def _comparison(context: Context, case: Case) -> Comparison:
    session = context.session
    references = session.scalars(
        select(BrandAsset)
        .where(
            BrandAsset.brand_id == case.brand_id,
            BrandAsset.kind.in_(
                [BrandAssetKind.REFERENCE_DESKTOP, BrandAssetKind.REFERENCE_MOBILE]
            ),
        )
        .order_by(BrandAsset.created_at.desc())
    ).all()
    official: dict[Profile, Image] = {}
    for asset in references:
        profile = (
            Profile.DESKTOP if asset.kind is BrandAssetKind.REFERENCE_DESKTOP else Profile.MOBILE
        )
        official.setdefault(profile, Image(profile=profile, url=f"/api/v1/brand-assets/{asset.id}"))
    shots = session.execute(
        select(Artifact)
        .join(Scan, Scan.id == Artifact.scan_id)
        .where(
            Artifact.case_id == case.id,
            Artifact.kind == ArtifactKind.SCREENSHOT,
            Scan.stage == ScanStage.BROWSER,
            Scan.outcome == "ok",
        )
        .order_by(Artifact.created_at.desc())
    ).scalars()
    clone: dict[Profile, Image] = {}
    for artifact in shots:
        if artifact.profile is not None:
            clone.setdefault(
                artifact.profile,
                Image(profile=artifact.profile, url=f"/api/v1/artifacts/{artifact.id}"),
            )
    # The case-level value first, else the newest scan's.
    similarity = session.scalar(
        select(Signal.value)
        .where(Signal.case_id == case.id, Signal.name == "visual_similarity")
        .order_by(Signal.scan_id.is_not(None), Signal.created_at.desc())
        .limit(1)
    )
    return Comparison(
        official=[official[p] for p in Profile if p in official],
        clone=[clone[p] for p in Profile if p in clone],
        visual_similarity=float(similarity) if isinstance(similarity, (int, float)) else None,
    )


def _signals(context: Context, case: Case) -> list[SignalOut]:
    rows = context.session.scalars(
        select(Signal).where(Signal.case_id == case.id).order_by(Signal.name, Signal.created_at)
    ).all()
    merged: dict[str, bool | float | int | None] = {}
    for row in rows:
        value = row.value if isinstance(row.value, (bool, int, float)) else None
        if isinstance(value, bool) and row.scan_id is not None:
            merged[row.name] = bool(merged.get(row.name)) or value  # any scan
        else:
            merged.setdefault(row.name, value)
    return [SignalOut(name=name, value=value) for name, value in sorted(merged.items())]


def _ai_review(context: Context, case: Case) -> AiReview:
    session = context.session
    reviews = []
    for source in (VerdictSource.AI_TRIAGE, VerdictSource.AI_CONFIRM, VerdictSource.STAFF):
        verdict = session.scalars(
            select(Verdict)
            .where(Verdict.case_id == case.id, Verdict.source == source)
            .order_by(Verdict.created_at.desc())
            .limit(1)
        ).first()
        if verdict is not None:
            reviews.append(
                ReviewOut(
                    source=verdict.source,
                    category=verdict.category,
                    confidence=float(verdict.confidence),
                    short_explanation=verdict.short_explanation,
                    created_at=verdict.created_at,
                )
            )
    decision = session.scalars(
        select(Decision).where(Decision.case_id == case.id).order_by(Decision.created_at.desc())
    ).first()
    rule = (
        RuleOut(outcome=decision.outcome, rule=decision.rule, created_at=decision.created_at)
        if decision is not None
        else None
    )
    return AiReview(reviews=reviews, rule=rule)


def _infrastructure(domain: Domain) -> Infrastructure:
    return Infrastructure(
        registered_at=domain.registered_at,
        registrar=domain.registrar,
        nameservers=list(domain.nameservers),
        proxy="Cloudflare" if domain.is_cloudflare else None,
        hosting=hosting_of(domain),
        hosting_country=domain.hosting_country,
        origin_host=domain.origin_org,
        origin_country=domain.origin_country,
        ips=[str(ip) for ip in domain.ips],
    )


def _timeline(context: Context, case: Case) -> list[EventOut]:
    events = context.session.scalars(
        select(CaseEvent)
        .where(CaseEvent.case_id == case.id, CaseEvent.type.in_(VISIBLE_EVENTS))
        .order_by(CaseEvent.at, CaseEvent.id)
    ).all()
    out = []
    for event in events:
        data = event.data or {}
        if data.get("dry_run"):
            continue  # a reply to a preview (§16, K145)
        detail: dict[str, str | int | None] = {
            key: value
            for key, value in data.items()
            if key in EVENT_DETAIL and (value is None or isinstance(value, (str, int)))
        }
        out.append(EventOut(type=event.type, at=event.at, detail=detail))
    return out


def _notices(context: Context, case: Case) -> list[NoticeOut]:
    rows = context.session.scalars(
        select(Notice)
        .where(Notice.case_id == case.id, Notice.dry_run.is_(False))
        .order_by(Notice.round, Notice.escalation_level, Notice.created_at)
    ).all()
    return [
        NoticeOut(
            id=row.id,
            channel=row.channel,
            method=row.method,
            recipient=row.recipient,
            template=row.template,
            language=row.language.value,
            status=row.status,
            escalation_level=row.escalation_level,
            round=row.round,
            sent_at=row.sent_at,
        )
        for row in rows
    ]


def _next_steps(context: Context, case: Case) -> NextSteps:
    followed = case.state in (CaseState.NOTIFIED, CaseState.TAKEN_DOWN)
    escalation = next_escalation(context.session, case)
    return NextSteps(
        next_check_at=case.next_check_at if followed else None,
        escalation_at=escalation[1] if escalation else None,
        escalation_channel=escalation[0].channel if escalation else None,
    )


def _files(context: Context, case: Case) -> list[Artifact]:
    return list(
        context.session.scalars(
            select(Artifact)
            .where(Artifact.case_id == case.id)
            .order_by(Artifact.created_at, Artifact.kind, Artifact.profile)
        ).all()
    )


def _file_out(artifact: Artifact) -> FileOut:
    return FileOut(
        id=artifact.id,
        kind=artifact.kind,
        profile=artifact.profile,
        bytes=artifact.bytes,
        sha256=artifact.sha256,
        content_type=artifact.content_type,
        captured_at=artifact.created_at,
        retained_until=artifact.created_at + RETENTION,
        url=f"/api/v1/artifacts/{artifact.id}",
    )


def _source(context: Context, case: Case) -> str | None:
    from kalkan.db.models import DomainSighting

    return context.session.scalar(
        select(DomainSighting.source)
        .where(DomainSighting.domain_id == case.domain_id)
        .order_by(DomainSighting.seen_at)
        .limit(1)
    )


@router.get("/threats/{case_id}", response_model=ThreatDetail)
def threat(case_id: uuid.UUID, context: Member) -> ThreatDetail:
    case, domain = _case(context, case_id)
    brand = context.session.get_one(Brand, case.brand_id)
    source = _source(context, case)
    return ThreatDetail(
        threat=threat_item(case, domain),
        brand=brand.name,
        official_domain=_official_domain(context, case.brand_id),
        source=str(source) if source is not None else None,
        comparison=_comparison(context, case),
        signals=_signals(context, case),
        ai_review=_ai_review(context, case),
        infrastructure=_infrastructure(domain),
        timeline=_timeline(context, case),
        notices=_notices(context, case),
        files=[_file_out(artifact) for artifact in _files(context, case)],
        next_steps=_next_steps(context, case),
        evidence_zip_url=f"/api/v1/threats/{case.id}/evidence.zip",
    )


def file_name(artifact: Artifact) -> str:
    """``screenshot-desktop-<id>.png``: the same name in the ZIP and as a single download."""
    extension = artifact.storage_key.rsplit(".", 1)[-1] if "." in artifact.storage_key else "bin"
    profile = f"-{artifact.profile.value}" if artifact.profile else ""
    return f"{artifact.kind.value}{profile}-{artifact.id}.{extension}"


@router.get("/threats/{case_id}/evidence.zip", response_class=StreamingResponse)
def evidence(case_id: uuid.UUID, context: Member, request: Request) -> StreamingResponse:
    case, domain = _case(context, case_id)
    files = _files(context, case)
    entries = []
    for artifact in files:
        entries.append(
            ZipEntry(
                name=file_name(artifact),
                key=artifact.storage_key,
                sha256=artifact.sha256,
                size=artifact.bytes,
                kind=artifact.kind.value,
                profile=artifact.profile.value if artifact.profile else None,
                captured_at=artifact.created_at,
            )
        )
    manifest = {
        "case": case.number,
        "domain": domain.name,
        "first_seen_at": case.first_seen_at.isoformat(),
        "generated_at": context.at.isoformat(),
        "files": [
            {
                "name": entry.name,
                "sha256": entry.sha256,
                "bytes": entry.size,
                "kind": entry.kind,
                "profile": entry.profile,
                "captured_at": entry.captured_at.isoformat(),
            }
            for entry in entries
        ],
    }
    return evidence_zip(request, entries, manifest, f"{case.number}-evidence.zip")


@router.get("/artifacts/{artifact_id}", response_class=StreamingResponse)
def artifact(artifact_id: uuid.UUID, context: Member, request: Request) -> StreamingResponse:
    row = context.session.get(Artifact, artifact_id)
    if row is None or row.customer_id != context.customer_id:
        raise ProblemError(404, title="No such file", code="not_found")
    return stream_file(request, row.storage_key, row.content_type, file_name(row))


@router.get("/brand-assets/{asset_id}", response_class=StreamingResponse)
def brand_asset(asset_id: uuid.UUID, context: Member, request: Request) -> StreamingResponse:
    row = context.session.get(BrandAsset, asset_id)
    if row is None or row.customer_id != context.customer_id:
        raise ProblemError(404, title="No such file", code="not_found")
    return stream_file(request, row.storage_key, "image/png", f"{row.kind.value}-{row.id}")


# --- "This is our site" ----------------------------------------------------------------------


class MarkOurs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: WhitelistKind = WhitelistKind.OTHER
    note: str | None = Field(default=None, max_length=500)


class MarkedOurs(BaseModel):
    whitelist_entry: WhitelistItem
    status: str = Field(description="'queued': the case is dismissed and retracted shortly")


@router.post("/threats/{case_id}/mark-ours", response_model=MarkedOurs, status_code=202)
def mark_ours(case_id: uuid.UUID, body: MarkOurs, context: Writer, request: Request) -> MarkedOurs:
    case, domain = _case(context, case_id)
    try:
        pattern = validate_pattern(domain.name)
    except InvalidHostnameError as exc:
        raise ProblemError(422, title="This host cannot be whitelisted", detail=str(exc)) from None
    entry, _ = add_whitelist_entry(
        context,
        pattern=pattern,
        kind=body.kind,
        note=body.note,
        brand_id=case.brand_id,
        source=WhitelistSource.THIS_IS_OURS,
    )
    queue_job(context.session, WHITELIST_CHANGED, customer_id=context.customer_id)
    audit(context, request, "threat.mark_ours", case.number, pattern=pattern)
    return MarkedOurs(whitelist_entry=whitelist_item(entry), status="queued")
