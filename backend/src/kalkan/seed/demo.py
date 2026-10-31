"""Demo workspace: the fictional Mintera Bank customer behind the design (product.md §9).

The seed is deterministic (fixed random seed) and idempotent: an existing demo customer is
removed with all its rows and demo images, then rebuilt. Dates are moved so that the
design's "today" is the day the seed runs. Users get new passwords and TOTP secrets on every
run; locally they are written to a git-ignored credentials file. In prod (refreshing the demo
account on a server) nothing is written to disk: the caller shows them once
(``kalkan seed --print-credentials``), and the seed writes only to the ``is_demo`` customer.

Demo cases are frozen: the pipeline must skip ``is_demo`` customers (K46).
"""

from __future__ import annotations

import ipaddress
import json
import os
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from kalkan.api.v1.deps import customer_tz
from kalkan.config import REPO_ROOT, ConfigError, Settings
from kalkan.db.base import Base, new_id
from kalkan.db.case_numbers import next_case_number
from kalkan.db.models import (
    Artifact,
    Brand,
    BrandAsset,
    BrandMatch,
    Case,
    CaseEvent,
    Customer,
    Decision,
    Domain,
    DomainSighting,
    Notice,
    OfficialDomain,
    Report,
    Scan,
    Signal,
    User,
    Verdict,
    WhitelistEntry,
)
from kalkan.db.session import Database
from kalkan.domain.enums import (
    ArtifactKind,
    BrandAssetKind,
    CaseState,
    Category,
    Channel,
    CustomerStatus,
    DecisionOutcome,
    EventType,
    Language,
    NoticeBasis,
    NoticeMethod,
    NoticeStatus,
    NoticeTemplate,
    OfficialDomainKind,
    OriginSource,
    Profile,
    ScanStage,
    SightingSource,
    UserRole,
    VerdictSource,
    WhitelistKind,
    WhitelistSource,
)
from kalkan.reports import build as report_build
from kalkan.reports import content as report_content
from kalkan.reports import persist as report_persist
from kalkan.security import (
    encrypt_totp_secret,
    hash_password,
    hash_recovery_code,
    hash_token,
    new_password,
    new_recovery_codes,
    new_token,
    new_totp_secret,
)
from kalkan.seed import design
from kalkan.seed.placeholders import html_page, json_document, png
from kalkan.storage import paths
from kalkan.storage.gcs import ObjectStore, StoredObject

DEMO_SLUG = "mintera-demo"
DEMO_NAME = "Mintera Bank"
RANDOM_SEED = 20261008
DEFAULT_CREDENTIALS_FILE = REPO_ROOT / ".local" / "demo-credentials.json"
SEED_TAG = {"seed": "demo"}
ACTOR = "seed"

NOTICE_DELAY = timedelta(minutes=25)
CAPTURE_DELAY = timedelta(minutes=2)
VERIFY_DELAY = timedelta(minutes=7)
CONFIRMATION_WINDOW = timedelta(hours=72)
WATCH_INTERVAL = timedelta(days=7)
DISMISSED_INTERVAL = timedelta(days=30)
RECHECK_INTERVAL = timedelta(hours=6)
MONITOR_PERIOD = timedelta(days=30)

PREFIXES = (
    "mintera",
    "mint-era",
    "minteraa",
    "mintra",
    "mintera24",
    "mintera-bank",
    "minterapl",
    "rnintera",
    "m1ntera",
    "mintera-pl",
    "minteera",
    "mintera-online",
)
WORDS = (
    "login",
    "secure",
    "verify",
    "account",
    "app",
    "online",
    "support",
    "update",
    "pay",
    "card",
    "mobile",
    "auth",
    "signin",
    "wallet",
    "transfer",
    "blik",
    "zaloguj",
    "bankowosc",
    "konto",
    "kredyt",
    "refund",
    "unlock",
    "alert",
    "id",
    "portal",
    "help",
    "official",
    "client",
)
TLDS = (
    "com",
    "net",
    "xyz",
    "top",
    "online",
    "site",
    "live",
    "info",
    "app",
    "support",
    "click",
    "club",
    "shop",
    "icu",
    "pro",
    "pl",
)
HOSTS = ("Hetzner", "OVHcloud", "DigitalOcean", "Hostinger", "Contabo", "Namecheap")
REGISTRARS = (
    "Namecheap, Inc.",
    "NameSilo, LLC",
    "Porkbun LLC",
    "Dynadot Inc",
    "GoDaddy.com, LLC",
    "Hostinger Operations, UAB",
)
CLOUDFLARE_RECIPIENT = "Cloudflare abuse report"


class SeedError(RuntimeError):
    pass


@dataclass(frozen=True)
class DemoResult:
    customer_id: uuid.UUID
    cases: int
    credentials_file: Path | None
    credentials: dict[str, Any] = field(repr=False)


# --------------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Clock:
    """Maps the design's day indices (0 = 29 days ago, 29 = today) to real times."""

    tz: ZoneInfo
    today: date
    # Elapsed time that moves today's events back when the seed runs before 10:30. Runs from
    # 04:30 (05:30 on the spring DST day) keep every event on its design day; earlier runs move
    # today's first events to yesterday, and the totals stay the same (K49).
    shift: timedelta
    reference: datetime  # every generated past event happens at or before this moment

    @classmethod
    def starting_at(cls, now: datetime) -> Clock:
        tz = ZoneInfo(design.TIMEZONE)
        today = now.astimezone(tz).date()
        # Absolute instants, not wall-clock differences, so DST days behave.
        reference = datetime.combine(today, design.REFERENCE_TIME, tzinfo=tz).astimezone(UTC)
        shift = max(timedelta(0), reference - now.astimezone(UTC))
        return cls(tz, today, shift, reference - shift)

    def at(self, day: int, clock_time: time) -> datetime:
        local = datetime.combine(
            self.today - timedelta(days=design.PERIOD_DAYS - 1 - day), clock_time, tzinfo=self.tz
        )
        moment = local.astimezone(UTC)
        if day == design.PERIOD_DAYS - 1:
            moment -= self.shift
        return moment

    def day_of(self, moment: datetime) -> int:
        first_day = self.today - timedelta(days=design.PERIOD_DAYS - 1)
        return (moment.astimezone(self.tz).date() - first_day).days


