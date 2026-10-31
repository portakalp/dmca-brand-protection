"""Operator actions on customers, brands, domains, the whitelist and staff accounts.

Used by the ``kalkan`` CLI and the ops panel (K53). Every function takes a kalkan_worker
session, changes nothing outside it and writes an ``audit_log`` row; files (logos, the LOA)
are stored by the caller first.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalkan.config import Settings
from kalkan.db.base import utcnow
from kalkan.db.models import (
    AuditLog,
    Brand,
    BrandAsset,
    BrandMatch,
    ChannelContact,
    Customer,
    Invitation,
    OfficialDomain,
    StaffUser,
    WhitelistEntry,
)
from kalkan.db.outbox import (
    CLEAR_BLOCKED_CASES,
    ENRICH_DOMAIN,
    HASH_BRAND_ASSETS,
    REFRESH_BRAND_FAVICONS,
    REFRESH_BRAND_REFERENCES,
    WHITELIST_CHANGED,
    queue_job,
)
from kalkan.discovery.catalog import profile_for
from kalkan.discovery.matcher import Matcher, MatchResult
from kalkan.discovery.normalize import view_host
from kalkan.discovery.permutations import refresh_brand_permutations
from kalkan.discovery.record import MANUAL_SCORE, Sighting, record_discovery, upsert_domain
from kalkan.domain.emails import clean_email
from kalkan.domain.enums import (
    BrandAssetKind,
    ContactKind,
    CustomerStatus,
    Language,
    MatchReason,
    OfficialDomainKind,
    SightingSource,
    UserRole,
    WhitelistKind,
    WhitelistSource,
)
from kalkan.domain.hostnames import InvalidHostnameError
from kalkan.enrichment.whitelist import validate_pattern
from kalkan.security import (
    encrypt_totp_secret,
    hash_password,
    hash_token,
    new_token,
    new_totp_secret,
)
from kalkan.storage.gcs import StoredObject

MIN_PASSWORD_LENGTH = 12
MIN_KEYWORD_LENGTH = 4  # architecture.md §8: shorter keywords match too much
INVITATION_TTL = timedelta(hours=24)
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Letters that Unicode decomposition does not reduce to ASCII (Polish, Turkish, German, ...).
_ASCII_FOLD = str.maketrans(
    {
        "\u0142": "l",
        "\u0141": "L",
        "\u0131": "i",
        "\u00f8": "o",
        "\u00d8": "O",
        "\u0111": "d",
        "\u00df": "ss",
    }
)


class AdminError(ValueError):
    """An operator action that cannot be carried out; the message is shown to the operator."""


def _audit(
    session: Session,
    actor: str,
    action: str,
    *,
    customer_id: uuid.UUID | None = None,
    target: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLog(
            actor=actor, action=action, customer_id=customer_id, target=target, data=data or {}
        )
    )


def ascii_fold(text: str) -> str:
    """``Güven Finans`` -> ``Guven Finans``; letters without an ASCII form are dropped."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_ASCII_FOLD))
    return decomposed.encode("ascii", "ignore").decode("ascii")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", ascii_fold(name).lower()).strip("-")


def get_customer(session: Session, slug: str) -> Customer:
    customer = session.scalar(select(Customer).where(Customer.slug == slug))
    if customer is None:
        raise AdminError(f"no customer with slug {slug!r}")
    return customer


def get_brand(session: Session, brand_id: uuid.UUID) -> Brand:
    brand = session.get(Brand, brand_id)
    if brand is None:
        raise AdminError(f"no brand with id {brand_id}")
    return brand


