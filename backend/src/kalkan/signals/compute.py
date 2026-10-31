"""compute_signals: the similarity signals of a captured case (architecture.md §7, §12).

Runs after ``persist_capture`` stored a capture. It reads the newest ``ok`` browser scan of each
profile, the brand's hashed assets and the domain row, and upserts:

- per browser scan: ``form_password``, ``form_card``, ``form_iban``, ``form_phone``,
  ``deposit_cta``, ``external_form_action``, ``brand_in_title``, ``brand_in_text``,
  ``redirects_to_official`` (``cta_targets`` was written by ``persist_capture``);
- per case (``scan_id`` empty, K40): ``visual_similarity``, ``phash_distance_desktop``,
  ``phash_distance_mobile``, ``favicon_phash_distance``, ``text_similarity``,
  ``cloaking_suspected``, ``domain_age_days``, ``webrisk_listed``.

A signal that cannot be computed (no reference yet, one profile missing) is written with an
empty value and the reason in ``detail``, so the snapshot always has every name. The same
inputs give the same rows: nothing time-dependent goes into a value or detail, and a second
run updates the rows in place (K113). A case that is no longer ``detected`` gets nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kalkan.db.base import new_id
from kalkan.db.models import (
    Artifact,
    Brand,
    BrandAsset,
    Case,
    CaseEvent,
    Domain,
    OfficialDomain,
    Scan,
    Signal,
)
from kalkan.db.session import Database
from kalkan.domain.enums import (
    ArtifactKind,
    BrandAssetKind,
    CaseState,
    EventType,
    Profile,
    ScanStage,
)
from kalkan.logging import get_logger
from kalkan.scanning.capture_contract import ButtonInfo, FormInfo, Outcome
from kalkan.scanning.precheck import host_of
from kalkan.signals import text as textsig
from kalkan.signals.assets import hash_brand_assets
from kalkan.signals.forms import form_signals, parse_buttons, parse_forms
from kalkan.signals.visual import Hashes, Reference, image_hashes, nearest, visual_similarity
from kalkan.storage.gcs import ObjectStore

REFERENCE_KINDS = {
    Profile.DESKTOP: BrandAssetKind.REFERENCE_DESKTOP,
    Profile.MOBILE: BrandAssetKind.REFERENCE_MOBILE,
}
SYSTEM = "system"
MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024  # the hand-off cap of a viewport screenshot

log = get_logger(__name__)


class ComputeStatus(StrEnum):
    COMPUTED = "computed"  # signals written; ai_triage comes next
    SKIPPED = "skipped"  # no such case, not detected, or no ok browser scan


@dataclass(frozen=True)
class BrowserPage:
    scan_id: uuid.UUID
    profile: Profile
    final_url: str | None
    title: str | None
    text: str
    forms: list[FormInfo]
    buttons: list[ButtonInfo]
    screenshot_key: str | None


@dataclass
class Inputs:
    customer_id: uuid.UUID
    keywords: list[str]
    officials: list[str]
    domain_name: str
    registered_at: datetime | None
    webrisk: dict[str, Any] | None
    detected_at: datetime
    pages: dict[Profile, BrowserPage]
    favicon: tuple[uuid.UUID, str, str | None] | None  # http scan id, pHash, dHash
    screens: dict[Profile, list[Reference]] = field(default_factory=dict)
    texts: dict[Profile, list[tuple[str, str]]] = field(default_factory=dict)
    icons: list[Reference] = field(default_factory=list)


Row = tuple[uuid.UUID | None, str, Any, dict[str, Any]]


# --- reading ----------------------------------------------------------------------------------


def _pages(session: Session, case_id: uuid.UUID) -> dict[Profile, BrowserPage]:
    scans = session.scalars(
        select(Scan)
        .where(
            Scan.case_id == case_id,
            Scan.stage == ScanStage.BROWSER,
            Scan.outcome == Outcome.OK.value,
        )
        .order_by(Scan.started_at.desc(), Scan.id.desc())
    )
    pages: dict[Profile, BrowserPage] = {}
    for scan in scans:
        if scan.profile is None or scan.profile in pages:
            continue
        key = session.scalar(
            select(Artifact.storage_key).where(
                Artifact.scan_id == scan.id, Artifact.kind == ArtifactKind.SCREENSHOT
            )
        )
        pages[scan.profile] = BrowserPage(
            scan_id=scan.id,
            profile=scan.profile,
            final_url=scan.final_url,
            title=scan.title,
            text=scan.text_excerpt or "",
            forms=parse_forms(scan.forms or []),
            buttons=parse_buttons(scan.buttons or []),
            screenshot_key=key,
        )
    return pages


def _favicon(session: Session, case_id: uuid.UUID) -> tuple[uuid.UUID, str, str | None] | None:
    """The candidate favicon's hashes from the newest pre-check that fetched one."""
    found = session.execute(
        select(Scan.id, Signal.detail)
        .join(Signal, Signal.scan_id == Scan.id)
        .where(
            Scan.case_id == case_id,
            Scan.stage == ScanStage.HTTP,
            Signal.name == "favicon_match",
        )
        .order_by(Scan.started_at.desc(), Scan.id.desc())
        .limit(1)
    ).first()
    if found is None:
        return None
    scan_id, detail = found
    phash = (detail or {}).get("phash")
    if not isinstance(phash, str):
        return None
    dhash = (detail or {}).get("dhash")
    return scan_id, phash, dhash if isinstance(dhash, str) else None


