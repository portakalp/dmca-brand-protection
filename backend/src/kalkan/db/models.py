"""Database models (architecture.md §5).

Global tables have no tenant. Tenant tables carry ``customer_id`` and are protected by row
level security (§17). References between tenant tables are composite foreign keys on
``(customer_id, <id>)``, so a row can never point at another tenant's row.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Sequence,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kalkan.db.base import (
    Base,
    created_at_column,
    enum_column,
    id_column,
    lift_column_checks,
    updated_at_column,
    utcnow,
)
from kalkan.domain.enums import (
    AiPurpose,
    ArtifactKind,
    AuthScope,
    BlockedReason,
    BrandAssetKind,
    CaseState,
    Category,
    Channel,
    ContactKind,
    CustomerStatus,
    DecisionOutcome,
    EventType,
    ExplanationSource,
    Language,
    MessageDirection,
    NoticeBasis,
    NoticeMethod,
    NoticeStatus,
    NoticeTemplate,
    OfficialDomainKind,
    OriginSource,
    Profile,
    RecheckMethod,
    RecheckResult,
    ReplyClass,
    ScanStage,
    SightingSource,
    TaskKind,
    TaskStatus,
    UserRole,
    VerdictSource,
    WebhookSource,
    WhitelistKind,
    WhitelistSource,
)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

CASE_NUMBER_SEQUENCE = Sequence("case_number_seq", metadata=Base.metadata)

# Tables kalkan_api may only INSERT into (no SELECT), so inserts must not use RETURNING.
NO_RETURNING: dict[str, Any] = {"eager_defaults": False}


def _text_array() -> Mapped[list[str]]:
    return mapped_column(ARRAY(Text), default=list, server_default=text("'{}'"))


def _tenant_fk(column: str, parent: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["customer_id", column], [f"{parent}.customer_id", f"{parent}.id"])


def _tenant_key() -> UniqueConstraint:
    """Target of composite tenant foreign keys."""
    return UniqueConstraint("customer_id", "id")


class TenantMixin:
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))


# --------------------------------------------------------------------------------------------
# Global tables
# --------------------------------------------------------------------------------------------


class Domain(Base):
    __tablename__ = "domains"
    __table_args__ = (
        Index(
            "ix_domains_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    name: Mapped[str] = mapped_column(Text, unique=True)  # punycode, lower case
    unicode_name: Mapped[str | None] = mapped_column(Text)
    registered_domain: Mapped[str | None] = mapped_column(Text, index=True)
    tld: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    registrar: Mapped[str | None] = mapped_column(Text)
    registrar_iana_id: Mapped[int | None] = mapped_column(Integer)
    registrar_abuse_email: Mapped[str | None] = mapped_column(Text)
    registered_at: Mapped[datetime | None]
    nameservers: Mapped[list[str]] = _text_array()
    ips: Mapped[list[IPAddress]] = mapped_column(
        ARRAY(INET), default=list, server_default=text("'{}'")
    )
    is_cloudflare: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    asn: Mapped[int | None] = mapped_column(BigInteger)
    hosting_org: Mapped[str | None] = mapped_column(Text)
    hosting_country: Mapped[str | None] = mapped_column(Text)
    hosting_abuse_email: Mapped[str | None] = mapped_column(Text)
    # The origin host behind a proxy such as Cloudflare, once disclosed (K37).
    origin_ip: Mapped[IPAddress | None] = mapped_column(INET)
    origin_org: Mapped[str | None] = mapped_column(Text)
    origin_country: Mapped[str | None] = mapped_column(Text)
    origin_abuse_email: Mapped[str | None] = mapped_column(Text)
    origin_source: Mapped[OriginSource | None] = enum_column(
        OriginSource, "origin_source", nullable=True
    )
    webrisk: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    enriched_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_column()


class DomainSighting(Base):
    __tablename__ = "domain_sightings"
    __table_args__ = (Index(None, "domain_id", "seen_at"),)

    id: Mapped[uuid.UUID] = id_column()
    domain_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("domains.id"))
    source: Mapped[SightingSource] = enum_column(SightingSource, "source")
    seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class ChannelContact(Base):
    __tablename__ = "channel_contacts"
    __table_args__ = (UniqueConstraint("kind", "key"),)

    id: Mapped[uuid.UUID] = id_column()
    kind: Mapped[ContactKind] = enum_column(ContactKind, "kind")
    key: Mapped[str] = mapped_column(Text)  # ASN or IANA registrar id
    email: Mapped[str | None] = mapped_column(Text)
    form_url: Mapped[str | None] = mapped_column(Text)
    language: Mapped[Language | None] = enum_column(Language, "language", nullable=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class StaffUser(Base):
    __tablename__ = "staff_users"

    id: Mapped[uuid.UUID] = id_column()
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    totp_secret_enc: Mapped[str] = mapped_column(Text)
    last_login_at: Mapped[datetime | None]
    disabled_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_column()


class StaffSession(Base):
    __tablename__ = "staff_sessions"

    id: Mapped[uuid.UUID] = id_column()
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    staff_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff_users.id"), index=True)
    mfa_passed: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    created_at: Mapped[datetime] = created_at_column()
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]


class AuthAttempt(Base):
    __tablename__ = "auth_attempts"
    __table_args__ = (Index(None, "email", "at"), Index(None, "ip", "at"))
    __mapper_args__ = NO_RETURNING

    id: Mapped[uuid.UUID] = id_column()
    scope: Mapped[AuthScope] = enum_column(AuthScope, "scope")
    email: Mapped[str | None] = mapped_column(CITEXT)
    ip: Mapped[IPAddress | None] = mapped_column(INET)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    success: Mapped[bool]


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = updated_at_column()
    updated_by: Mapped[str | None] = mapped_column(Text)


class JobOutbox(Base):
    __tablename__ = "jobs_outbox"
    __table_args__ = (
        Index(
            "ix_jobs_outbox_pending",
            "created_at",
            postgresql_where=text("dispatched_at IS NULL"),
        ),
    )
    __mapper_args__ = NO_RETURNING

    id: Mapped[uuid.UUID] = id_column()
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = created_at_column()
    dispatched_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        Index(
            "ix_webhook_events_unprocessed",
            "received_at",
            postgresql_where=text("processed_at IS NULL"),
        ),
    )
    __mapper_args__ = NO_RETURNING

    id: Mapped[uuid.UUID] = id_column()
    source: Mapped[WebhookSource] = enum_column(WebhookSource, "source")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    signature_ok: Mapped[bool]
    received_at: Mapped[datetime] = mapped_column(default=utcnow)
    processed_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)


class AiCall(Base):
    __tablename__ = "ai_calls"

    id: Mapped[uuid.UUID] = id_column()
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id"), index=True)
    purpose: Mapped[AiPurpose] = enum_column(AiPurpose, "purpose")
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    cache_write_tokens: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal(0), server_default=text("0")
    )
    latency_ms: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class Label(Base):
    __tablename__ = "labels"

    id: Mapped[uuid.UUID] = id_column()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), unique=True)
    category: Mapped[Category] = enum_column(Category, "category")
    labeled_by: Mapped[str] = mapped_column(Text)
    labeled_at: Mapped[datetime] = mapped_column(default=utcnow)
    note: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index(None, "customer_id", "at"),)
    __mapper_args__ = NO_RETURNING

    id: Mapped[uuid.UUID] = id_column()
    at: Mapped[datetime] = mapped_column(default=utcnow)
    actor: Mapped[str] = mapped_column(Text)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))
    action: Mapped[str] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    ip: Mapped[IPAddress | None] = mapped_column(INET)


# --------------------------------------------------------------------------------------------
# Tenant tables (customer_id + RLS)
# --------------------------------------------------------------------------------------------


class Customer(Base):
    """The tenant itself; its RLS policy compares ``id``."""

    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint("NOT is_test OR dry_run", name="test_customer_stays_dry_run"),
    )

    id: Mapped[uuid.UUID] = id_column()
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[CustomerStatus] = enum_column(
        CustomerStatus, "status", default=CustomerStatus.TRIAL
    )
    sending_enabled: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    dry_run: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    is_test: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    monitor_only: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    loa_signed_at: Mapped[datetime | None]
    loa_file_key: Mapped[str | None] = mapped_column(Text)
    alert_email: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, default="UTC", server_default="UTC")
    is_demo: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class User(TenantMixin, Base):
    __tablename__ = "users"
    __table_args__ = (_tenant_key(),)

    id: Mapped[uuid.UUID] = id_column()
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text)  # shown in the panel (K45)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[UserRole] = enum_column(UserRole, "role")
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_enabled_at: Mapped[datetime | None]
    recovery_codes_hash: Mapped[list[str]] = _text_array()
    last_login_at: Mapped[datetime | None]
    disabled_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class UserSession(TenantMixin, Base):
    __tablename__ = "sessions"
    __table_args__ = (_tenant_fk("user_id", "users"),)

    id: Mapped[uuid.UUID] = id_column()
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    mfa_passed: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    created_at: Mapped[datetime] = created_at_column()
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    ip: Mapped[IPAddress | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)


class Invitation(TenantMixin, Base):
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = id_column()
    email: Mapped[str] = mapped_column(CITEXT)
    role: Mapped[UserRole] = enum_column(UserRole, "role")
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
    invited_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class PasswordReset(TenantMixin, Base):
    __tablename__ = "password_resets"
    __table_args__ = (_tenant_fk("user_id", "users"),)

    id: Mapped[uuid.UUID] = id_column()
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_column()


class Brand(TenantMixin, Base):
    __tablename__ = "brands"
    __table_args__ = (_tenant_key(),)

    id: Mapped[uuid.UUID] = id_column()
    name: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list[str]] = _text_array()
    variants: Mapped[list[str]] = _text_array()
    target_markets: Mapped[list[str]] = _text_array()  # ISO 3166-1 alpha-2
    languages: Mapped[list[str]] = _text_array()
    active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class BrandAsset(TenantMixin, Base):
    __tablename__ = "brand_assets"
    __table_args__ = (_tenant_fk("brand_id", "brands"),)

    id: Mapped[uuid.UUID] = id_column()
    brand_id: Mapped[uuid.UUID] = mapped_column(index=True)
    kind: Mapped[BrandAssetKind] = enum_column(BrandAssetKind, "kind")
    storage_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    phash: Mapped[str | None] = mapped_column(Text)
    dhash: Mapped[str | None] = mapped_column(Text)
    favicon_mmh3: Mapped[int | None] = mapped_column(Integer)
    anthropic_file_id: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    # The official page's visible text, on reference screenshots only (K110).
    text_excerpt: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_column()


class OfficialDomain(TenantMixin, Base):
    __tablename__ = "official_domains"
    __table_args__ = (UniqueConstraint("brand_id", "domain"), _tenant_fk("brand_id", "brands"))

    id: Mapped[uuid.UUID] = id_column()
    brand_id: Mapped[uuid.UUID]
    domain: Mapped[str] = mapped_column(Text)
    kind: Mapped[OfficialDomainKind] = enum_column(OfficialDomainKind, "kind")
    created_at: Mapped[datetime] = created_at_column()


class BrandPermutation(TenantMixin, Base):
    __tablename__ = "brand_permutations"
    __table_args__ = (UniqueConstraint("brand_id", "domain"), _tenant_fk("brand_id", "brands"))

    id: Mapped[uuid.UUID] = id_column()
    brand_id: Mapped[uuid.UUID]
    domain: Mapped[str] = mapped_column(Text, index=True)
    fuzzer: Mapped[str] = mapped_column(Text)
    registered: Mapped[bool | None]
    checked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_column()


class WhitelistEntry(TenantMixin, Base):
    __tablename__ = "whitelist_entries"
    __table_args__ = (
        UniqueConstraint("customer_id", "brand_id", "pattern", postgresql_nulls_not_distinct=True),
        _tenant_fk("brand_id", "brands"),
    )

    id: Mapped[uuid.UUID] = id_column()
    brand_id: Mapped[uuid.UUID | None]  # empty: all brands of the customer
    pattern: Mapped[str] = mapped_column(Text)  # host; covers its subdomains
    kind: Mapped[WhitelistKind] = enum_column(WhitelistKind, "kind")
    source: Mapped[WhitelistSource] = enum_column(WhitelistSource, "source")
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class BrandMatch(TenantMixin, Base):
    __tablename__ = "brand_matches"
    __table_args__ = (
        UniqueConstraint("brand_id", "domain_id"),
        Index(
            "ix_brand_matches_unchecked",
            "first_matched_at",
            postgresql_where=text("checked_at IS NULL"),
        ),
        CheckConstraint("score BETWEEN 0 AND 100", name="score_range"),
        _tenant_fk("brand_id", "brands"),
    )

    id: Mapped[uuid.UUID] = id_column()
    domain_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("domains.id"), index=True)
    brand_id: Mapped[uuid.UUID]
    score: Mapped[int] = mapped_column(SmallInteger)
    reasons: Mapped[list[str]] = _text_array()
    first_matched_at: Mapped[datetime] = mapped_column(default=utcnow)
    admitted: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    # When admit_match checked the whitelist; empty means admission never ran (K62).
    checked_at: Mapped[datetime | None]


class Case(TenantMixin, Base):
    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("customer_id", "brand_id", "domain_id"),
        _tenant_key(),
        _tenant_fk("brand_id", "brands"),
        Index(None, "customer_id", "first_seen_at"),
        Index(None, "state", "next_check_at"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    number: Mapped[str] = mapped_column(Text, unique=True)
    brand_id: Mapped[uuid.UUID]
    domain_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("domains.id"), index=True)
    state: Mapped[CaseState] = enum_column(CaseState, "state", default=CaseState.DETECTED)
    category: Mapped[Category | None] = enum_column(Category, "category", nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    auto_approved: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    blocked_reason: Mapped[BlockedReason | None] = enum_column(
        BlockedReason, "blocked_reason", nullable=True
    )
    first_seen_at: Mapped[datetime]
    verified_at: Mapped[datetime | None]
    approved_at: Mapped[datetime | None]
    approved_by: Mapped[str | None] = mapped_column(Text)  # "rule" or staff e-mail
    first_notice_at: Mapped[datetime | None]
    offline_since: Mapped[datetime | None]
    taken_down_at: Mapped[datetime | None]
    monitor_until: Mapped[datetime | None]
    reopened_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # The customer's "verified phishing" alert went out (§15, K141); it goes once.
    customer_alert_sent_at: Mapped[datetime | None]
    next_check_at: Mapped[datetime | None]
    last_action: Mapped[str | None] = mapped_column(Text)  # short English text (K39)
    last_action_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class CaseEvent(TenantMixin, Base):
    __tablename__ = "case_events"
    __table_args__ = (_tenant_fk("case_id", "cases"), Index(None, "case_id", "at"))

    id: Mapped[uuid.UUID] = id_column()
    case_id: Mapped[uuid.UUID]
    at: Mapped[datetime] = mapped_column(default=utcnow)
    type: Mapped[EventType] = enum_column(EventType, "type")
    actor: Mapped[str] = mapped_column(Text)  # "system", "rule", a staff or customer e-mail
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class Scan(TenantMixin, Base):
    __tablename__ = "scans"
    __table_args__ = (_tenant_key(), _tenant_fk("case_id", "cases"), Index(None, "case_id"))

    id: Mapped[uuid.UUID] = id_column()
    case_id: Mapped[uuid.UUID]
    stage: Mapped[ScanStage] = enum_column(ScanStage, "stage")
    profile: Mapped[Profile | None] = enum_column(Profile, "profile", nullable=True)
    proxy_country: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    outcome: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    final_url: Mapped[str | None] = mapped_column(Text)
    redirect_chain: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    title: Mapped[str | None] = mapped_column(Text)
    text_excerpt: Mapped[str | None] = mapped_column(Text)
    forms: Mapped[list[Any]] = mapped_column(JSONB, default=list, server_default="[]")
    # Browser buttons outside forms (K111); forms carry their own.
    buttons: Mapped[list[Any]] = mapped_column(JSONB, default=list, server_default="[]")
    error: Mapped[str | None] = mapped_column(Text)


class Artifact(TenantMixin, Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        _tenant_fk("case_id", "cases"),
        _tenant_fk("scan_id", "scans"),
        Index(None, "case_id"),
    )

    id: Mapped[uuid.UUID] = id_column()
    case_id: Mapped[uuid.UUID]
    scan_id: Mapped[uuid.UUID | None]
    kind: Mapped[ArtifactKind] = enum_column(ArtifactKind, "kind")
    profile: Mapped[Profile | None] = enum_column(Profile, "profile", nullable=True)
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    sha256: Mapped[str] = mapped_column(Text)
    bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class Signal(TenantMixin, Base):
    """Similarity signals (§12); case-level signals have no scan (K40)."""

    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("case_id", "scan_id", "name", postgresql_nulls_not_distinct=True),
        _tenant_fk("case_id", "cases"),
        _tenant_fk("scan_id", "scans"),
    )

    id: Mapped[uuid.UUID] = id_column()
    case_id: Mapped[uuid.UUID]
    scan_id: Mapped[uuid.UUID | None]
    name: Mapped[str] = mapped_column(Text)
    value: Mapped[Any] = mapped_column(JSONB)  # bool or number
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = created_at_column()


class Verdict(TenantMixin, Base):
    __tablename__ = "verdicts"
    __table_args__ = (
        _tenant_fk("case_id", "cases"),
        Index(None, "case_id"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    id: Mapped[uuid.UUID] = id_column()
    case_id: Mapped[uuid.UUID]
    source: Mapped[VerdictSource] = enum_column(VerdictSource, "source")
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    category: Mapped[Category] = enum_column(Category, "category")
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    copied_elements: Mapped[list[str]] = _text_array()
    collects_credentials_or_payment: Mapped[bool]
    evidence: Mapped[list[str]] = _text_array()
    suggested_basis: Mapped[NoticeBasis] = enum_column(NoticeBasis, "suggested_basis")
    short_explanation: Mapped[str] = mapped_column(Text)
    ai_call_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_calls.id"))
    created_at: Mapped[datetime] = created_at_column()


class Decision(TenantMixin, Base):
    __tablename__ = "decisions"
    __table_args__ = (_tenant_fk("case_id", "cases"), Index(None, "case_id"))

    id: Mapped[uuid.UUID] = id_column()
    case_id: Mapped[uuid.UUID]
    outcome: Mapped[DecisionOutcome] = enum_column(DecisionOutcome, "outcome")
    rule: Mapped[str] = mapped_column(Text)
    reasons: Mapped[list[str]] = _text_array()
    decided_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class ReviewTask(TenantMixin, Base):
    """Human review and operator tasks."""

    __tablename__ = "review_tasks"
    __table_args__ = (
        _tenant_fk("case_id", "cases"),
        _tenant_fk("notice_id", "notices"),
        Index(None, "status", "kind"),
        Index(
            "uq_review_tasks_open_review_per_case",
            "case_id",
            unique=True,
            postgresql_where=text("kind = 'review' AND status = 'open'"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    case_id: Mapped[uuid.UUID | None]
    notice_id: Mapped[uuid.UUID | None]
    kind: Mapped[TaskKind] = enum_column(TaskKind, "kind")
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TaskStatus] = enum_column(TaskStatus, "status", default=TaskStatus.OPEN)
    resolved_by: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None]
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class EvidenceLink(TenantMixin, Base):
    __tablename__ = "evidence_links"
    __table_args__ = (_tenant_key(), _tenant_fk("case_id", "cases"))

    id: Mapped[uuid.UUID] = id_column()
    case_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    views: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    created_at: Mapped[datetime] = created_at_column()


class Notice(TenantMixin, Base):
    """A takedown notice; gates that stop it are recorded per notice (K42)."""

    __tablename__ = "notices"
    __table_args__ = (
        UniqueConstraint("case_id", "channel", "template", "escalation_level", "round", "dry_run"),
        _tenant_key(),
        _tenant_fk("case_id", "cases"),
        _tenant_fk("evidence_link_id", "evidence_links"),
        Index(None, "status"),
    )

    id: Mapped[uuid.UUID] = id_column()
    case_id: Mapped[uuid.UUID]
    channel: Mapped[Channel] = enum_column(Channel, "channel")
    method: Mapped[NoticeMethod] = enum_column(NoticeMethod, "method")
    recipient: Mapped[str | None] = mapped_column(Text)
    template: Mapped[NoticeTemplate] = enum_column(NoticeTemplate, "template")
    language: Mapped[Language] = enum_column(Language, "language", default=Language.EN)
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    evidence_link_id: Mapped[uuid.UUID | None]
    status: Mapped[NoticeStatus] = enum_column(NoticeStatus, "status", default=NoticeStatus.DRAFT)
    blocked_reason: Mapped[BlockedReason | None] = enum_column(
        BlockedReason, "blocked_reason", nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    escalation_level: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    round: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    sent_at: Mapped[datetime | None]
    provider_message_id: Mapped[str | None] = mapped_column(Text, index=True)
    external_ref: Mapped[str | None] = mapped_column(Text)
    dry_run: Mapped[bool]
    # The explanation paragraph in English and in the notice's language (§13, K138).
    explanation_en: Mapped[str | None] = mapped_column(Text)
    explanation_local: Mapped[str | None] = mapped_column(Text)
    explanation_source: Mapped[ExplanationSource | None] = enum_column(
        ExplanationSource, "explanation_source", nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class Message(TenantMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        _tenant_fk("case_id", "cases"),
        _tenant_fk("notice_id", "notices"),
        Index(None, "case_id"),
        CheckConstraint(
            "classification_confidence IS NULL OR "
            "(classification_confidence >= 0 AND classification_confidence <= 1)",
            name="classification_confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    case_id: Mapped[uuid.UUID]
    notice_id: Mapped[uuid.UUID | None]
    direction: Mapped[MessageDirection] = enum_column(MessageDirection, "direction")
    from_addr: Mapped[str | None] = mapped_column(Text)
    to_addr: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    raw_key: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(default=utcnow)
    classification: Mapped[ReplyClass | None] = enum_column(
        ReplyClass, "classification", nullable=True
    )
    classification_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))


class Recheck(TenantMixin, Base):
    __tablename__ = "rechecks"
    __table_args__ = (_tenant_fk("case_id", "cases"), Index(None, "case_id", "checked_at"))

    id: Mapped[uuid.UUID] = id_column()
    case_id: Mapped[uuid.UUID]
    checked_at: Mapped[datetime] = mapped_column(default=utcnow)
    method: Mapped[RecheckMethod] = enum_column(RecheckMethod, "method")
    result: Mapped[RecheckResult] = enum_column(RecheckResult, "result")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class Report(TenantMixin, Base):
    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint("customer_id", "period"),
        CheckConstraint("period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'", name="period_format"),
    )

    id: Mapped[uuid.UUID] = id_column()
    period: Mapped[str] = mapped_column(Text)  # YYYY-MM
    pdf_key: Mapped[str | None] = mapped_column(Text)
    csv_key: Mapped[str | None] = mapped_column(Text)
    print_token_hash: Mapped[str | None] = mapped_column(Text, unique=True)
    print_token_expires_at: Mapped[datetime | None]
    generated_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_column()


# Tables with a required customer_id and row level security (customers is protected on id).
TENANT_TABLES: tuple[str, ...] = tuple(
    sorted(
        table.name
        for table in Base.metadata.tables.values()
        if "customer_id" in table.c and not table.c.customer_id.nullable
    )
)
GLOBAL_TABLES: tuple[str, ...] = tuple(
    sorted(
        name
        for name in Base.metadata.tables
        if name not in TENANT_TABLES and name != Customer.__tablename__
    )
)

lift_column_checks(Base.metadata)
