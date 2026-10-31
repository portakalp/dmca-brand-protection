"""The case snapshot: what the AI layer and the decision engine read (architecture.md §12-§14).

:func:`build_snapshot` gathers a case's domain and infrastructure, its newest scans, its
signals, the match that admitted it and the ids of the brand's official reference assets into
one versioned Pydantic model. It is never stored; it is built again whenever it is needed, so
it always shows the current rows. Bump :data:`SNAPSHOT_VERSION` when a field changes meaning
or goes away; adding an optional field does not need a new version.

The newest scans are the newest HTTP pre-check and, per profile, the newest ``ok`` browser
scan (the one ``compute_signals`` read); a browser profile without an ``ok`` scan shows its
newest attempt. Page text is cut to :data:`MAX_TEXT` characters and, like titles, forms and
URLs, is untrusted: whoever puts it into a prompt wraps it in a labelled block (§13).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from kalkan.db.models import (
    Artifact,
    Brand,
    BrandAsset,
    BrandMatch,
    Case,
    Domain,
    OfficialDomain,
    Scan,
    Signal,
)
from kalkan.domain.enums import (
    ArtifactKind,
    BrandAssetKind,
    CaseState,
    OfficialDomainKind,
    Profile,
    ScanStage,
)
from kalkan.scanning.capture_contract import ButtonInfo, FormInfo, Outcome
from kalkan.signals.forms import parse_buttons, parse_forms
from kalkan.signals.text import domain_age_days

SNAPSHOT_VERSION: Literal[1] = 1
MAX_TEXT = 4000  # page text in the candidate block (§13)
MAX_REDIRECTS = 20


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SignalValue(_Model):
    value: JsonValue = None
    detail: dict[str, JsonValue] = Field(default_factory=dict)


class CaseInfo(_Model):
    id: uuid.UUID
    number: str
    state: CaseState
    customer_id: uuid.UUID
    brand_id: uuid.UUID
    first_seen_at: datetime


class OfficialDomainInfo(_Model):
    domain: str
    kind: OfficialDomainKind


class BrandInfo(_Model):
    id: uuid.UUID
    name: str
    keywords: list[str]
    target_markets: list[str]
    languages: list[str]
    official_domains: list[OfficialDomainInfo]


class DomainInfo(_Model):
    name: str
    unicode_name: str | None
    registered_domain: str | None
    tld: str | None
    registrar: str | None
    registrar_iana_id: int | None
    registered_at: datetime | None
    age_days: int | None  # at detection
    nameservers: list[str]
    ips: list[str]
    is_cloudflare: bool
    asn: int | None
    hosting_org: str | None
    hosting_country: str | None
    origin_ip: str | None
    origin_org: str | None
    origin_country: str | None
    webrisk_threat_types: list[str] | None  # None: not checked


class MatchInfo(_Model):
    score: int
    reasons: list[str]
    first_matched_at: datetime


class ScanInfo(_Model):
    id: uuid.UUID
    stage: ScanStage
    profile: Profile | None
    outcome: str | None
    started_at: datetime
    finished_at: datetime | None
    proxy_country: str | None
    http_status: int | None
    final_url: str | None
    redirect_chain: list[str]
    title: str | None
    text_excerpt: str  # untrusted, at most MAX_TEXT characters
    forms: list[FormInfo]  # browser scans; the pre-check's raw inventory is not repeated
    buttons: list[ButtonInfo]
    screenshot_artifact_id: uuid.UUID | None
    signals: dict[str, SignalValue]


class ReferenceAsset(_Model):
    id: uuid.UUID
    kind: BrandAssetKind
    captured_at: datetime | None
    source_url: str | None
    sha256: str
    phash: str | None
    anthropic_file_id: str | None


class CaseSnapshot(_Model):
    version: Literal[1] = SNAPSHOT_VERSION
    generated_at: datetime
    case: CaseInfo
    brand: BrandInfo
    domain: DomainInfo
    match: MatchInfo | None
    scans: list[ScanInfo]
    signals: dict[str, SignalValue]  # case-level signals (no scan)
    references: list[ReferenceAsset]

    def scan_values(self, name: str, stage: ScanStage | None = None) -> list[JsonValue]:
        """The signal's values on the snapshot's scans (empty values left out)."""
        return [
            scan.signals[name].value
            for scan in self.scans
            if (stage is None or scan.stage is stage)
            and name in scan.signals
            and scan.signals[name].value is not None
        ]

    def flag(self, name: str) -> bool | None:
        """A boolean signal over the case and its scans: true on any, false when only false
        was seen, None when nothing was computed."""
        values = self.scan_values(name)
        case_level = self.signals.get(name)
        if case_level is not None and case_level.value is not None:
            values.append(case_level.value)
        if any(value is True for value in values):
            return True
        return False if any(value is False for value in values) else None

    def value(self, name: str) -> JsonValue:
        """A case-level signal's value, None when it is missing or empty."""
        found = self.signals.get(name)
        return found.value if found is not None else None


# --- building ---------------------------------------------------------------------------------


