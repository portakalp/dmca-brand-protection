"""Minimal valid rows for tests (written as kalkan_worker)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from kalkan.db.base import utcnow
from kalkan.db.models import (
    Artifact,
    Brand,
    BrandAsset,
    BrandMatch,
    BrandPermutation,
    Case,
    CaseEvent,
    Customer,
    Decision,
    Domain,
    EvidenceLink,
    Invitation,
    Message,
    Notice,
    OfficialDomain,
    PasswordReset,
    Recheck,
    Report,
    ReviewTask,
    Scan,
    Signal,
    User,
    UserSession,
    Verdict,
    WhitelistEntry,
)
from kalkan.domain.enums import (
    ArtifactKind,
    BrandAssetKind,
    CaseState,
    Category,
    Channel,
    DecisionOutcome,
    EventType,
    MessageDirection,
    NoticeBasis,
    NoticeMethod,
    NoticeTemplate,
    OfficialDomainKind,
    RecheckMethod,
    RecheckResult,
    ScanStage,
    TaskKind,
    UserRole,
    VerdictSource,
    WhitelistKind,
    WhitelistSource,
)
from kalkan.security import hash_password, hash_token


def customer(session: Session, slug: str | None = None, **values: object) -> Customer:
    slug = slug or f"customer-{uuid.uuid4().hex[:8]}"
    row = Customer(name=slug.title(), slug=slug, **values)
    session.add(row)
    session.flush()
    return row


def domain(session: Session, name: str | None = None) -> Domain:
    row = Domain(name=name or f"{uuid.uuid4().hex[:10]}-mintera.com")
    session.add(row)
    session.flush()
    return row


def brand(session: Session, owner: Customer) -> Brand:
    row = Brand(customer_id=owner.id, name="Mintera Bank", keywords=["mintera"])
    session.add(row)
    session.flush()
    return row


def case(
    session: Session, owner: Customer, of_brand: Brand, state: CaseState = CaseState.DETECTED
) -> Case:
    row = Case(
        customer_id=owner.id,
        number=f"TST-{uuid.uuid4().hex[:8]}",
        brand_id=of_brand.id,
        domain_id=domain(session).id,
        state=state,
        first_seen_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


@dataclass
class TenantRows:
    """One row in every tenant table for a customer; ``ids`` maps table name to row id."""

    customer: Customer
    ids: dict[str, uuid.UUID] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)


def tenant_rows(session: Session, slug: str | None = None) -> TenantRows:
    owner = customer(session, slug)
    rows = TenantRows(owner)
    now = utcnow()
    later = now + timedelta(hours=1)

    def add(table: str, row: object) -> None:
        session.add(row)
        session.flush()
        rows.ids[table] = row.id  # type: ignore[attr-defined]

    rows.ids["customers"] = owner.id
    user = User(
        customer_id=owner.id,
        email=f"owner@{owner.slug}.example",
        password_hash=hash_password("correct horse battery"),
        role=UserRole.OWNER,
    )
    add("users", user)
    for name in ("session", "invitation", "reset", "evidence"):
        rows.tokens[name] = f"{name}-token-{owner.slug}"
    add(
        "sessions",
        UserSession(
            customer_id=owner.id,
            token_hash=hash_token(rows.tokens["session"]),
            user_id=user.id,
            expires_at=later,
        ),
    )
    add(
        "invitations",
        Invitation(
            customer_id=owner.id,
            email=f"viewer@{owner.slug}.example",
            role=UserRole.VIEWER,
            token_hash=hash_token(rows.tokens["invitation"]),
            expires_at=later,
        ),
    )
    add(
        "password_resets",
        PasswordReset(
            customer_id=owner.id,
            user_id=user.id,
            token_hash=hash_token(rows.tokens["reset"]),
            expires_at=later,
        ),
    )
    of_brand = Brand(customer_id=owner.id, name="Mintera Bank", keywords=["mintera"])
    add("brands", of_brand)
    add(
        "brand_assets",
        BrandAsset(
            customer_id=owner.id,
            brand_id=of_brand.id,
            kind=BrandAssetKind.LOGO_LIGHT,
            storage_key=f"gs://bucket/{uuid.uuid4()}.png",
            sha256="0" * 64,
        ),
    )
    add(
        "official_domains",
        OfficialDomain(
            customer_id=owner.id,
            brand_id=of_brand.id,
            domain=f"{owner.slug}.example",
            kind=OfficialDomainKind.PRIMARY,
        ),
    )
    add(
        "brand_permutations",
        BrandPermutation(
            customer_id=owner.id,
            brand_id=of_brand.id,
            domain=f"{owner.slug}-login.example",
            fuzzer="addition",
        ),
    )
    add(
        "whitelist_entries",
        WhitelistEntry(
            customer_id=owner.id,
            pattern=f"partners.{owner.slug}.example",
            kind=WhitelistKind.AFFILIATE,
            source=WhitelistSource.MANUAL,
        ),
    )
    matched = domain(session)
    add(
        "brand_matches",
        BrandMatch(customer_id=owner.id, domain_id=matched.id, brand_id=of_brand.id, score=90),
    )
    the_case = Case(
        customer_id=owner.id,
        number=f"TST-{uuid.uuid4().hex[:8]}",
        brand_id=of_brand.id,
        domain_id=matched.id,
        state=CaseState.NOTIFIED,
        first_seen_at=now,
    )
    add("cases", the_case)
    add(
        "case_events",
        CaseEvent(customer_id=owner.id, case_id=the_case.id, type=EventType.DETECTED, actor="test"),
    )
    scan = Scan(customer_id=owner.id, case_id=the_case.id, stage=ScanStage.HTTP)
    add("scans", scan)
    add(
        "artifacts",
        Artifact(
            customer_id=owner.id,
            case_id=the_case.id,
            scan_id=scan.id,
            kind=ArtifactKind.HTML,
            storage_key=f"gs://bucket/{uuid.uuid4()}.html",
            sha256="0" * 64,
            bytes=10,
            content_type="text/html",
        ),
    )
    add(
        "signals",
        Signal(
            customer_id=owner.id,
            case_id=the_case.id,
            scan_id=scan.id,
            name="favicon_match",
            value=True,
        ),
    )
    add(
        "verdicts",
        Verdict(
            customer_id=owner.id,
            case_id=the_case.id,
            source=VerdictSource.STAFF,
            category=Category.PHISHING_CLONE,
            confidence=Decimal("0.9"),
            collects_credentials_or_payment=True,
            suggested_basis=NoticeBasis.PHISHING,
            short_explanation="test",
        ),
    )
    add(
        "decisions",
        Decision(
            customer_id=owner.id,
            case_id=the_case.id,
            outcome=DecisionOutcome.REVIEW,
            rule="R2",
            decided_by="test",
        ),
    )
    link = EvidenceLink(
        customer_id=owner.id,
        case_id=the_case.id,
        token_hash=hash_token(rows.tokens["evidence"]),
        expires_at=later,
    )
    add("evidence_links", link)
    notice = Notice(
        customer_id=owner.id,
        case_id=the_case.id,
        channel=Channel.HOSTING,
        method=NoticeMethod.EMAIL,
        template=NoticeTemplate.PHISHING,
        evidence_link_id=link.id,
        dry_run=False,
    )
    add("notices", notice)
    add(
        "messages",
        Message(
            customer_id=owner.id,
            case_id=the_case.id,
            notice_id=notice.id,
            direction=MessageDirection.INBOUND,
        ),
    )
    add(
        "review_tasks",
        ReviewTask(
            customer_id=owner.id, case_id=the_case.id, notice_id=notice.id, kind=TaskKind.BOUNCE
        ),
    )
    add(
        "rechecks",
        Recheck(
            customer_id=owner.id,
            case_id=the_case.id,
            method=RecheckMethod.HTTP,
            result=RecheckResult.ONLINE,
        ),
    )
    add("reports", Report(customer_id=owner.id, period="2026-09"))
    return rows