# --------------------------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------------------------


@dataclass
class PlannedNotice:
    channel: Channel
    sent_at: datetime
    recipient: str
    status: NoticeStatus
    template: NoticeTemplate
    escalation_level: int = 0


@dataclass
class PlannedCase:
    domain: str
    category: Category
    state: CaseState
    first_seen: datetime
    hosting: str | None
    is_cloudflare: bool
    source: SightingSource = SightingSource.CT
    notices: list[PlannedNotice] = field(default_factory=list)
    verified_at: datetime | None = None
    offline_since: datetime | None = None
    taken_down_at: datetime | None = None
    archived_at: datetime | None = None
    last_action: str | None = None
    last_action_at: datetime | None = None
    next_check_at: datetime | None = None
    detail: bool = False


class Planner:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.rng = random.Random(RANDOM_SEED)
        self.used_names: set[str] = {row.domain for row in design.RECENT_ROWS}
        self.used_names.update(pattern for pattern, *_ in design.WHITELIST_RECENT)
        self.used_names.update(pattern for pattern, _ in design.WHITELIST_OLDER)

    # names and infrastructure

    def domain_name(self) -> str:
        while True:
            prefix = self.rng.choice(PREFIXES)
            word = self.rng.choice(WORDS)
            joiner = self.rng.choice(("-", ""))
            name = f"{prefix}{joiner}{word}.{self.rng.choice(TLDS)}"
            if name not in self.used_names:
                self.used_names.add(name)
                return name

    def hosting(self) -> str:
        return self.rng.choice(HOSTS)

    def source(self) -> SightingSource:
        roll = self.rng.random()
        if roll < 0.85:
            return SightingSource.CT
        return SightingSource.DNSTWIST if roll < 0.95 else SightingSource.CZDS

    def random_time(self, day: int, latest: time = time(22, 0)) -> time:
        if day == design.PERIOD_DAYS - 1:
            latest = min(latest, time(8, 0))
        start = 6 * 60
        end = latest.hour * 60 + latest.minute
        minute = self.rng.randrange(start, end)
        return time(minute // 60, minute % 60)

    # notices

    @staticmethod
    def first_wave(case: PlannedCase, at: datetime) -> PlannedNotice:
        template = (
            NoticeTemplate.PHISHING
            if case.category is Category.PHISHING_CLONE
            else NoticeTemplate.TRADEMARK
        )
        if case.is_cloudflare:
            return PlannedNotice(
                Channel.CLOUDFLARE, at, CLOUDFLARE_RECIPIENT, NoticeStatus.ACKNOWLEDGED, template
            )
        host = (case.hosting or "host").lower().replace(" ", "")
        return PlannedNotice(
            Channel.HOSTING, at, f"abuse@{host}.example", NoticeStatus.SENT, template
        )

    @staticmethod
    def registrar_notice(at: datetime) -> PlannedNotice:
        return PlannedNotice(
            Channel.REGISTRAR,
            at,
            "abuse@registrar.example",
            NoticeStatus.SENT,
            NoticeTemplate.PHISHING,
            escalation_level=1,
        )

    def take_down(self, case: PlannedCase, closing: PlannedNotice, offline: datetime) -> None:
        closing.status = NoticeStatus.RESOLVED
        case.offline_since = offline
        confirmed = min(offline + CONFIRMATION_WINDOW, self.clock.reference - timedelta(minutes=5))
        case.taken_down_at = max(confirmed, offline)
        case.state = CaseState.TAKEN_DOWN
        case.last_action = "Confirmed offline"
        case.last_action_at = case.taken_down_at
        archive_at = case.taken_down_at + MONITOR_PERIOD
        if archive_at <= self.clock.reference:
            case.state = CaseState.ARCHIVED
            case.archived_at = archive_at
        else:
            case.next_check_at = self.clock.reference + timedelta(hours=self.rng.randint(1, 23))

    def taken_down_case(
        self,
        *,
        channel: Channel,
        hours: float,
        offline: datetime,
        row: design.RecentRow | None = None,
    ) -> PlannedCase:
        """A case closed by ``channel``, ``hours`` after that channel's notice.

        ``row`` fixes the domain, category and hosting of a design row.
        """
        if channel is Channel.REGISTRAR:
            registrar_at = offline - timedelta(hours=hours)
            first_notice = registrar_at - timedelta(hours=design.ESCALATION_HOURS)
            is_cloudflare = self.rng.random() < 0.5
            category = Category.PHISHING_CLONE
        else:
            first_notice = offline - timedelta(hours=hours)
            is_cloudflare = channel is Channel.CLOUDFLARE
            category = (
                Category.PHISHING_CLONE
                if self.rng.random() < 0.85
                else Category.BRAND_IMPERSONATION
            )
            # A phishing site still online 48 h after the first wave goes to the registrar
            # (§15); one that closed later without it was a trademark case.
            if hours > design.ESCALATION_HOURS:
                category = Category.BRAND_IMPERSONATION
        hosting: str | None = "Cloudflare" if is_cloudflare else self.hosting()
        if row is not None:
            category, hosting, is_cloudflare = row.category, row.hosting, row.is_cloudflare
        case = PlannedCase(
            domain=row.domain if row is not None else self.domain_name(),
            category=category,
            state=CaseState.NOTIFIED,
            first_seen=first_notice - NOTICE_DELAY,
            hosting=hosting,
            is_cloudflare=is_cloudflare,
            source=SightingSource.CT if row is not None else self.source(),
        )
        case.verified_at = case.first_seen + VERIFY_DELAY
        wave = self.first_wave(case, first_notice)
        case.notices.append(wave)
        closing = wave
        if channel is Channel.REGISTRAR:
            closing = self.registrar_notice(first_notice + timedelta(hours=design.ESCALATION_HOURS))
            case.notices.append(closing)
        self.take_down(case, closing, offline)
        return case

    def live_case(self, day: int, clock_time: time, *, notified: bool) -> PlannedCase:
        first_seen = self.clock.at(day, clock_time)
        category = (
            Category.PHISHING_CLONE if self.rng.random() < 0.8 else Category.BRAND_IMPERSONATION
        )
        is_cloudflare = self.rng.random() < 0.5
        case = PlannedCase(
            domain=self.domain_name(),
            category=category,
            state=CaseState.APPROVED,
            first_seen=first_seen,
            hosting="Cloudflare" if is_cloudflare else self.hosting(),
            is_cloudflare=is_cloudflare,
            source=self.source(),
            verified_at=first_seen + VERIFY_DELAY,
            last_action="Notice being prepared",
            last_action_at=first_seen + VERIFY_DELAY,
        )
        if notified:
            self.notify(case, first_seen + NOTICE_DELAY)
        return case

    def notify(self, case: PlannedCase, first_notice: datetime) -> None:
        wave = self.first_wave(case, first_notice)
        case.notices.append(wave)
        case.state = CaseState.NOTIFIED
        case.last_action = (
            "Reported to Cloudflare" if wave.channel is Channel.CLOUDFLARE else "Host notified"
        )
        case.last_action_at = first_notice
        escalation = first_notice + timedelta(hours=design.ESCALATION_HOURS)
        if case.category is Category.PHISHING_CLONE and escalation <= self.clock.reference:
            case.notices.append(self.registrar_notice(escalation))
            case.last_action = "Sent to registrar"
            case.last_action_at = escalation
        case.next_check_at = self.clock.reference + timedelta(minutes=self.rng.randint(30, 359))

    def watching_case(self, day: int, clock_time: time, domain: str | None = None) -> PlannedCase:
        first_seen = self.clock.at(day, clock_time)
        last_scan = first_seen
        while last_scan + WATCH_INTERVAL <= self.clock.reference:
            last_scan += WATCH_INTERVAL
        return PlannedCase(
            domain=domain or self.domain_name(),
            category=Category.PARKED_OR_EMPTY,
            state=CaseState.WATCHING,
            first_seen=first_seen,
            hosting=None,
            is_cloudflare=False,
            source=self.source(),
            last_action="Parked page found",
            last_action_at=last_scan + timedelta(minutes=1),
            next_check_at=last_scan + WATCH_INTERVAL,
        )

    def dismissed_case(self, day: int, clock_time: time) -> PlannedCase:
        first_seen = self.clock.at(day, clock_time)
        affiliate = self.rng.random() < 0.65
        dismissed_at = first_seen + timedelta(minutes=12)
        return PlannedCase(
            domain=self.domain_name(),
            category=Category.AFFILIATE_OR_REVIEW if affiliate else Category.UNRELATED,
            state=CaseState.DISMISSED,
            first_seen=first_seen,
            hosting=self.hosting(),
            is_cloudflare=False,
            source=self.source(),
            last_action="Dismissed as affiliate site" if affiliate else "Dismissed as unrelated",
            last_action_at=dismissed_at,
            next_check_at=dismissed_at + DISMISSED_INTERVAL,
        )


def _recent_rows(planner: Planner) -> list[PlannedCase]:
    """The six rows of the design's "Recent threats" table, with the detail case first."""
    clock = planner.clock
    rows = {row.domain: row for row in design.RECENT_ROWS}
    cases: list[PlannedCase] = []

    # mintera-login.com: the threat-detail case; its records are written by _write_detail.
    login = rows[design.DETAIL_DOMAIN]
    seen = clock.at(login.day, login.at)
    detail = PlannedCase(
        domain=login.domain,
        category=login.category,
        state=CaseState.NOTIFIED,
        first_seen=seen,
        hosting=login.hosting,
        is_cloudflare=True,
        verified_at=seen + timedelta(minutes=7),
        detail=True,
        notices=[
            PlannedNotice(
                Channel.CLOUDFLARE,
                seen + timedelta(minutes=26),
                CLOUDFLARE_RECIPIENT,
                NoticeStatus.ACKNOWLEDGED,
                NoticeTemplate.PHISHING,
            ),
            PlannedNotice(
                Channel.HOSTING,
                seen + timedelta(minutes=51),
                "abuse@hetzner.example",
                NoticeStatus.SENT,
                NoticeTemplate.PHISHING,
            ),
        ],
        last_action=login.last_action,
        last_action_at=seen + timedelta(minutes=51),
        next_check_at=seen + timedelta(minutes=51) + RECHECK_INTERVAL,
    )
    cases.append(detail)

    rewards = rows["mintera-rewards.top"]
    seen = clock.at(rewards.day, rewards.at)
    cases.append(
        PlannedCase(
            domain=rewards.domain,
            category=rewards.category,
            state=CaseState.APPROVED,
            first_seen=seen,
            hosting=rewards.hosting,
            is_cloudflare=False,
            verified_at=seen + VERIFY_DELAY,
            last_action=rewards.last_action,
            last_action_at=seen + VERIFY_DELAY,
        )
    )

    secure = rows["mintera-secure.xyz"]
    seen = clock.at(secure.day, secure.at)
    secure_case = PlannedCase(
        domain=secure.domain,
        category=secure.category,
        state=CaseState.APPROVED,
        first_seen=seen,
        hosting=secure.hosting,
        is_cloudflare=True,
        verified_at=seen + VERIFY_DELAY,
    )
    planner.notify(secure_case, seen + NOTICE_DELAY)
    cases.append(secure_case)

    for domain, (channel, hours) in design.FIXED_TAKEDOWNS.items():
        row = rows[domain]
        seen = clock.at(row.day, row.at)
        first_notice = seen + NOTICE_DELAY
        if channel is Channel.REGISTRAR:
            offline = first_notice + timedelta(hours=design.ESCALATION_HOURS + hours)
        else:
            offline = first_notice + timedelta(hours=hours)
        case = planner.taken_down_case(channel=channel, hours=hours, offline=offline, row=row)
        case.last_action = row.last_action
        # "Registrar suspended · <day>" shows when the site went down, not the confirmation.
        if channel is Channel.REGISTRAR:
            case.last_action_at = offline
        cases.append(case)

    app = rows["mint-era-app.com"]
    cases.append(planner.watching_case(app.day, app.at, domain=app.domain))
    return cases


def plan_current_period(planner: Planner) -> list[PlannedCase]:
    clock = planner.clock
    days = design.PERIOD_DAYS
    cases = _recent_rows(planner)

    # Taken down: the shortest takedowns close earliest, so every case starts in the period.
    slots = [day for day, count in enumerate(design.TAKEN_DOWN_PER_DAY) for _ in range(count)]
    for case in cases:
        if case.offline_since is not None:
            slots.remove(clock.day_of(case.offline_since))

    durations: list[tuple[float, Channel, float]] = []  # (overall hours, channel, channel hours)
    fixed_hours = dict(design.FIXED_TAKEDOWNS.values())
    for channel, values in (
        (Channel.CLOUDFLARE, design.CLOUDFLARE_HOURS),
        (Channel.HOSTING, design.HOSTING_HOURS),
        (Channel.REGISTRAR, design.REGISTRAR_HOURS),
    ):
        remaining = list(values)
        if channel in fixed_hours:
            remaining.remove(fixed_hours[channel])
        wait = design.ESCALATION_HOURS if channel is Channel.REGISTRAR else 0
        durations.extend((hours + wait, channel, hours) for hours in remaining)
    # Closing time of each slot; today's sites closed before the design's 10:05 events.
    today = days - 1
    early_times = iter((time(6, 10), time(8, 40), time(7, 25), time(9, 5)))
    offline_at: list[datetime] = []
    for day in sorted(slots):
        if day == today:
            clock_time = next(early_times)
        else:
            clock_time = time(planner.rng.randint(7, 21), planner.rng.choice((5, 20, 35, 50)))
        offline_at.append(clock.at(day, clock_time))

    capacity = list(design.DETECTED_PER_DAY)
    for case in cases:
        capacity[clock.day_of(case.first_seen)] -= 1

    # Longest takedowns first, each on the latest closing slot whose start day still has a
    # free detection; this keeps every case inside the period and the per-day counts exact.
    for overall, channel, hours in sorted(durations, reverse=True):
        for offline in sorted(offline_at, reverse=True):
            start_day = clock.day_of(offline - timedelta(hours=overall) - NOTICE_DELAY)
            if start_day >= 0 and capacity[start_day] > 0:
                break
        else:
            raise SeedError(f"no slot fits a {overall} h takedown within the period")
        offline_at.remove(offline)
        capacity[start_day] -= 1
        case = planner.taken_down_case(channel=channel, hours=hours, offline=offline)
        if clock.day_of(case.first_seen) != start_day:
            raise SeedError("takedown start day changed while planning")
        cases.append(case)

    def take_day(latest_first: bool) -> int:
        order = range(days - 1, -1, -1) if latest_first else range(days)
        for day in order:
            if capacity[day] > 0:
                capacity[day] -= 1
                return day
        raise SeedError("no detections left to assign")

    notice_sent = sum(1 for case in cases if case.state is CaseState.NOTIFIED)
    for _ in range(design.NOTICE_SENT - notice_sent):
        day = take_day(latest_first=True)
        cases.append(planner.live_case(day, planner.random_time(day, time(20, 0)), notified=True))
    active = sum(1 for case in cases if case.state is CaseState.APPROVED)
    for _ in range(design.ACTIVE - active):
        day = take_day(latest_first=True)
        cases.append(planner.live_case(day, planner.random_time(day, time(20, 0)), notified=False))

    open_days = [day for day in range(days) for _ in range(capacity[day])]
    planner.rng.shuffle(open_days)
    for index, day in enumerate(open_days):
        clock_time = planner.random_time(day)
        if index < design.DISMISSED:
            cases.append(planner.dismissed_case(day, clock_time))
        else:
            cases.append(planner.watching_case(day, clock_time))
    return cases


def plan_previous_period(planner: Planner) -> list[PlannedCase]:
    """The 30 days before the period: everything verified has been taken down since."""
    cases: list[PlannedCase] = []
    registrar_left = len(design.REGISTRAR_HOURS)
    hours_list = sorted(design.PREVIOUS_HOURS, reverse=True)
    for index, overall in enumerate(hours_list):
        day = planner.rng.randint(-design.PERIOD_DAYS, -1)
        first_notice = planner.clock.at(day, planner.random_time(day)) + NOTICE_DELAY
        offline = first_notice + timedelta(hours=overall)
        if registrar_left and overall >= design.ESCALATION_HOURS + 8:
            registrar_left -= 1
            channel, hours = Channel.REGISTRAR, overall - design.ESCALATION_HOURS
        else:
            channel = Channel.CLOUDFLARE if index % 2 else Channel.HOSTING
            hours = overall
        cases.append(planner.taken_down_case(channel=channel, hours=hours, offline=offline))

    others = design.PREVIOUS_DETECTED - design.PREVIOUS_TAKEN_DOWN
    for index in range(others):
        day = planner.rng.randint(-design.PERIOD_DAYS, -1)
        clock_time = planner.random_time(day)
        if index < design.PREVIOUS_DISMISSED:
            cases.append(planner.dismissed_case(day, clock_time))
        else:
            cases.append(planner.watching_case(day, clock_time))
    return cases


# --------------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------------


def purge_demo_customer(session: Session, store: ObjectStore, settings: Settings) -> None:
    """Delete the demo customer, every row that belongs to it and its demo images."""
    customer = session.scalar(select(Customer).where(Customer.slug == DEMO_SLUG))
    if customer is None:
        return
    if not customer.is_demo:
        raise SeedError(f"customer {DEMO_SLUG!r} exists and is not a demo customer")
    customer_id = customer.id
    case_ids = select(Case.id).where(Case.customer_id == customer_id)
    for table in reversed(Base.metadata.sorted_tables):
        if table.name == Customer.__tablename__:
            continue
        columns = table.c
        if "customer_id" in columns:
            session.execute(delete(table).where(columns.customer_id == customer_id))
        elif table.name in ("labels", "ai_calls"):
            session.execute(delete(table).where(columns.case_id.in_(case_ids)))
    session.execute(delete(Customer).where(Customer.id == customer_id))
    if settings.gcs_reports_bucket:
        store.delete_demo_objects(settings.gcs_reports_bucket, paths.demo(customer_id, ""))


class Writer:
    def __init__(
        self, session: Session, store: ObjectStore, settings: Settings, clock: Clock
    ) -> None:
        self.session = session
        self.store = store
        self.settings = settings
        self.clock = clock
        if not settings.gcs_reports_bucket:
            raise ConfigError("GCS_REPORTS_BUCKET is required for demo images")
        self.bucket = settings.gcs_reports_bucket

    def upload(self, customer: Customer, path: str, data: bytes, content_type: str) -> StoredObject:
        return self.store.put(self.bucket, paths.demo(customer.id, path), data, content_type)

    def domain(self, planned: PlannedCase, rng: random.Random) -> uuid.UUID:
        registered_days = rng.randint(1, 20)
        values: dict[str, Any] = {
            "name": planned.domain,
            "unicode_name": planned.domain,
            "registered_domain": planned.domain,
            "tld": planned.domain.rsplit(".", 1)[-1],
            "first_seen_at": planned.first_seen,
            "registrar": rng.choice(REGISTRARS),
            "registered_at": planned.first_seen - timedelta(days=registered_days),
            "nameservers": (
                ["aria.ns.cloudflare.com", "jake.ns.cloudflare.com"]
                if planned.is_cloudflare
                else [f"ns{n}.{(planned.hosting or 'parking').lower()}.example" for n in (1, 2)]
            ),
            "is_cloudflare": planned.is_cloudflare,
            "hosting_org": planned.hosting,
            "hosting_country": "US"
            if planned.is_cloudflare
            else rng.choice(("DE", "FR", "NL", "PL")),
            "enriched_at": planned.first_seen + timedelta(minutes=1),
        }
        statement = (
            pg_insert(Domain)
            .values(id=new_id(), **values)
            .on_conflict_do_update(index_elements=[Domain.name], set_=values)
            .returning(Domain.id)
        )
        domain_id: uuid.UUID = self.session.execute(statement).scalar_one()
        return domain_id

    def case(
        self, customer: Customer, brand: Brand, planned: PlannedCase, rng: random.Random
    ) -> Case:
        domain_id = self.domain(planned, rng)
        self.session.add(
            DomainSighting(
                domain_id=domain_id,
                source=planned.source,
                seen_at=planned.first_seen,
                detail={**SEED_TAG, "log": "Google 'Argon2026h2'"}
                if planned.source is SightingSource.CT
                else dict(SEED_TAG),
            )
        )
        self.session.add(
            BrandMatch(
                customer_id=customer.id,
                domain_id=domain_id,
                brand_id=brand.id,
                score=rng.randint(72, 98),
                reasons=["keyword"],
                first_matched_at=planned.first_seen,
                admitted=True,
            )
        )
        verified = planned.verified_at is not None
        case = Case(
            customer_id=customer.id,
            number=next_case_number(self.session, self.settings.case_prefix),
            brand_id=brand.id,
            domain_id=domain_id,
            state=planned.state,
            category=planned.category,
            confidence=Decimal(str(round(rng.uniform(0.9, 0.99), 3))) if verified else None,
            auto_approved=planned.detail,
            first_seen_at=planned.first_seen,
            verified_at=planned.verified_at,
            approved_at=planned.verified_at,
            approved_by="rule"
            if planned.detail
            else ("staff@kalkan.example" if verified else None),
            first_notice_at=planned.notices[0].sent_at if planned.notices else None,
            offline_since=planned.offline_since,
            taken_down_at=planned.taken_down_at,
            monitor_until=(
                planned.taken_down_at + MONITOR_PERIOD if planned.taken_down_at else None
            ),
            next_check_at=planned.next_check_at,
            last_action=planned.last_action,
            last_action_at=planned.last_action_at,
            created_at=planned.first_seen,
        )
        self.session.add(case)
        self.session.flush()
        self.events(case, planned)
        for notice in planned.notices:
            self.session.add(
                Notice(
                    customer_id=customer.id,
                    case_id=case.id,
                    channel=notice.channel,
                    method=NoticeMethod.API
                    if notice.channel is Channel.CLOUDFLARE
                    else NoticeMethod.EMAIL,
                    recipient=notice.recipient,
                    template=notice.template,
                    language=Language.EN,
                    subject=f"[{case.number}] Phishing report: {planned.domain}",
                    status=notice.status,
                    escalation_level=notice.escalation_level,
                    sent_at=notice.sent_at,
                    dry_run=False,
                    created_at=notice.sent_at,
                )
            )
        return case

    def events(self, case: Case, planned: PlannedCase) -> None:
        def add(event: EventType, at: datetime, **data: Any) -> None:
            self.session.add(
                CaseEvent(
                    customer_id=case.customer_id,
                    case_id=case.id,
                    at=at,
                    type=event,
                    actor="system" if event is not EventType.APPROVED else "rule",
                    data=data,
                )
            )

        seen = planned.first_seen
        add(EventType.DETECTED, seen, source=planned.source.value)
        if planned.state is CaseState.WATCHING:
            add(EventType.PRECHECKED, seen + timedelta(minutes=1), outcome="parked")
            add(EventType.WATCHING, seen + timedelta(minutes=1), reason="parked")
            return
        add(EventType.CAPTURED, seen + CAPTURE_DELAY)
        if planned.state is CaseState.DISMISSED:
            add(EventType.DISMISSED, seen + timedelta(minutes=12), reason=planned.category.value)
            return
        if planned.verified_at is not None:
            add(EventType.VERIFIED, planned.verified_at)
            add(EventType.APPROVED, planned.verified_at)
        for notice in planned.notices:
            if notice.channel is Channel.REGISTRAR:
                add(EventType.REGISTRAR_ESCALATED, notice.sent_at)
            add(EventType.NOTICE_SENT, notice.sent_at, channel=notice.channel.value)
        if planned.offline_since is not None:
            add(EventType.OFFLINE_DETECTED, planned.offline_since)
        if planned.taken_down_at is not None:
            add(EventType.TAKEN_DOWN, planned.taken_down_at)
        if planned.archived_at is not None:
            add(EventType.ARCHIVED, planned.archived_at)


def _write_detail(writer: Writer, customer: Customer, case: Case) -> None:
    """Infrastructure, evidence, signals and AI review of the threat-detail case."""
    session = writer.session
    seen = case.first_seen_at
    domain = session.get_one(Domain, case.domain_id)
    domain.registrar = "Namecheap, Inc."
    domain.registrar_abuse_email = "abuse@namecheap.example"
    domain.registered_at = seen - timedelta(days=2)
    domain.ips = [ipaddress.ip_address("104.21.48.12"), ipaddress.ip_address("172.67.140.20")]
    domain.hosting_org = "Cloudflare"
    domain.hosting_country = "US"
    domain.origin_ip = ipaddress.ip_address("198.51.100.24")
    domain.origin_org = "Hetzner Online GmbH"
    domain.origin_country = "DE"
    domain.origin_abuse_email = "abuse@hetzner.example"
    domain.origin_source = OriginSource.CLOUDFLARE_DISCLOSURE
    sighting = session.scalar(
        select(DomainSighting).where(DomainSighting.domain_id == domain.id).limit(1)
    )
    if sighting is not None:
        sighting.detail = {
            **SEED_TAG,
            "log": "Google 'Argon2026h2'",
            "issuer": "Let's Encrypt",
            "issued_at": (seen - timedelta(minutes=1)).isoformat(),
        }

    captured = seen + CAPTURE_DELAY
    http_scan = Scan(
        customer_id=customer.id,
        case_id=case.id,
        stage=ScanStage.HTTP,
        started_at=seen + timedelta(minutes=1),
        finished_at=seen + timedelta(minutes=1),
        outcome="suspicious",
        http_status=200,
        final_url=f"https://{domain.name}/",
        title="Mintera Bank - Sign in",
        text_excerpt="Verify your card to keep your account active.",
    )
    scans = {
        profile: Scan(
            customer_id=customer.id,
            case_id=case.id,
            stage=ScanStage.BROWSER,
            profile=profile,
            proxy_country="PL",
            started_at=captured,
            finished_at=captured + timedelta(seconds=40),
            outcome="ok",
            http_status=200,
            final_url=f"https://{domain.name}/",
            title="Mintera Bank - Sign in",
            forms=[{"fields": ["email", "password", "card_number"], "button": "Sign in"}],
        )
        for profile in (Profile.DESKTOP, Profile.MOBILE)
    }
    session.add_all([http_scan, *scans.values()])
    session.flush()

    clone_colour = (11, 42, 74)
    files = (
        (
            ArtifactKind.SCREENSHOT,
            Profile.DESKTOP,
            "desktop.png",
            "image/png",
            png(1280, 800, clone_colour, size=1_200_000),
        ),
        (
            ArtifactKind.SCREENSHOT,
            Profile.MOBILE,
            "mobile.png",
            "image/png",
            png(390, 844, clone_colour, size=640_000),
        ),
        (
            ArtifactKind.HTML,
            Profile.DESKTOP,
            "page.html",
            "text/html",
            html_page(
                "Mintera Bank - Sign in",
                "<p>Placeholder page for the demo workspace.</p>",
                size=84_000,
            ),
        ),
        (
            ArtifactKind.HAR,
            Profile.DESKTOP,
            "session.har",
            "application/json",
            json_document({"log": {"version": "1.2", "entries": []}}, size=2_300_000),
        ),
        (
            ArtifactKind.RDAP,
            None,
            "rdap.json",
            "application/rdap+json",
            json_document({"objectClassName": "domain", "ldhName": domain.name}, size=3_000),
        ),
    )
    for kind, profile, name, content_type, data in files:
        stored = writer.upload(customer, f"cases/{case.id}/{name}", data, content_type)
        session.add(
            Artifact(
                customer_id=customer.id,
                case_id=case.id,
                scan_id=scans[profile].id if profile else None,
                kind=kind,
                profile=profile,
                storage_key=stored.key,
                sha256=stored.sha256,
                bytes=stored.size,
                content_type=content_type,
                created_at=captured if profile else seen,
            )
        )

    desktop = scans[Profile.DESKTOP]
    for scan_id, name, value in (
        (desktop.id, "visual_similarity", 0.94),
        (desktop.id, "phash_distance_desktop", 4),
        (scans[Profile.MOBILE].id, "phash_distance_mobile", 5),
        (desktop.id, "favicon_match", True),
        (desktop.id, "logo_match", True),
        (desktop.id, "text_similarity", 88),
        (desktop.id, "brand_in_title", True),
        (desktop.id, "form_password", True),
        (desktop.id, "form_card", True),
        (desktop.id, "deposit_cta", True),
        (desktop.id, "external_form_action", False),
        (desktop.id, "redirects_to_official", False),
        (None, "brand_in_domain", 92),
        (None, "domain_age_days", 2),
        (None, "webrisk_listed", False),
    ):
        session.add(
            Signal(
                customer_id=customer.id, case_id=case.id, scan_id=scan_id, name=name, value=value
            )
        )

    explanation = (
        "The page copies the Mintera Bank logo, layout and login form, and adds a field that asks "
        "for a card number. The domain was registered two days before it was detected."
    )
    for source, model, confidence, minutes in (
        (VerdictSource.AI_TRIAGE, "claude-haiku-5-5", "0.95", 4),
        (VerdictSource.AI_CONFIRM, "claude-sonnet-5-5", "0.97", 7),
    ):
        session.add(
            Verdict(
                customer_id=customer.id,
                case_id=case.id,
                source=source,
                model=model,
                prompt_version="demo",
                category=Category.PHISHING_CLONE,
                confidence=Decimal(confidence),
                copied_elements=["logo", "layout", "favicon", "brand_name_in_domain"],
                collects_credentials_or_payment=True,
                evidence=[
                    "Image 1 shows the Mintera Bank logo and login form.",
                    "The forms ask for a password and a card number.",
                ],
                suggested_basis=NoticeBasis.PHISHING,
                short_explanation=explanation,
                created_at=seen + timedelta(minutes=minutes),
            )
        )
    case.confidence = Decimal("0.97")
    session.add_all(
        [
            Decision(
                customer_id=customer.id,
                case_id=case.id,
                outcome=DecisionOutcome.CONFIRM,
                rule="R1",
                reasons=["High-confidence phishing clone with a login and a card form."],
                decided_by="rule",
                created_at=seen + timedelta(minutes=4),
            ),
            Decision(
                customer_id=customer.id,
                case_id=case.id,
                outcome=DecisionOutcome.APPROVE,
                rule="auto_approve",
                reasons=["Both reviews and the rule checks agreed."],
                decided_by="rule",
                created_at=seen + timedelta(minutes=7),
            ),
        ]
    )


def _write_customer(writer: Writer, now: datetime) -> tuple[Customer, Brand]:
    session = writer.session
    clock = writer.clock
    customer = Customer(
        name=DEMO_NAME,
        slug=DEMO_SLUG,
        status=CustomerStatus.ACTIVE,
        is_demo=True,
        loa_signed_at=clock.at(-40, time(12, 0)),
        alert_email="security@mintera.example",
        timezone=design.TIMEZONE,
        created_at=clock.at(-60, time(12, 0)),
    )
    session.add(customer)
    session.flush()
    brand = Brand(
        customer_id=customer.id,
        name=DEMO_NAME,
        keywords=["mintera"],
        variants=["mint era", "mint-era"],
        target_markets=["PL"],
        languages=["pl", "en"],
        created_at=customer.created_at,
    )
    session.add(brand)
    session.flush()
    for domain in ("mintera.com", "mintera.pl"):
        session.add(
            OfficialDomain(
                customer_id=customer.id,
                brand_id=brand.id,
                domain=domain,
                kind=OfficialDomainKind.PRIMARY,
                created_at=customer.created_at,
            )
        )

    for index, (pattern, kind) in enumerate(design.WHITELIST_OLDER):
        session.add(
            WhitelistEntry(
                customer_id=customer.id,
                brand_id=None,
                pattern=pattern,
                kind=WhitelistKind(kind),
                source=WhitelistSource.ONBOARDING,
                created_by="onboarding",
                created_at=clock.at(-50 + index // 4, time(11, 0)),
            )
        )
    local_today = now.astimezone(clock.tz).date()
    for pattern, kind, days_ago, note in design.WHITELIST_RECENT:
        # Inside both the calendar month and the 30-day period ("added this month").
        added = max(
            datetime.combine(local_today - timedelta(days=days_ago), time(11, 0), tzinfo=clock.tz),
            datetime.combine(local_today.replace(day=1), time(9, 0), tzinfo=clock.tz),
        )
        session.add(
            WhitelistEntry(
                customer_id=customer.id,
                brand_id=None,
                pattern=pattern,
                kind=WhitelistKind(kind),
                source=WhitelistSource.MANUAL,
                note=note,
                created_by="security@mintera.example",
                created_at=min(added.astimezone(UTC), clock.reference),
            )
        )

    official_colour = (11, 42, 74)
    for kind, profile, width, height in (
        (BrandAssetKind.REFERENCE_DESKTOP, Profile.DESKTOP, 1280, 800),
        (BrandAssetKind.REFERENCE_MOBILE, Profile.MOBILE, 390, 844),
    ):
        stored = writer.upload(
            customer,
            f"brands/{brand.id}/{profile.value}.png",
            png(width, height, official_colour),
            "image/png",
        )
        session.add(
            BrandAsset(
                customer_id=customer.id,
                brand_id=brand.id,
                kind=kind,
                storage_key=stored.key,
                sha256=stored.sha256,
                source_url="https://mintera.com/",
                captured_at=clock.reference - timedelta(days=2),
            )
        )
    logo = writer.upload(
        customer, f"brands/{brand.id}/logo.png", png(512, 128, (21, 101, 192)), "image/png"
    )
    session.add(
        BrandAsset(
            customer_id=customer.id,
            brand_id=brand.id,
            kind=BrandAssetKind.LOGO_LIGHT,
            storage_key=logo.key,
            sha256=logo.sha256,
        )
    )
    return customer, brand


def _write_users(writer: Writer, customer: Customer) -> list[dict[str, Any]]:
    credentials = []
    for role, email, display_name in (
        (UserRole.OWNER, "security@mintera.example", "Mintera Security"),
        (UserRole.VIEWER, "analyst@mintera.example", "Mintera Analyst"),
    ):
        password = new_password()
        secret = new_totp_secret()
        codes = new_recovery_codes()
        writer.session.add(
            User(
                customer_id=customer.id,
                email=email,
                display_name=display_name,
                password_hash=hash_password(password),
                role=role,
                totp_secret_enc=encrypt_totp_secret(writer.settings, secret),
                totp_enabled_at=writer.clock.reference - timedelta(days=30),
                recovery_codes_hash=[hash_recovery_code(code) for code in codes],
            )
        )
        credentials.append(
            {
                "role": role.value,
                "email": email,
                "password": password,
                "totp_secret": secret,
                "recovery_codes": codes,
            }
        )
    return credentials


def _write_credentials(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    # The mode above applies only to a new file; an older, wider one is narrowed here.
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def _write_report(
    writer: Writer, customer: Customer, now: datetime, *, print_token: bool
) -> dict[str, str]:
    """Last month's report with its real CSV (no PDF: printing needs worker-browser), and
    locally a print token for the panel's e2e, which prints the page itself (§21)."""
    period = report_content.previous_month(now, customer_tz(customer))
    csv, _ = report_persist.summary(writer.session, customer, period)
    stored = writer.upload(customer, f"reports/report-{period}.csv", csv.encode(), "text/csv")
    token = new_token() if print_token else None
    report = Report(
        id=new_id(),
        customer_id=customer.id,
        period=period,
        csv_key=stored.key,
        generated_at=now,
        print_token_hash=hash_token(token) if token else None,
        print_token_expires_at=now + report_build.TOKEN_TTL if token else None,
    )
    writer.session.add(report)
    written = {"id": str(report.id), "period": period}
    return written | {"print_token": token} if token else written


def _check_names(session: Session, planned: list[PlannedCase]) -> None:
    """The domains table is shared: a demo name another customer watches is never rewritten."""
    names = [item.domain for item in planned]
    taken = session.scalars(
        select(Domain.name)
        .join(Case, Case.domain_id == Domain.id)
        .join(Customer, Customer.id == Case.customer_id)
        .where(Domain.name.in_(names), Customer.is_demo.is_(False))
        .union(
            select(Domain.name)
            .join(BrandMatch, BrandMatch.domain_id == Domain.id)
            .join(Customer, Customer.id == BrandMatch.customer_id)
            .where(Domain.name.in_(names), Customer.is_demo.is_(False))
        )
    ).all()
    if taken:
        raise SeedError(f"demo domains belong to real customers too: {', '.join(sorted(taken))}")


def seed_demo(
    database: Database,
    store: ObjectStore,
    settings: Settings,
    *,
    now: datetime | None = None,
    credentials_file: Path | None = DEFAULT_CREDENTIALS_FILE,
) -> DemoResult:
    """``credentials_file=None`` writes no file; the credentials are only in the result."""
    prod = settings.env == "prod"
    if prod and credentials_file is not None:
        raise SeedError(
            "with ENV=prod the demo seed writes no credentials file; use --print-credentials"
        )
    now = now or datetime.now(UTC)
    clock = Clock.starting_at(now)
    if (
        settings.storage_emulator_host
        and settings.gcs_evidence_bucket
        and settings.gcs_reports_bucket
    ):
        store.ensure_buckets(settings, settings.gcs_evidence_bucket, settings.gcs_reports_bucket)

    planner = Planner(clock)
    planned = plan_current_period(planner) + plan_previous_period(planner)
    with database.worker_session() as session:
        _check_names(session, planned)
        purge_demo_customer(session, store, settings)
        session.execute(delete(DomainSighting).where(DomainSighting.detail.contains(SEED_TAG)))
        writer = Writer(session, store, settings, clock)
        customer, brand = _write_customer(writer, now)
        credentials = _write_users(writer, customer)
        rng = random.Random(RANDOM_SEED + 1)
        for item in planned:
            case = writer.case(customer, brand, item, rng)
            if item.detail:
                _write_detail(writer, customer, case)
        customer_id = customer.id
        session.flush()
        report = _write_report(writer, customer, now, print_token=not prod)

    data = {
        "customer": DEMO_SLUG,
        "generated_at": now.isoformat(),
        "users": credentials,
        "report": report,
    }
    if credentials_file is not None:
        _write_credentials(credentials_file, data)
    return DemoResult(customer_id, len(planned), credentials_file, data)