def create_customer(
    session: Session,
    *,
    name: str,
    actor: str,
    slug: str | None = None,
    monitor_only: bool = False,
    demo: bool = False,
    alert_email: str | None = None,
    timezone: str = "UTC",
) -> Customer:
    """A new customer starts in ``trial`` with sending disabled and dry-run on (product.md §8)."""
    slug = slug or slugify(name)
    if not _SLUG.match(slug):
        raise AdminError(f"invalid slug {slug!r}: use lower-case letters, digits and hyphens")
    if session.scalar(select(Customer.id).where(Customer.slug == slug)) is not None:
        raise AdminError(f"slug {slug!r} is already taken")
    customer = Customer(
        name=name,
        slug=slug,
        monitor_only=monitor_only,
        is_demo=demo,
        alert_email=alert_email,
        timezone=timezone,
    )
    session.add(customer)
    session.flush()
    _audit(
        session,
        actor,
        "customer.create",
        customer_id=customer.id,
        target=slug,
        data={"monitor_only": monitor_only, "is_demo": demo},
    )
    return customer


def add_brand(
    session: Session,
    customer: Customer,
    *,
    name: str,
    actor: str,
    keywords: Sequence[str] = (),
    variants: Sequence[str] = (),
    target_markets: Sequence[str] = (),
    languages: Sequence[str] = (),
) -> Brand:
    markets = [market.strip().upper() for market in target_markets]
    if any(len(market) != 2 or not market.isalpha() for market in markets):
        raise AdminError("target markets are ISO 3166-1 alpha-2 codes, e.g. PL")
    words = [keyword.strip().lower() for keyword in keywords] or [slugify(name).replace("-", "")]
    short = [word for word in words if len(word) < MIN_KEYWORD_LENGTH]
    if short:
        listed = ", ".join(repr(word) for word in short)
        raise AdminError(
            f"keywords need at least {MIN_KEYWORD_LENGTH} characters ({listed}); "
            "pass --keyword explicitly"
        )
    brand = Brand(
        customer_id=customer.id,
        name=name,
        keywords=words,
        variants=[variant.strip() for variant in variants],
        target_markets=markets,
        languages=[language.strip().lower() for language in languages],
    )
    session.add(brand)
    session.flush()
    _audit(session, actor, "brand.add", customer_id=customer.id, target=str(brand.id))
    return brand


def add_official_domain(
    session: Session,
    brand: Brand,
    domain: str,
    *,
    actor: str,
    kind: OfficialDomainKind = OfficialDomainKind.PRIMARY,
) -> OfficialDomain:
    """Add an official domain; it is also listed in the whitelist as ``official`` (§9)."""
    try:
        host = validate_pattern(domain)
    except InvalidHostnameError as exc:
        raise AdminError(str(exc)) from exc
    existing = session.scalar(
        select(OfficialDomain).where(
            OfficialDomain.brand_id == brand.id, OfficialDomain.domain == host
        )
    )
    if existing is not None:
        return existing
    official = OfficialDomain(
        customer_id=brand.customer_id, brand_id=brand.id, domain=host, kind=kind
    )
    session.add(official)
    if _find_whitelist(session, brand.customer_id, brand.id, host) is None:
        session.add(
            WhitelistEntry(
                customer_id=brand.customer_id,
                brand_id=brand.id,
                pattern=host,
                kind=WhitelistKind.OFFICIAL,
                source=WhitelistSource.ONBOARDING,
                note=f"Official domain ({kind.value})",
                created_by=actor,
            )
        )
    session.flush()
    customer = session.get(Customer, brand.customer_id)
    data: dict[str, Any] = {}
    # The demo workspace is frozen (K46): no permutations, enrichment or case re-checks.
    if customer is not None and not customer.is_demo:
        # dnstwist permutations of the new domain (§8).
        data["permutations"] = refresh_brand_permutations(session, brand).total
        # Its DNS footprint feeds the official_infrastructure signal (K57), and open cases
        # on the domain are dismissed now that it is whitelisted (§9).
        domain_id, _ = upsert_domain(session, view_host(host), utcnow())
        queue_job(session, ENRICH_DOMAIN, domain_id=domain_id)
        queue_job(session, WHITELIST_CHANGED, customer_id=brand.customer_id)
        # The pre-check compares candidates' favicons with the brand's (§10).
        queue_job(session, REFRESH_BRAND_FAVICONS, brand_id=brand.id, domain=host)
        # Reference screenshots come from the primary official site (§10; K100).
        if kind is OfficialDomainKind.PRIMARY:
            queue_job(session, REFRESH_BRAND_REFERENCES, brand_id=brand.id)
    _audit(
        session,
        actor,
        "official_domain.add",
        customer_id=brand.customer_id,
        target=host,
        data=data,
    )
    return official


