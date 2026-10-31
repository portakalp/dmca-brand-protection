"""The demo seed reproduces the design's numbers (product.md §12; K49)."""

from __future__ import annotations

import json
import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text

from kalkan.config import Settings
from kalkan.db.models import (
    Case,
    Customer,
    Domain,
    DomainSighting,
    Report,
    User,
    WhitelistEntry,
)
from kalkan.db.session import Database
from kalkan.security import decrypt_totp_secret, verify_password
from kalkan.seed import design
from kalkan.seed.demo import DEMO_SLUG, SeedError, seed_demo
from kalkan.storage.gcs import ObjectStore
from tests import factories

TZ = ZoneInfo(design.TIMEZONE)
# The design's own "now": Thursday 2026-10-08, between 10:05 and 16:05 in Warsaw.
DESIGN_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=TZ).astimezone(UTC)
EARLY_MORNING = datetime(2026, 10, 8, 5, 30, tzinfo=TZ).astimezone(UTC)

COHORT = """
WITH c AS (
    SELECT cs.*, (cs.first_seen_at AT TIME ZONE :tz)::date AS day,
           (cs.offline_since AT TIME ZONE :tz)::date AS offline_day
    FROM cases cs JOIN customers cu ON cu.id = cs.customer_id
    WHERE cu.slug = :slug AND (cs.first_seen_at AT TIME ZONE :tz)::date BETWEEN :start AND :end
)
"""
STATUS_COUNTS = (
    COHORT
    + """
SELECT count(*) AS detected,
       count(*) FILTER (WHERE state IN ('verified', 'approved')) AS active,
       count(*) FILTER (WHERE state = 'notified') AS notice_sent,
       count(*) FILTER (WHERE state IN ('taken_down', 'archived')) AS taken_down,
       count(*) FILTER (WHERE state = 'watching') AS watching,
       count(*) FILTER (WHERE state = 'dismissed') AS dismissed,
       count(*) FILTER (WHERE state IN ('detected', 'in_review')) AS reviewing,
       count(*) FILTER (WHERE verified_at IS NOT NULL) AS verified,
       percentile_cont(0.5) WITHIN GROUP (
           ORDER BY extract(epoch FROM offline_since - first_notice_at) / 3600
       ) FILTER (WHERE state IN ('taken_down', 'archived')) AS median_hours
FROM c
"""
)
CHANNELS = (
    COHORT  # noqa: S608 - constant SQL, no user input
    + """
, closing AS (
    SELECT DISTINCT ON (c.id) n.channel,
           extract(epoch FROM c.offline_since - n.sent_at) / 3600 AS hours
    FROM c JOIN notices n ON n.case_id = c.id AND n.sent_at <= c.offline_since AND NOT n.dry_run
    WHERE c.state IN ('taken_down', 'archived')
    ORDER BY c.id, n.sent_at DESC
)
SELECT channel, count(*) AS cases, percentile_cont(0.5) WITHIN GROUP (ORDER BY hours) AS median
FROM closing GROUP BY channel
"""
)
DAILY = (
    COHORT  # noqa: S608 - constant SQL, no user input
    + """
SELECT g.day,
       (SELECT count(*) FROM c WHERE c.day = g.day) AS detected,
       (SELECT count(*) FROM c WHERE c.offline_day = g.day
            AND c.state IN ('taken_down', 'archived')) AS taken_down
FROM generate_series(CAST(:start AS date), CAST(:end AS date), interval '1 day') AS g(day)
ORDER BY g.day
"""
)


def _period(now: datetime, back: int = 0) -> dict[str, Any]:
    today = now.astimezone(TZ).date() - timedelta(days=back * design.PERIOD_DAYS)
    return {
        "tz": design.TIMEZONE,
        "slug": DEMO_SLUG,
        "start": today - timedelta(days=design.PERIOD_DAYS - 1),
        "end": today,
    }


def _seed(
    database: Database,
    store: ObjectStore,
    settings: Settings,
    tmp_path: Path,
    now: datetime = DESIGN_NOW,
) -> Path:
    credentials = tmp_path / ".local" / "demo-credentials.json"
    result = seed_demo(database, store, settings, now=now, credentials_file=credentials)
    assert result.cases == 128 + design.PREVIOUS_DETECTED
    return credentials


@pytest.fixture
def seeded(
    database: Database, object_store: ObjectStore, test_settings: Settings, tmp_path: Path
) -> Path:
    return _seed(database, object_store, test_settings, tmp_path)