def _load(session: Session, case: Case) -> Inputs | None:
    pages = _pages(session, case.id)
    brand = session.get(Brand, case.brand_id)
    domain = session.get(Domain, case.domain_id)
    if not pages or brand is None or domain is None:
        return None
    officials = sorted(
        set(
            session.scalars(
                select(OfficialDomain.domain).where(OfficialDomain.customer_id == case.customer_id)
            )
        )
    )
    inputs = Inputs(
        customer_id=case.customer_id,
        keywords=list(brand.keywords),
        officials=officials,
        domain_name=domain.name,
        registered_at=domain.registered_at,
        webrisk=domain.webrisk,
        detected_at=case.first_seen_at,
        pages=pages,
        favicon=_favicon(session, case.id),
    )
    assets = session.scalars(
        select(BrandAsset)
        .where(BrandAsset.brand_id == brand.id)
        .order_by(BrandAsset.captured_at.desc().nulls_last(), BrandAsset.id)
    )
    for asset in assets:
        profile = next((p for p, kind in REFERENCE_KINDS.items() if kind == asset.kind), None)
        if profile is not None:
            if asset.phash:
                inputs.screens.setdefault(profile, []).append(
                    Reference(str(asset.id), asset.phash, asset.dhash)
                )
            if asset.text_excerpt:
                inputs.texts.setdefault(profile, []).append((str(asset.id), asset.text_excerpt))
        elif asset.kind == BrandAssetKind.FAVICON and asset.phash:
            inputs.icons.append(Reference(str(asset.id), asset.phash, asset.dhash))
    return inputs


def _screenshot(store: ObjectStore, key: str | None) -> Hashes | None:
    if key is None:
        return None
    data = store.read_bytes(key)
    return image_hashes(data, screenshot=True) if len(data) <= MAX_SCREENSHOT_BYTES else None


# --- computing --------------------------------------------------------------------------------


def _page_rows(inputs: Inputs, page: BrowserPage) -> list[Row]:
    rows: list[Row] = []
    forms = form_signals(page.forms, page.buttons, page.final_url)
    details = forms.details()
    for name, value in forms.values().items():
        rows.append((page.scan_id, name, value, details[name]))
    final_host = host_of(page.final_url)
    in_title, in_text = textsig.brand_on_page(
        page.title, page.text, inputs.keywords, (inputs.domain_name, final_host)
    )
    rows.append((page.scan_id, "brand_in_title", in_title, {}))
    rows.append((page.scan_id, "brand_in_text", in_text, {}))
    rows.append(
        (
            page.scan_id,
            "redirects_to_official",
            textsig.redirects_to_official(page.final_url, inputs.officials),
            {"host": final_host} if final_host else {},
        )
    )
    return rows


def _visual_rows(inputs: Inputs, shots: dict[Profile, Hashes]) -> list[Row]:
    result = visual_similarity(shots, inputs.screens)
    profiles: dict[str, Any] = {}
    rows: list[Row] = []
    for profile in Profile:
        page = inputs.pages.get(profile)
        found = result.distances.get(profile)
        if page is None:
            reason = "no_capture"
        elif profile not in shots:
            reason = "no_screenshot"
        elif found is None:
            reason = "no_reference"
        else:
            reason = ""
        detail: dict[str, Any] = {"scan_id": str(page.scan_id)} if page is not None else {}
        if found is not None:
            detail |= {
                "reference_asset_id": found.asset_id,
                "dhash_distance": found.dhash_distance,
            }
        if reason:
            detail["reason"] = reason
        profiles[profile.value] = {"distance": found.distance if found else None, **detail}
        rows.append(
            (None, f"phash_distance_{profile.value}", found.distance if found else None, detail)
        )
    rows.append((None, "visual_similarity", result.similarity, {"profiles": profiles}))
    return rows


def _favicon_row(inputs: Inputs) -> Row:
    if inputs.favicon is None:
        return (None, "favicon_phash_distance", None, {"reason": "no_favicon"})
    scan_id, phash, dhash = inputs.favicon
    found = nearest(phash, dhash, inputs.icons)
    if found is None:
        return (
            None,
            "favicon_phash_distance",
            None,
            {"scan_id": str(scan_id), "reason": "no_reference"},
        )
    return (
        None,
        "favicon_phash_distance",
        found.distance,
        {"scan_id": str(scan_id), "reference_asset_id": found.asset_id},
    )