# --- customer flags, LOA and brand assets (ops panel, §8, §15) -------------------------------

FLAGS = ("monitor_only", "is_test", "sending_enabled", "dry_run")
# The value of each flag that lets notices through; changing a flag to it lifts a gate (§15).
_OPEN_VALUE = {"monitor_only": False, "is_test": False, "sending_enabled": True, "dry_run": False}
ASSET_KINDS = (
    BrandAssetKind.LOGO_LIGHT,
    BrandAssetKind.LOGO_DARK,
    BrandAssetKind.FAVICON,
    BrandAssetKind.CAMPAIGN,
)


@dataclass(frozen=True)
class FlagChange:
    flag: str
    old: bool
    new: bool
    lifts_block: bool


def set_customer_flag(
    session: Session, customer: Customer, flag: str, value: bool, *, actor: str
) -> FlagChange | None:
    """Change one sending flag; ``None`` when it already has the value.

    A test customer always stays in dry-run (§15). A change that lifts a gate leaves a
    ``clear_blocked_cases`` job (§7).
    """
    if flag not in FLAGS:
        raise AdminError(f"unknown flag {flag!r}")
    old = bool(getattr(customer, flag))
    if old == value:
        return None
    if flag == "is_test" and value and not customer.dry_run:
        raise AdminError("turn dry-run on before marking the customer as a test customer")
    if flag == "dry_run" and not value and customer.is_test:
        raise AdminError("a test customer always stays in dry-run; clear is_test first")
    setattr(customer, flag, value)
    lifts = value == _OPEN_VALUE[flag]
    if lifts:
        queue_job(session, CLEAR_BLOCKED_CASES, customer_id=customer.id)
    session.flush()
    _audit(
        session,
        actor,
        f"customer.{flag}",
        customer_id=customer.id,
        target=customer.slug,
        data={"old": old, "new": value},
    )
    return FlagChange(flag, old, value, lifts)


def set_customer_status(
    session: Session, customer: Customer, status: CustomerStatus, *, actor: str
) -> bool:
    """``trial``, ``active`` or ``suspended``; ``False`` when it already had the status.

    Leaving ``suspended`` lifts the ``suspended`` sending gate, so a ``clear_blocked_cases`` job
    is left (§7).
    """
    if customer.is_demo:
        raise AdminError("the demo workspace keeps its status")
    old = customer.status
    if old is status:
        return False
    customer.status = status
    if old is CustomerStatus.SUSPENDED:
        queue_job(session, CLEAR_BLOCKED_CASES, customer_id=customer.id)
    session.flush()
    _audit(
        session,
        actor,
        "customer.status",
        customer_id=customer.id,
        target=customer.slug,
        data={"old": old.value, "new": status.value, "reason": "operator"},
    )
    return True


def record_loa(
    session: Session,
    customer: Customer,
    *,
    file_key: str,
    signed_at: datetime,
    actor: str,
) -> None:
    """The signed letter of authority (stored by the caller); lifts the ``no_loa`` gate."""
    old = customer.loa_signed_at
    customer.loa_file_key = file_key
    customer.loa_signed_at = signed_at
    queue_job(session, CLEAR_BLOCKED_CASES, customer_id=customer.id)
    session.flush()
    _audit(
        session,
        actor,
        "customer.loa",
        customer_id=customer.id,
        target=customer.slug,
        data={
            "old_signed_at": old.isoformat() if old else None,
            "signed_at": signed_at.isoformat(),
            "file_key": file_key,
        },
    )