def test_status_distribution_and_kpis(database: Database, seeded: Path) -> None:
    with database.worker_session() as session:
        current = session.execute(text(STATUS_COUNTS), _period(DESIGN_NOW)).one()
        previous = session.execute(text(STATUS_COUNTS), _period(DESIGN_NOW, back=1)).one()
    assert (current.active, current.notice_sent, current.taken_down) == (2, 6, 39)
    assert (current.watching, current.dismissed, current.reviewing) == (69, 12, 0)
    assert current.detected == 128
    assert current.verified == 47
    assert current.median_hours == pytest.approx(31)
    assert round(100 * current.taken_down / current.verified) == 83

    assert previous.detected == 108
    assert previous.verified == 43
    assert previous.median_hours == pytest.approx(39.75)
    # product.md §12: change against the previous period, rounded half up.
    assert round(100 * (128 - 108) / 108) == 19
    assert round(100 * (47 - 43) / 43) == 9
    assert round(100 * (39.75 - 31) / 39.75) == 22


def test_takedowns_by_channel(database: Database, seeded: Path) -> None:
    with database.worker_session() as session:
        rows = session.execute(text(CHANNELS), _period(DESIGN_NOW)).all()
    by_channel = {row.channel: (row.cases, row.median) for row in rows}
    assert by_channel == {
        "cloudflare": (17, pytest.approx(22)),
        "hosting": (14, pytest.approx(34)),
        "registrar": (8, pytest.approx(51)),
    }


def test_daily_series_match_the_design_chart(database: Database, seeded: Path) -> None:
    with database.worker_session() as session:
        rows = session.execute(text(DAILY), _period(DESIGN_NOW)).all()
    assert tuple(row.detected for row in rows) == design.DETECTED_PER_DAY
    assert tuple(row.taken_down for row in rows) == design.TAKEN_DOWN_PER_DAY


def test_recent_rows_and_detail_case(database: Database, seeded: Path) -> None:
    with database.worker_session() as session:
        rows = session.execute(
            select(Domain.name, Case.state, Case.category, Case.last_action, Domain.hosting_org)
            .join(Case, Case.domain_id == Domain.id)
            .where(Domain.name.in_([row.domain for row in design.RECENT_ROWS]))
        ).all()
        found = {row.name: row for row in rows}
        for row in design.RECENT_ROWS:
            assert found[row.domain].state is row.state, row.domain
            assert found[row.domain].category is row.category, row.domain
            assert found[row.domain].last_action == row.last_action, row.domain
            assert found[row.domain].hosting_org == row.hosting, row.domain

        detail = session.execute(
            text(
                """
                SELECT d.origin_org, d.registrar, cs.confidence, cs.auto_approved,
                       (SELECT count(*) FROM artifacts a WHERE a.case_id = cs.id) AS artifacts,
                       (SELECT sum(bytes) FROM artifacts a WHERE a.case_id = cs.id) AS bytes,
                       (SELECT count(*) FROM notices n WHERE n.case_id = cs.id) AS notices,
                       (SELECT count(*) FROM verdicts v WHERE v.case_id = cs.id) AS verdicts,
                       (SELECT value FROM signals s WHERE s.case_id = cs.id
                          AND s.name = 'visual_similarity') AS similarity
                FROM cases cs JOIN domains d ON d.id = cs.domain_id WHERE d.name = :name
                """
            ),
            {"name": design.DETAIL_DOMAIN},
        ).one()
    assert detail.origin_org == "Hetzner Online GmbH"
    assert detail.registrar == "Namecheap, Inc."
    assert float(detail.confidence) == pytest.approx(0.97)
    assert detail.auto_approved is True
    assert (detail.artifacts, detail.bytes) == (5, 4_227_000)
    assert (detail.notices, detail.verdicts) == (2, 2)
    assert detail.similarity == pytest.approx(0.94)


def test_whitelist_users_and_credentials(
    database: Database, test_settings: Settings, seeded: Path
) -> None:
    month_start = date(2026, 10, 1)
    with database.worker_session() as session:
        customer = session.scalars(select(Customer).where(Customer.slug == DEMO_SLUG)).one()
        entries = session.scalars(
            select(WhitelistEntry).where(WhitelistEntry.customer_id == customer.id)
        ).all()
        users = {
            user.email: user
            for user in session.scalars(select(User).where(User.customer_id == customer.id))
        }
    assert customer.is_demo is True
    assert len(entries) == 24
    this_month = [e for e in entries if e.created_at.astimezone(TZ).date() >= month_start]
    assert sorted(e.pattern for e in this_month) == sorted(
        pattern for pattern, *_ in design.WHITELIST_RECENT
    )

    assert stat.S_IMODE(seeded.stat().st_mode) == 0o600
    credentials = json.loads(seeded.read_text())
    assert {user["role"] for user in credentials["users"]} == {"owner", "viewer"}
    for entry in credentials["users"]:
        user = users[entry["email"]]
        assert verify_password(user.password_hash, entry["password"])
        assert user.totp_secret_enc is not None
        assert decrypt_totp_secret(test_settings, user.totp_secret_enc) == entry["totp_secret"]
        assert len(user.recovery_codes_hash) == 10
        assert entry["password"] not in user.password_hash


