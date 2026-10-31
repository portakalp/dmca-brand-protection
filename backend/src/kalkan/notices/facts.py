"""What the notices of a case are built from, read from our own rows (architecture.md §15).

- :func:`notice_facts`: the fact block and the fixed paragraph (:class:`NoticeFacts`);
- :func:`channel_facts`: the planner's input (basis, registration, hosting, contacts);
- :func:`notice_basis`: the newest verdict's ``suggested_basis`` (the rule's confirmation or
  the operator's ``staff`` verdict, K132).

The registrar's name and the hosting organisation are RDAP text: they go to the AI as untrusted
fields and never into a notice by themselves.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from kalkan.db.models import (
    Artifact,
    Brand,
    Case,
    ChannelContact,
    Domain,
    OfficialDomain,
    Scan,
    Signal,
    Verdict,
)
from kalkan.domain.enums import (
    ArtifactKind,
    ContactKind,
    NoticeBasis,
    OfficialDomainKind,
    OriginSource,
    ScanStage,
)
from kalkan.notices.planner import ChannelFacts, Contact
from kalkan.notices.render import NoticeFacts, clean_url, screenshot_hashes
from kalkan.signals.text import domain_age_days

# The signals the notice text may name (§13, "Bildirim metni").
ALLOWED_SIGNALS = (
    "visual_similarity",
    "favicon_match",
    "form_password",
    "form_card",
    "form_iban",
    "deposit_cta",
    "external_form_action",
    "domain_age_days",
)


@dataclass(frozen=True)
class TextInputs:
    """The AI's input for the explanation paragraph (structured fields only, §13)."""

    facts: NoticeFacts
    signals: dict[str, object]
    registered_on: str | None
    registrar: str | None  # untrusted
    hosting_org: str | None  # untrusted


def newest_verdict(session: Session, case_id: uuid.UUID) -> Verdict | None:
    return session.scalar(
        select(Verdict)
        .where(Verdict.case_id == case_id)
        .order_by(Verdict.created_at.desc(), Verdict.id.desc())
        .limit(1)
    )


def notice_basis(session: Session, case_id: uuid.UUID) -> NoticeBasis:
    verdict = newest_verdict(session, case_id)
    return verdict.suggested_basis if verdict is not None else NoticeBasis.NONE


def _signals(session: Session, case_id: uuid.UUID) -> dict[str, object]:
    found: dict[str, object] = {}
    rows = session.execute(
        select(Signal.name, Signal.value)
        .where(Signal.case_id == case_id, Signal.name.in_(ALLOWED_SIGNALS))
        .order_by(Signal.created_at)
    ).all()
    for name, value in rows:
        if isinstance(value, bool | int | float) and (name not in found or value is True):
            found[name] = value
    return found


def notice_facts(session: Session, case: Case) -> TextInputs:
    domain = session.get_one(Domain, case.domain_id)
    brand = session.get_one(Brand, case.brand_id)
    official = session.scalar(
        select(OfficialDomain.domain)
        .where(OfficialDomain.brand_id == brand.id)
        .order_by(OfficialDomain.kind != OfficialDomainKind.PRIMARY, OfficialDomain.domain)
        .limit(1)
    )
    final_url = session.scalar(
        select(Scan.final_url)
        .where(Scan.case_id == case.id, Scan.final_url.is_not(None))
        .order_by(Scan.stage == ScanStage.HTTP, Scan.started_at.desc())
        .limit(1)
    )
    url = clean_url(final_url, domain.name)
    shots = session.execute(
        select(Artifact.profile, Artifact.sha256)
        .where(Artifact.case_id == case.id, Artifact.kind == ArtifactKind.SCREENSHOT)
        .order_by(Artifact.created_at.desc())
    ).all()
    verdict = newest_verdict(session, case.id)
    signals = _signals(session, case.id)
    similarity = signals.get("visual_similarity")
    facts = NoticeFacts(
        case_number=case.number,
        brand=brand.name,
        official_domain=official,
        domain=domain.name,
        domain_unicode=domain.unicode_name,
        tld=domain.tld,
        url=url,
        final_host=url.split("/")[2],
        ips=tuple(str(ip) for ip in domain.ips[:6]),
        first_seen=case.first_seen_at,
        category=(case.category.value if case.category else "phishing_clone"),
        copied_elements=tuple(verdict.copied_elements) if verdict else (),
        collects=bool(verdict and verdict.collects_credentials_or_payment),
        visual_similarity=(
            float(similarity)
            if isinstance(similarity, int | float) and not isinstance(similarity, bool)
            else None
        ),
        domain_age_days=domain_age_days(domain.registered_at, case.first_seen_at),
        screenshots=screenshot_hashes(
            [(profile.value if profile else None, digest) for profile, digest in shots]
        ),
    )
    return TextInputs(
        facts=facts,
        signals=signals,
        registered_on=domain.registered_at.date().isoformat() if domain.registered_at else None,
        registrar=domain.registrar,
        hosting_org=domain.origin_org if domain.origin_source else domain.hosting_org,
    )


def channel_facts(session: Session, case: Case, basis: NoticeBasis) -> ChannelFacts:
    domain = session.get_one(Domain, case.domain_id)
    keys = [
        (ContactKind.REGISTRAR, str(domain.registrar_iana_id or "")),
        (ContactKind.HOST, str(domain.asn or "")),
        (ContactKind.REGISTRY, (domain.tld or "").lower()),
    ]
    rows = session.scalars(
        select(ChannelContact).where(
            or_(
                *(
                    and_(ChannelContact.kind == kind, ChannelContact.key == key)
                    for kind, key in keys
                )
            )
        )
    )
    contacts = {(row.kind, row.key): Contact(row.email, row.form_url, row.language) for row in rows}
    return ChannelFacts(
        basis=basis,
        tld=domain.tld,
        registrar_iana_id=domain.registrar_iana_id,
        registrar_abuse_email=domain.registrar_abuse_email,
        behind_proxy=domain.is_cloudflare,
        asn=domain.asn,
        hosting_abuse_email=domain.hosting_abuse_email,
        hosting_country=domain.hosting_country,
        origin_known=domain.origin_source is OriginSource.OPERATOR,
        origin_abuse_email=domain.origin_abuse_email,
        origin_country=domain.origin_country,
        contacts=contacts,
    )