def record_brand_asset(
    session: Session,
    brand: Brand,
    kind: BrandAssetKind,
    stored: StoredObject,
    *,
    actor: str,
    filename: str | None = None,
) -> BrandAsset:
    """A logo, favicon or campaign image the caller stored; its hashes are computed later."""
    if kind not in ASSET_KINDS:
        raise AdminError(f"{kind.value} assets are not uploaded")
    asset = BrandAsset(
        customer_id=brand.customer_id,
        brand_id=brand.id,
        kind=kind,
        storage_key=stored.key,
        sha256=stored.sha256,
    )
    session.add(asset)
    session.flush()
    customer = session.get(Customer, brand.customer_id)
    if customer is not None and not customer.is_demo:
        queue_job(session, HASH_BRAND_ASSETS, brand_id=brand.id)
    _audit(
        session,
        actor,
        "brand_asset.add",
        customer_id=brand.customer_id,
        target=str(asset.id),
        data={"kind": kind.value, "sha256": stored.sha256, "filename": filename},
    )
    return asset


def refresh_references(session: Session, brand: Brand, *, actor: str) -> None:
    """New reference screenshots of the brand's primary official site (§10)."""
    queue_job(session, REFRESH_BRAND_REFERENCES, brand_id=brand.id)
    _audit(session, actor, "brand.references", customer_id=brand.customer_id, target=str(brand.id))


@dataclass(frozen=True)
class NewInvitation:
    invitation: Invitation
    token: str


def invite_user(
    session: Session,
    customer: Customer,
    email: str,
    *,
    actor: str,
    role: UserRole = UserRole.OWNER,
    now: datetime | None = None,
) -> NewInvitation:
    """A single-use invitation valid for 24 hours (§17); the token is shown once."""
    address = clean_email(email)
    if address is None:
        raise AdminError(f"invalid e-mail address: {email!r}")
    token = new_token()
    invitation = Invitation(
        customer_id=customer.id,
        email=address,
        role=role,
        token_hash=hash_token(token),
        expires_at=(now or utcnow()) + INVITATION_TTL,
        invited_by=actor,
    )
    session.add(invitation)
    session.flush()
    _audit(
        session,
        actor,
        "invitation.create",
        customer_id=customer.id,
        target=address,
        data={"role": role.value},
    )
    return NewInvitation(invitation, token)


def _find_whitelist(
    session: Session, customer_id: uuid.UUID, brand_id: uuid.UUID | None, pattern: str
) -> WhitelistEntry | None:
    brand_filter = (
        WhitelistEntry.brand_id.is_(None)
        if brand_id is None
        else WhitelistEntry.brand_id == brand_id
    )
    return session.scalar(
        select(WhitelistEntry).where(
            WhitelistEntry.customer_id == customer_id,
            brand_filter,
            WhitelistEntry.pattern == pattern,
        )
    )


@dataclass(frozen=True)
class RowError:
    line: int
    message: str


@dataclass
class WhitelistImport:
    imported: int = 0
    already_listed: int = 0
    errors: list[RowError] = field(default_factory=list)