def _newest_scans(session: Session, case_id: uuid.UUID) -> list[Scan]:
    scans = list(
        session.scalars(
            select(Scan)
            .where(Scan.case_id == case_id)
            .order_by(Scan.started_at.desc(), Scan.id.desc())
        )
    )
    chosen: list[Scan] = []
    http = next((scan for scan in scans if scan.stage is ScanStage.HTTP), None)
    if http is not None:
        chosen.append(http)
    for profile in Profile:
        browser = [s for s in scans if s.stage is ScanStage.BROWSER and s.profile is profile]
        ok = next((scan for scan in browser if scan.outcome == Outcome.OK.value), None)
        pick = ok or next(iter(browser), None)
        if pick is not None:
            chosen.append(pick)
    return chosen


def _signals(
    session: Session, case_id: uuid.UUID
) -> dict[uuid.UUID | None, dict[str, SignalValue]]:
    grouped: dict[uuid.UUID | None, dict[str, SignalValue]] = {}
    for signal in session.scalars(
        select(Signal).where(Signal.case_id == case_id).order_by(Signal.name)
    ):
        grouped.setdefault(signal.scan_id, {})[signal.name] = SignalValue(
            value=signal.value, detail=signal.detail or {}
        )
    return grouped


def _scan_info(session: Session, scan: Scan, signals: dict[str, SignalValue]) -> ScanInfo:
    browser = scan.stage is ScanStage.BROWSER
    screenshot = session.scalar(
        select(Artifact.id).where(
            Artifact.scan_id == scan.id, Artifact.kind == ArtifactKind.SCREENSHOT
        )
    )
    return ScanInfo(
        id=scan.id,
        stage=scan.stage,
        profile=scan.profile,
        outcome=scan.outcome,
        started_at=scan.started_at,
        finished_at=scan.finished_at,
        proxy_country=scan.proxy_country,
        http_status=scan.http_status,
        final_url=scan.final_url,
        redirect_chain=list(scan.redirect_chain or [])[:MAX_REDIRECTS],
        title=scan.title,
        text_excerpt=(scan.text_excerpt or "")[:MAX_TEXT],
        forms=parse_forms(scan.forms or []) if browser else [],
        buttons=parse_buttons(scan.buttons or []) if browser else [],
        screenshot_artifact_id=screenshot,
        signals=signals,
    )


def build_snapshot(session: Session, case_id: uuid.UUID, *, now: datetime) -> CaseSnapshot | None:
    """The case's snapshot, or None when the case, its brand or its domain is missing."""
    case = session.get(Case, case_id)
    if case is None:
        return None
    brand = session.get(Brand, case.brand_id)
    domain = session.get(Domain, case.domain_id)
    if brand is None or domain is None:
        return None
    officials = session.scalars(
        select(OfficialDomain)
        .where(OfficialDomain.brand_id == brand.id)
        .order_by(OfficialDomain.domain)
    )
    match = session.scalar(
        select(BrandMatch).where(BrandMatch.brand_id == brand.id, BrandMatch.domain_id == domain.id)
    )
    signals = _signals(session, case.id)
    references = session.scalars(
        select(BrandAsset)
        .where(BrandAsset.brand_id == brand.id)
        .order_by(BrandAsset.kind, BrandAsset.captured_at.desc().nulls_last(), BrandAsset.id)
    )
    webrisk = domain.webrisk
    return CaseSnapshot(
        generated_at=now,
        case=CaseInfo(
            id=case.id,
            number=case.number,
            state=case.state,
            customer_id=case.customer_id,
            brand_id=case.brand_id,
            first_seen_at=case.first_seen_at,
        ),
        brand=BrandInfo(
            id=brand.id,
            name=brand.name,
            keywords=list(brand.keywords),
            target_markets=list(brand.target_markets),
            languages=list(brand.languages),
            official_domains=[
                OfficialDomainInfo(domain=item.domain, kind=item.kind) for item in officials
            ],
        ),
        domain=DomainInfo(
            name=domain.name,
            unicode_name=domain.unicode_name,
            registered_domain=domain.registered_domain,
            tld=domain.tld,
            registrar=domain.registrar,
            registrar_iana_id=domain.registrar_iana_id,
            registered_at=domain.registered_at,
            age_days=domain_age_days(domain.registered_at, case.first_seen_at),
            nameservers=list(domain.nameservers),
            ips=[str(ip) for ip in domain.ips],
            is_cloudflare=domain.is_cloudflare,
            asn=domain.asn,
            hosting_org=domain.hosting_org,
            hosting_country=domain.hosting_country,
            origin_ip=str(domain.origin_ip) if domain.origin_ip is not None else None,
            origin_org=domain.origin_org,
            origin_country=domain.origin_country,
            webrisk_threat_types=(
                list(webrisk.get("threat_types") or []) if webrisk is not None else None
            ),
        ),
        match=(
            MatchInfo(
                score=match.score,
                reasons=list(match.reasons),
                first_matched_at=match.first_matched_at,
            )
            if match is not None
            else None
        ),
        scans=[
            _scan_info(session, scan, signals.get(scan.id, {}))
            for scan in _newest_scans(session, case.id)
        ],
        signals=signals.get(None, {}),
        references=[
            ReferenceAsset(
                id=asset.id,
                kind=asset.kind,
                captured_at=asset.captured_at,
                source_url=asset.source_url,
                sha256=asset.sha256,
                phash=asset.phash,
                anthropic_file_id=asset.anthropic_file_id,
            )
            for asset in references
        ],
    )