def test_seeding_twice_creates_no_duplicates(
    database: Database, object_store: ObjectStore, test_settings: Settings, tmp_path: Path
) -> None:
    def counts() -> tuple[int, ...]:
        with database.worker_session() as session:
            return tuple(
                session.scalar(select(func.count()).select_from(model)) or 0
                for model in (Customer, Case, Domain, DomainSighting, WhitelistEntry, User)
            )

    first_credentials = _seed(database, object_store, test_settings, tmp_path)
    first = counts()
    first_password = json.loads(first_credentials.read_text())["users"][0]["password"]
    second_credentials = _seed(database, object_store, test_settings, tmp_path)
    assert counts() == first
    assert first == (1, 236, 236, 236, 24, 2)
    assert json.loads(second_credentials.read_text())["users"][0]["password"] != first_password


def test_totals_hold_when_seeding_early_in_the_morning(
    database: Database, object_store: ObjectStore, test_settings: Settings, tmp_path: Path
) -> None:
    _seed(database, object_store, test_settings, tmp_path, now=EARLY_MORNING)
    with database.worker_session() as session:
        current = session.execute(text(STATUS_COUNTS), _period(EARLY_MORNING)).one()
        latest = session.scalar(select(func.max(Case.first_seen_at)))
    assert (current.detected, current.verified, current.taken_down) == (128, 47, 39)
    assert latest is not None
    assert latest <= EARLY_MORNING


def test_seed_refuses_prod(
    database: Database, object_store: ObjectStore, test_settings: Settings, tmp_path: Path
) -> None:
    prod = test_settings.model_copy(update={"env": "prod", "storage_emulator_host": None})
    with pytest.raises(SeedError, match="ENV=prod"):
        seed_demo(database, object_store, prod, credentials_file=tmp_path / "c.json")


def test_prod_refresh_prints_only_and_leaves_other_customers(
    database: Database, object_store: ObjectStore, test_settings: Settings, tmp_path: Path
) -> None:
    """Refreshing the demo on a server (K175): no file, no print token, other tenants intact."""
    _seed(database, object_store, test_settings, tmp_path)
    with database.worker_session() as session:
        other = factories.case(
            session, (owner := factories.customer(session)), factories.brand(session, owner)
        )
        other_id, owner_id = other.id, owner.id
    prod = test_settings.model_copy(update={"env": "prod", "storage_emulator_host": None})

    result = seed_demo(database, object_store, prod, now=DESIGN_NOW, credentials_file=None)

    assert result.credentials_file is None
    assert len(list(tmp_path.glob("**/*.json"))) == 1  # the first, local run's file
    assert "print_token" not in result.credentials["report"]
    assert [user["role"] for user in result.credentials["users"]] == ["owner", "viewer"]
    assert "password" not in repr(result)
    with database.worker_session() as session:
        assert session.get(Case, other_id) is not None
        assert session.get(Customer, owner_id) is not None
        demo = session.scalar(select(Customer).where(Customer.slug == DEMO_SLUG))
        assert demo is not None
        assert demo.is_demo
        report = session.scalar(select(Report).where(Report.customer_id == demo.id))
        assert report is not None
        assert report.print_token_hash is None


def test_seed_keeps_off_domains_of_real_customers(
    database: Database, object_store: ObjectStore, test_settings: Settings, tmp_path: Path
) -> None:
    with database.worker_session() as session:
        owner = factories.customer(session)
        case = factories.case(session, owner, factories.brand(session, owner))
        session.get_one(Domain, case.domain_id).name = "mintera-login.com"
    with pytest.raises(SeedError, match=r"mintera-login\.com"):
        _seed(database, object_store, test_settings, tmp_path)


def test_seed_never_replaces_a_real_customer(
    database: Database, object_store: ObjectStore, test_settings: Settings, tmp_path: Path
) -> None:
    with database.worker_session() as session:
        factories.customer(session, DEMO_SLUG)
    with pytest.raises(SeedError, match="not a demo customer"):
        _seed(database, object_store, test_settings, tmp_path)