def import_whitelist(
    session: Session,
    customer: Customer,
    csv_text: str,
    *,
    actor: str,
    brand: Brand | None = None,
) -> WhitelistImport:
    """Import ``pattern,kind,note`` rows. Nothing is imported when any row is invalid.

    ``kind`` is one of official, mirror, affiliate, partner, social, other (default other).
    Patterns already listed for the same scope are counted, not duplicated.
    """
    report = WhitelistImport()
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None or "pattern" not in [f.strip().lower() for f in reader.fieldnames]:
        report.errors.append(RowError(1, "the header row must contain a 'pattern' column"))
        return report

    seen: set[str] = set()
    rows: list[tuple[str, WhitelistKind, str | None]] = []
    for row in reader:
        line = reader.line_num
        values = {key.strip().lower(): (value or "").strip() for key, value in row.items() if key}
        try:
            pattern = validate_pattern(values.get("pattern", ""))
        except InvalidHostnameError as exc:
            report.errors.append(RowError(line, str(exc)))
            continue
        kind_text = values.get("kind", "").lower() or WhitelistKind.OTHER.value
        try:
            kind = WhitelistKind(kind_text)
        except ValueError:
            allowed = ", ".join(k.value for k in WhitelistKind)
            report.errors.append(
                RowError(line, f"unknown kind {kind_text!r}; use one of {allowed}")
            )
            continue
        if pattern in seen:
            report.errors.append(
                RowError(line, f"{pattern} appears more than once in the file (www. is dropped)")
            )
            continue
        seen.add(pattern)
        rows.append((pattern, kind, values.get("note") or None))

    if report.errors:
        return report

    brand_id = brand.id if brand is not None else None
    for pattern, kind, note in rows:
        if _find_whitelist(session, customer.id, brand_id, pattern) is not None:
            report.already_listed += 1
            continue
        session.add(
            WhitelistEntry(
                customer_id=customer.id,
                brand_id=brand_id,
                pattern=pattern,
                kind=kind,
                source=WhitelistSource.CSV,
                note=note,
                created_by=actor,
            )
        )
        report.imported += 1
    if report.imported:
        # Open cases that the new entries cover are dismissed by the worker (§9).
        queue_job(session, WHITELIST_CHANGED, customer_id=customer.id)
    session.flush()
    _audit(
        session,
        actor,
        "whitelist.import",
        customer_id=customer.id,
        data={"imported": report.imported, "already_listed": report.already_listed},
    )
    return report


@dataclass(frozen=True)
class NewStaffUser:
    user: StaffUser
    totp_secret: str


def create_staff_user(
    session: Session, settings: Settings, *, email: str, password: str, actor: str
) -> NewStaffUser:
    """Create an operator account with a new TOTP secret (shown once, stored encrypted)."""
    email = email.strip().lower()
    if "@" not in email:
        raise AdminError(f"invalid e-mail address: {email!r}")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AdminError(f"the password must be at least {MIN_PASSWORD_LENGTH} characters")
    if session.scalar(select(StaffUser.id).where(StaffUser.email == email)) is not None:
        raise AdminError(f"a staff user with e-mail {email} already exists")
    secret = new_totp_secret()
    user = StaffUser(
        email=email,
        password_hash=hash_password(password),
        totp_secret_enc=encrypt_totp_secret(settings, secret),
    )
    session.add(user)
    session.flush()
    _audit(session, actor, "staff.create", target=email)
    return NewStaffUser(user, secret)


@dataclass(frozen=True)
class CandidateResult:
    match_id: uuid.UUID
    host: str
    created: bool
    reasons: tuple[MatchReason, ...]


def candidate_host(url: str) -> str:
    """The host of ``url``; a bare host name is accepted too.

    Backslashes, white space, control characters and user information are refused: browsers
    and Python's URL parser read such URLs differently, so the host would be ambiguous.
    """
    value = url.strip()
    if any(char == "\\" or char.isspace() or ord(char) < 32 for char in value):
        raise AdminError(f"the URL contains a backslash, space or control character: {url!r}")
    parts = urlsplit(value if "://" in value else f"http://{value}")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AdminError(f"not an http(s) URL or host name: {url!r}")
    if "@" in parts.netloc:
        raise AdminError(f"URLs with user information are not accepted; pass the host: {url!r}")
    return parts.hostname