def _text_rows(inputs: Inputs) -> list[Row]:
    texts = {profile: page.text for profile, page in inputs.pages.items()}
    best, per_profile = textsig.text_similarity(texts, inputs.texts)
    detail: dict[str, Any] = {
        "profiles": {
            profile.value: (
                {"ratio": match.ratio, "reference_asset_id": match.reference_asset_id}
                if match is not None
                else None
            )
            for profile, match in sorted(per_profile.items())
        }
    }
    if best is None:
        detail["reason"] = "no_reference" if not inputs.texts else "no_text"
    views = {}
    for profile, page in inputs.pages.items():
        in_title, in_text = textsig.brand_on_page(
            page.title, page.text, inputs.keywords, (inputs.domain_name, host_of(page.final_url))
        )
        views[profile] = textsig.PageView(f"{page.title or ''} {page.text}", in_title or in_text)
    cloaked, cloak_detail = textsig.cloaking(views)
    if cloaked is None:
        cloak_detail = {"reason": "one_profile"}
    webrisk = inputs.webrisk
    return [
        (None, "text_similarity", best, detail),
        (None, "cloaking_suspected", cloaked, cloak_detail),
        (
            None,
            "domain_age_days",
            textsig.domain_age_days(inputs.registered_at, inputs.detected_at),
            {"registered_at": inputs.registered_at.isoformat()} if inputs.registered_at else {},
        ),
        (
            None,
            "webrisk_listed",
            textsig.webrisk_listed(webrisk),
            {"threat_types": list(webrisk.get("threat_types") or [])} if webrisk else {},
        ),
    ]


def signal_rows(inputs: Inputs, shots: dict[Profile, Hashes]) -> list[Row]:
    """Every signal row of the case, in a fixed order."""
    rows: list[Row] = []
    for profile in Profile:
        page = inputs.pages.get(profile)
        if page is not None:
            rows += _page_rows(inputs, page)
    rows += _visual_rows(inputs, shots)
    rows.append(_favicon_row(inputs))
    rows += _text_rows(inputs)
    return rows


# --- writing ----------------------------------------------------------------------------------


def upsert_signals(
    session: Session, customer_id: uuid.UUID, case_id: uuid.UUID, rows: list[Row]
) -> None:
    for scan_id, name, value, detail in rows:
        session.execute(
            insert(Signal)
            .values(
                id=new_id(),
                customer_id=customer_id,
                case_id=case_id,
                scan_id=scan_id,
                name=name,
                value=value,
                detail=detail,
            )
            .on_conflict_do_update(
                index_elements=[Signal.case_id, Signal.scan_id, Signal.name],
                set_={"value": value, "detail": detail},
            )
        )


def compute_signals(database: Database, store: ObjectStore, case_id: uuid.UUID) -> ComputeStatus:
    with database.worker_session() as session:
        case = session.get(Case, case_id)
        if case is None or case.state is not CaseState.DETECTED:
            log.info("signals.skipped", case_id=str(case_id), reason="not detected")
            return ComputeStatus.SKIPPED
        brand_id = case.brand_id
    hash_brand_assets(database, store, brand_id)
    with database.worker_session() as session:
        case = session.get(Case, case_id)
        inputs = _load(session, case) if case is not None else None
    if inputs is None:
        log.info("signals.skipped", case_id=str(case_id), reason="no ok browser scan")
        return ComputeStatus.SKIPPED
    shots: dict[Profile, Hashes] = {}
    for profile, page in inputs.pages.items():
        found = _screenshot(store, page.screenshot_key)
        if found is not None:
            shots[profile] = found
    rows = signal_rows(inputs, shots)
    with database.worker_session() as session:
        case = session.get(Case, case_id, with_for_update=True)
        if case is None or case.state is not CaseState.DETECTED:
            log.info("signals.skipped", case_id=str(case_id), reason="left detected")
            return ComputeStatus.SKIPPED
        upsert_signals(session, case.customer_id, case.id, rows)
    values = {name: value for scan_id, name, value, _ in rows if scan_id is None}
    log.info("signals.computed", case_id=str(case_id), rows=len(rows), **values)
    return ComputeStatus.COMPUTED


def record_failure(
    database: Database,
    case_id: uuid.UUID,
    error: str,
    *,
    at: datetime,
    job: str = "compute_signals",
) -> None:
    """The last attempt of ``job`` failed: ``job_failed`` stops the reconciler; ops sees it (§7)."""
    with database.worker_session() as session:
        case = session.get(Case, case_id, with_for_update=True)
        if case is None or case.state is not CaseState.DETECTED:
            return
        session.add(
            CaseEvent(
                customer_id=case.customer_id,
                case_id=case.id,
                at=at,
                type=EventType.JOB_FAILED,
                actor=SYSTEM,
                data={"job": job, "error": error[:500]},
            )
        )
