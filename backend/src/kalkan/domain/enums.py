"""Enumerations stored as ``text`` + ``CHECK`` in Postgres (architecture.md §5)."""

from __future__ import annotations

from enum import StrEnum


class CustomerStatus(StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class UserRole(StrEnum):
    OWNER = "owner"
    VIEWER = "viewer"


class AuthScope(StrEnum):
    CUSTOMER = "customer"
    STAFF = "staff"


class SightingSource(StrEnum):
    CT = "ct"
    DNSTWIST = "dnstwist"
    CZDS = "czds"
    MANUAL = "manual"


class MatchReason(StrEnum):
    """Why a domain matched a brand (architecture.md §8); stored in ``brand_matches.reasons``."""

    KEYWORD = "keyword"
    HOMOGLYPH = "homoglyph"
    PERMUTATION = "permutation"
    FUZZY = "fuzzy"
    MANUAL = "manual"


class OriginSource(StrEnum):
    """Where a domain's origin host behind a proxy came from (K37)."""

    CLOUDFLARE_DISCLOSURE = "cloudflare_disclosure"
    OPERATOR = "operator"


class ContactKind(StrEnum):
    HOST = "host"
    REGISTRAR = "registrar"
    REGISTRY = "registry"  # key: the TLD as a lower-case A-label (K136)


class WebhookSource(StrEnum):
    POSTMARK_INBOUND = "postmark_inbound"
    POSTMARK_BOUNCE = "postmark_bounce"


class AiPurpose(StrEnum):
    TRIAGE = "triage"
    CONFIRM = "confirm"
    NOTICE_TEXT = "notice_text"
    CLASSIFY_REPLY = "classify_reply"
    EVAL = "eval"


class BrandAssetKind(StrEnum):
    LOGO_LIGHT = "logo_light"
    LOGO_DARK = "logo_dark"
    FAVICON = "favicon"
    REFERENCE_DESKTOP = "reference_desktop"
    REFERENCE_MOBILE = "reference_mobile"
    CAMPAIGN = "campaign"


class OfficialDomainKind(StrEnum):
    PRIMARY = "primary"
    LEGACY = "legacy"
    BACKUP = "backup"
    MIRROR = "mirror"


class WhitelistKind(StrEnum):
    OFFICIAL = "official"
    MIRROR = "mirror"
    AFFILIATE = "affiliate"
    PARTNER = "partner"
    SOCIAL = "social"
    OTHER = "other"


class WhitelistSource(StrEnum):
    MANUAL = "manual"
    CSV = "csv"
    ONBOARDING = "onboarding"
    THIS_IS_OURS = "this_is_ours"


class CaseState(StrEnum):
    DETECTED = "detected"
    IN_REVIEW = "in_review"
    VERIFIED = "verified"
    APPROVED = "approved"
    NOTIFIED = "notified"
    TAKEN_DOWN = "taken_down"
    ARCHIVED = "archived"
    WATCHING = "watching"
    DISMISSED = "dismissed"


class Category(StrEnum):
    """Classification of a candidate site; the same values as the AI verdict (§13)."""

    PHISHING_CLONE = "phishing_clone"
    BRAND_IMPERSONATION = "brand_impersonation"
    AFFILIATE_OR_REVIEW = "affiliate_or_review"
    PARKED_OR_EMPTY = "parked_or_empty"
    UNRELATED = "unrelated"
    UNCLEAR = "unclear"


class BlockedReason(StrEnum):
    MONITOR_ONLY = "monitor_only"
    SENDING_DISABLED = "sending_disabled"
    SUSPENDED = "suspended"
    DRY_RUN = "dry_run"
    KILL_SWITCH = "kill_switch"
    DAILY_LIMIT = "daily_limit"
    NO_LOA = "no_loa"
    LEGAL_REVIEW = "legal_review"


class EventType(StrEnum):
    DETECTED = "detected"
    PRECHECKED = "prechecked"
    CAPTURED = "captured"
    VERIFIED = "verified"
    APPROVED = "approved"
    REVIEW_REQUESTED = "review_requested"
    NOTICE_SENT = "notice_sent"
    REGISTRAR_ESCALATED = "registrar_escalated"
    REPLY_RECEIVED = "reply_received"
    OFFLINE_DETECTED = "offline_detected"
    ONLINE_AGAIN = "online_again"
    WARNING_ACTIVE = "warning_active"
    TAKEN_DOWN = "taken_down"
    REOPENED = "reopened"
    WATCHING = "watching"
    DISMISSED = "dismissed"
    WHITELISTED = "whitelisted"
    ARCHIVED = "archived"
    JOB_FAILED = "job_failed"
    DECISION = "decision"
    NOTE = "note"


class ScanStage(StrEnum):
    HTTP = "http"
    BROWSER = "browser"


class Profile(StrEnum):
    DESKTOP = "desktop"
    MOBILE = "mobile"


class ArtifactKind(StrEnum):
    SCREENSHOT = "screenshot"
    SCREENSHOT_FULL = "screenshot_full"
    HTML = "html"
    HAR = "har"
    DNS = "dns"
    RDAP = "rdap"
    TLS = "tls"
    FAVICON = "favicon"


class VerdictSource(StrEnum):
    AI_TRIAGE = "ai_triage"
    AI_CONFIRM = "ai_confirm"
    STAFF = "staff"


class NoticeBasis(StrEnum):
    PHISHING = "phishing"
    TRADEMARK = "trademark"
    COPYRIGHT = "copyright"
    NONE = "none"


class DecisionOutcome(StrEnum):
    CONFIRM = "confirm"
    APPROVE = "approve"
    VERIFY = "verify"  # the operator's "Verify only" (§19)
    REVIEW = "review"
    WATCH = "watch"
    DISMISS = "dismiss"


class TaskKind(StrEnum):
    REVIEW = "review"
    NEEDS_INFO = "needs_info"
    ESCALATION = "escalation"
    MANUAL_CHANNEL = "manual_channel"
    RETRACTION = "retraction"
    BOUNCE = "bounce"
    REPLY = "reply"


class TaskStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Channel(StrEnum):
    CLOUDFLARE = "cloudflare"  # kept for old rows; never planned (K56)
    HOSTING = "hosting"
    REGISTRAR = "registrar"
    REGISTRY = "registry"
    GOOGLE = "google"


class NoticeMethod(StrEnum):
    API = "api"
    EMAIL = "email"
    MANUAL_FORM = "manual_form"


class NoticeTemplate(StrEnum):
    PHISHING = "phishing"
    TRADEMARK = "trademark"
    DMCA = "dmca"
    DSA16 = "dsa16"
    RETRACTION = "retraction"


class NoticeStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    BLOCKED = "blocked"
    SENT = "sent"
    MANUAL_PENDING = "manual_pending"
    FAILED = "failed"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    RETRACTED = "retracted"


class Language(StrEnum):
    EN = "en"
    TR = "tr"
    PL = "pl"
    RU = "ru"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class ReplyClass(StrEnum):
    REMOVED = "removed"
    NEEDS_INFO = "needs_info"
    NOT_OUR_CUSTOMER = "not_our_customer"
    AUTO_REPLY = "auto_reply"
    OTHER = "other"


class RecheckMethod(StrEnum):
    HTTP = "http"
    BROWSER = "browser"


class RecheckResult(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    WARNING = "warning"
    UNKNOWN = "unknown"


class ExplanationSource(StrEnum):
    """Where a notice's explanation paragraph came from (§13)."""

    AI = "ai"
    FALLBACK = "fallback"  # the fixed paragraph: AI unavailable or its text failed validation
