"""Labels for the evaluation set (architecture.md §13, §19; K121).

The queue holds the cases without a label that have at least one stored screenshot, demo
cases excepted, in case-number order. Skipping (N) moves the cursor past a case for this
pass; it comes back on the next pass. A label is one row per case: labelling again replaces
the category and note, and every label is an ``audit_log`` row.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from kalkan.db.models import (
    Artifact,
    AuditLog,
    Brand,
    BrandAsset,
    Case,
    Customer,
    Domain,
    Label,
    Scan,
    Signal,
)
from kalkan.domain.enums import ArtifactKind, BrandAssetKind, Category, Profile, ScanStage

TARGET = 200
# Category per shortcut key (§19: 1-6).
KEYS: dict[str, Category] = {
    "1": Category.PHISHING_CLONE,
    "2": Category.BRAND_IMPERSONATION,
    "3": Category.AFFILIATE_OR_REVIEW,
    "4": Category.PARKED_OR_EMPTY,
    "5": Category.UNRELATED,
    "6": Category.UNCLEAR,
}
MAX_NOTE = 2000
# Signals shown next to the screenshots.
SHOWN_SIGNALS = (
    "visual_similarity",
    "favicon_match",
    "text_similarity",
    "form_password",
    "form_card",
    "form_iban",
    "deposit_cta",
    "external_form_action",
    "redirects_to_official",
    "cloaking_suspected",
    "domain_age_days",
)
REFERENCE_KIND = {
    Profile.DESKTOP: BrandAssetKind.REFERENCE_DESKTOP,
    Profile.MOBILE: BrandAssetKind.REFERENCE_MOBILE,
}


class LabelError(ValueError):
    pass


def _queue_filter() -> list[object]:
    has_screenshot = exists().where(
        Artifact.case_id == Case.id, Artifact.kind == ArtifactKind.SCREENSHOT
    )
    labelled = exists().where(Label.case_id == Case.id)
    return [Customer.is_demo.is_(False), has_screenshot, ~labelled]


def next_case(session: Session, after: str | None = None) -> Case | None:
    query = (
        select(Case)
        .join(Customer, Customer.id == Case.customer_id)
        .where(*_queue_filter())  # type: ignore[arg-type]
        .order_by(Case.number)
        .limit(1)
    )
    if after:
        query = query.where(Case.number > after)
    return session.scalar(query)


def remaining(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count(Case.id))
            .join(Customer, Customer.id == Case.customer_id)
            .where(*_queue_filter())  # type: ignore[arg-type]
        )
        or 0
    )


@dataclass(frozen=True)
class Progress:
    total: int
    target: int
    by_category: dict[Category, int]

    @property
    def percent(self) -> int:
        return min(100, round(100 * self.total / self.target)) if self.target else 100


def progress(session: Session) -> Progress:
    counts: Counter[Category] = Counter(
        dict(session.execute(select(Label.category, func.count()).group_by(Label.category)).all())
    )
    return Progress(
        total=sum(counts.values()),
        target=TARGET,
        by_category={category: counts.get(category, 0) for category in Category},
    )


@dataclass
class Shot:
    artifact_id: uuid.UUID
    final_url: str | None
    title: str | None


@dataclass
class LabelView:
    case: Case
    domain: str
    brand: str
    customer: str
    shots: dict[Profile, Shot] = field(default_factory=dict)
    references: dict[Profile, uuid.UUID] = field(default_factory=dict)
    signals: list[tuple[str, str | None, object]] = field(default_factory=list)
    label: Label | None = None


def screenshots(session: Session, case_id: uuid.UUID) -> dict[Profile, Shot]:
    """The newest ``ok`` browser screenshot per profile."""
    rows = session.execute(
        select(Artifact.id, Artifact.profile, Scan.final_url, Scan.title)
        .join(Scan, Scan.id == Artifact.scan_id)
        .where(
            Artifact.case_id == case_id,
            Artifact.kind == ArtifactKind.SCREENSHOT,
            Scan.stage == ScanStage.BROWSER,
        )
        .order_by(Scan.started_at.desc(), Artifact.created_at.desc())
    ).all()
    shots: dict[Profile, Shot] = {}
    for artifact_id, profile, final_url, title in rows:
        if profile is not None and profile not in shots:
            shots[profile] = Shot(artifact_id, final_url, title)
    return shots


def references(session: Session, brand_id: uuid.UUID) -> dict[Profile, uuid.UUID]:
    """The newest reference screenshot of the brand per profile."""
    found: dict[Profile, uuid.UUID] = {}
    for profile, kind in REFERENCE_KIND.items():
        asset_id = session.scalar(
            select(BrandAsset.id)
            .where(BrandAsset.brand_id == brand_id, BrandAsset.kind == kind)
            .order_by(BrandAsset.captured_at.desc().nulls_last(), BrandAsset.created_at.desc())
            .limit(1)
        )
        if asset_id is not None:
            found[profile] = asset_id
    return found


def view(session: Session, case: Case) -> LabelView:
    domain = session.get(Domain, case.domain_id)
    brand = session.get(Brand, case.brand_id)
    customer = session.get(Customer, case.customer_id)
    result = LabelView(
        case=case,
        domain=domain.name if domain else "?",
        brand=brand.name if brand else "?",
        customer=customer.name if customer else "?",
        shots=screenshots(session, case.id),
        references=references(session, case.brand_id),
        label=session.scalar(select(Label).where(Label.case_id == case.id)),
    )
    result.signals = case_signals(session, case.id, SHOWN_SIGNALS)
    return result


def case_signals(
    session: Session, case_id: uuid.UUID, names: tuple[str, ...] | None = None
) -> list[tuple[str, str | None, object]]:
    """``(name, profile, value)``; case-level signals have no profile (K40)."""
    query = (
        select(Signal.name, Scan.profile, Signal.value)
        .outerjoin(Scan, Scan.id == Signal.scan_id)
        .where(Signal.case_id == case_id)
        .order_by(Signal.name, Scan.started_at.desc())
    )
    if names is not None:
        query = query.where(Signal.name.in_(names))
    return [
        (name, profile.value if profile else None, value)
        for name, profile, value in session.execute(query).all()
    ]


def save_label(
    session: Session,
    case_id: uuid.UUID,
    category: str,
    note: str | None,
    *,
    actor: str,
    ip: str | None,
    now: datetime,
) -> Label:
    try:
        chosen = Category(category)
    except ValueError:
        raise LabelError(f"unknown category {category!r}") from None
    case = session.get(Case, case_id)
    if case is None:
        raise LabelError("no such case")
    text = (note or "").strip()[:MAX_NOTE] or None
    label = session.scalar(select(Label).where(Label.case_id == case_id).with_for_update())
    old = label.category.value if label else None
    if label is None:
        label = Label(case_id=case_id, category=chosen, labeled_by=actor, labeled_at=now, note=text)
        session.add(label)
    else:
        label.category = chosen
        label.note = text
        label.labeled_by = actor
        label.labeled_at = now
    session.add(
        AuditLog(
            actor=actor,
            action="label.set",
            customer_id=case.customer_id,
            target=case.number,
            data={"category": chosen.value, "old": old, "note": text},
            ip=ip,
            at=now,
        )
    )
    session.flush()
    return label