def add_candidate(
    session: Session,
    brand: Brand,
    url: str,
    *,
    actor: str,
    now: datetime | None = None,
) -> CandidateResult:
    """Record a candidate an operator found (source ``manual``); the caller queues admission.

    The match keeps the matcher's reasons next to ``manual`` and scores 100. An existing
    match of the same brand and domain is raised to 100 and gains the ``manual`` reason.
    """
    customer = session.get(Customer, brand.customer_id)
    if customer is None or customer.is_demo:
        raise AdminError("the demo workspace is frozen; candidates cannot be added to it")
    try:
        view = view_host(candidate_host(url))
    except InvalidHostnameError as exc:
        raise AdminError(str(exc)) from exc
    found = Matcher([profile_for(session, brand)]).match(view)
    reasons = (MatchReason.MANUAL, *(found[0].reasons if found else ()))
    seen_at = now or utcnow()
    recorded = record_discovery(
        session,
        view,
        [MatchResult(brand.id, brand.customer_id, MANUAL_SCORE, reasons)],
        Sighting(SightingSource.MANUAL, seen_at, {"url": url.strip(), "added_by": actor}),
    )
    if recorded.new_match_ids:
        match_id, created = recorded.new_match_ids[0], True
    else:
        existing = session.scalar(
            select(BrandMatch.id).where(
                BrandMatch.brand_id == brand.id, BrandMatch.domain_id == recorded.domain_id
            )
        )
        if existing is None:  # pragma: no cover - record_discovery either created or found it
            raise AdminError(f"no match recorded for {view.host}")
        match = session.get(BrandMatch, existing)
        if match is None:  # pragma: no cover - selected just above in this transaction
            raise AdminError(f"no match recorded for {view.host}")
        known = [MatchReason(reason) for reason in match.reasons]
        reasons = tuple(sorted({*known, *reasons}, key=list(MatchReason).index))
        match.score = MANUAL_SCORE
        match.reasons = [str(reason) for reason in reasons]
        match_id, created = existing, False
    _audit(
        session,
        actor,
        "candidate.add",
        customer_id=brand.customer_id,
        target=view.host,
        data={"brand_id": str(brand.id), "url": url.strip(), "created": created},
    )
    return CandidateResult(match_id, view.host, created, reasons)


TLD_LABEL = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")


def registry_key(value: str) -> str:
    """A TLD as a lower-case A-label without dots: ``.APP`` → ``app``, ``рф`` → ``xn--p1ai``."""
    raw = value.strip().strip(".").lower()
    try:
        label = raw.encode("idna").decode("ascii")
    except UnicodeError:
        raise AdminError(f"not a top-level domain: {value!r}") from None
    if not TLD_LABEL.match(label):
        raise AdminError(f"not a top-level domain: {value!r}")
    return label


def set_channel_contact(
    session: Session,
    *,
    kind: ContactKind,
    key: str,
    actor: str,
    email: str | None = None,
    form_url: str | None = None,
    language: Language | None = None,
    notes: str | None = None,
) -> ChannelContact:
    """Override the abuse contact of a host (key: ASN), a registrar (key: IANA id) or a
    registry (key: the TLD, e.g. ``online``; the only source of registry contacts, K136) (§9)."""
    if kind is ContactKind.REGISTRY:
        key = registry_key(key)
    else:
        key = key.strip().upper().removeprefix("AS")
        if not key.isdigit():
            raise AdminError(f"the key is an ASN or an IANA registrar id (digits): {key!r}")
        key = str(int(key))  # "024940" and "24940" are the same ASN
    if email is None and form_url is None:
        raise AdminError("give an e-mail address, a form URL or both")
    if email is not None:
        cleaned = clean_email(email)
        if cleaned is None or cleaned != email.strip().lower():
            raise AdminError(f"not an e-mail address: {email!r}")
        email = cleaned
    if form_url is not None and not form_url.startswith("https://"):
        raise AdminError("the form URL must start with https://")
    contact = session.scalar(
        select(ChannelContact).where(ChannelContact.kind == kind, ChannelContact.key == key)
    )
    if contact is None:
        contact = ChannelContact(kind=kind, key=key)
        session.add(contact)
    contact.email = email
    contact.form_url = form_url
    contact.language = language
    contact.notes = notes
    session.flush()
    _audit(
        session,
        actor,
        "channel_contact.set",
        target=f"{kind.value}:{key}",
        data={"email": email, "form_url": form_url},
    )
    return contact
