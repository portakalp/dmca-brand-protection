"""HTTP pre-check against the fixture sites, the test database and fake-gcs."""

from __future__ import annotations

import ipaddress
import socket
import uuid
from collections.abc import Iterator
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import select

from kalkan import admin
from kalkan.config import REPO_ROOT, Settings
from kalkan.db.models import (
    Brand,
    BrandAsset,
    BrandMatch,
    Case,
    CaseEvent,
    Domain,
    DomainSighting,
    Scan,
    Signal,
)
from kalkan.db.session import Database
from kalkan.discovery.matcher import MatchResult
from kalkan.discovery.normalize import view_host
from kalkan.discovery.record import Sighting, record_discovery
from kalkan.domain.enums import (
    BrandAssetKind,
    CaseState,
    EventType,
    MatchReason,
    ScanStage,
    SightingSource,
)
from kalkan.enrichment.admission import admit
from kalkan.net.safe_http import NoSuchHostError, SafeHttpClient
from kalkan.scanning.brand_assets import refresh_brand_favicons
from kalkan.scanning.favicon import favicon_hash
from kalkan.scanning.precheck import REDIRECT_AGAIN, WATCH_AGAIN, Route, run_precheck
from kalkan.signals.visual import image_hashes
from kalkan.storage.gcs import ObjectStore
from tests import factories

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
FIXTURE_PORT = 18080
FIXTURE_HOSTS = {
    "mintera.test",
    "www.mintera.test",
    "mintera-login.test",
    "mintera-parked.test",
    "mintera-empty.test",
    "mintera-redirect.test",
    "mintera-mobile.test",
    "mintera-challenge.test",
    "mintera-verify.test",
    "mintera-review.test",
}
TEST_EVIDENCE_BUCKET = "kalkan-test-evidence"


@pytest.fixture(scope="module")
def client() -> Iterator[SafeHttpClient]:
    """Reaches the fixture sites on the host: *.test is 127.0.0.1, port 80 is 18080."""
    try:
        socket.create_connection(("127.0.0.1", FIXTURE_PORT), timeout=2).close()
    except OSError:
        pytest.fail("the fixture sites are not running; start them with `make up-infra`")

    def resolve(host: str) -> list[str]:
        if host in FIXTURE_HOSTS:
            return ["127.0.0.1"]
        raise NoSuchHostError(host)

    with SafeHttpClient(
        allow_cidrs=[ipaddress.ip_network("127.0.0.1/32")],
        resolver=resolve,
        port_map={80: FIXTURE_PORT, 443: FIXTURE_PORT + 363},  # nothing listens on 18443
    ) as safe:
        yield safe


def _brand(database: Database, slug: str = "mint") -> Brand:
    with database.worker_session() as session:
        owner = factories.customer(session, slug)
        brand = factories.brand(session, owner)
        brand.target_markets = ["PL"]
        admin.add_official_domain(session, brand, "mintera.test", actor="test")
        return brand


def _case(database: Database, brand: Brand, host: str, *, age_days: int | None = None) -> uuid.UUID:
    with database.worker_session() as session:
        recorded = record_discovery(
            session,
            view_host(host),
            [MatchResult(brand.id, brand.customer_id, 90, (MatchReason.KEYWORD,))],
            Sighting(SightingSource.CT, NOW),
        )
        if age_days is not None:
            domain = session.get(Domain, recorded.domain_id)
            assert domain is not None
            domain.registered_at = NOW - timedelta(days=age_days)
        result = admit(session, recorded.new_match_ids[0], now=NOW, prefix="KLK")
    assert result.case_id is not None
    return result.case_id


def _signals(database: Database, scan_id: uuid.UUID) -> dict[str, Any]:
    with database.worker_session() as session:
        return {
            signal.name: signal.value
            for signal in session.scalars(select(Signal).where(Signal.scan_id == scan_id))
        }


@pytest.fixture
def favicons(database: Database, object_store: ObjectStore, client: SafeHttpClient) -> Brand:
    brand = _brand(database)
    added = refresh_brand_favicons(
        database, client, object_store, TEST_EVIDENCE_BUCKET, brand.id, now=lambda: NOW
    )
    assert added == 1
    return brand


def test_official_favicons_are_stored_once(
    database: Database, object_store: ObjectStore, client: SafeHttpClient, favicons: Brand
) -> None:
    again = refresh_brand_favicons(
        database, client, object_store, TEST_EVIDENCE_BUCKET, favicons.id, now=lambda: NOW
    )
    assert again == 0
    with database.worker_session() as session:
        asset = session.scalars(select(BrandAsset)).one()
    assert asset.kind is BrandAssetKind.FAVICON
    assert asset.source_url == "http://mintera.test/favicon.ico"
    data = object_store.read_bytes(asset.storage_key)
    assert asset.favicon_mmh3 == favicon_hash(data)
    assert asset.storage_key.startswith(f"gs://{TEST_EVIDENCE_BUCKET}/brands/{favicons.id}/assets/")


@pytest.mark.parametrize(
    ("host", "route", "reason", "signals"),
    [
        (
            "mintera-login.test",
            Route.CAPTURE,
            "favicon match, brand in title, brand in text, password field",
            {"favicon_match": True, "password_field_html": True, "http_status": 200},
        ),
        ("mintera-parked.test", Route.WATCH, "parked", {"parked": True}),
        ("mintera-empty.test", Route.WATCH, "empty page", {"empty": True}),
        (
            "mintera-challenge.test",
            Route.CAPTURE,
            "bot challenge",
            {"challenge": True, "http_status": 403},
        ),
        (
            "mintera-redirect.test",
            Route.WATCH,
            "redirects to an official site",
            {"redirects_to_official": True},
        ),
        ("mintera-mobile.test", Route.WATCH, "nothing suspicious", {"brand_in_text": False}),
        ("mintera-review.test", Route.CAPTURE, "brand in title, brand in text", {}),
        ("mintera-gone.test", Route.WATCH, "no DNS", {"http_status": None}),
    ],
)
def test_fixture_sites_are_routed(
    database: Database,
    client: SafeHttpClient,
    favicons: Brand,
    host: str,
    route: Route,
    reason: str,
    signals: dict[str, Any],
) -> None:
    case_id = _case(database, favicons, host)
    result = run_precheck(database, client, case_id, now=lambda: NOW)
    assert result.decision is not None
    assert (result.decision.route, result.decision.reason) == (route, reason)
    assert result.scan_id is not None
    stored = _signals(database, result.scan_id)
    for name, value in signals.items():
        assert stored[name] == value, name
    with database.worker_session() as session:
        case = session.get(Case, case_id)
        scan = session.get(Scan, result.scan_id)
        events = session.scalars(
            select(CaseEvent.type).where(CaseEvent.case_id == case_id).order_by(CaseEvent.at)
        ).all()
    assert case is not None
    assert scan is not None
    assert scan.stage is ScanStage.HTTP
    assert EventType.PRECHECKED in events
    if route is Route.WATCH:
        assert case.state is CaseState.WATCHING
        expected = REDIRECT_AGAIN if reason.startswith("redirects") else WATCH_AGAIN
        assert case.next_check_at == NOW + expected
    else:
        assert case.state is CaseState.DETECTED


def test_the_clone_scan_is_recorded(
    database: Database, client: SafeHttpClient, favicons: Brand
) -> None:
    case_id = _case(database, favicons, "mintera-login.test")
    result = run_precheck(database, client, case_id, now=lambda: NOW)
    with database.worker_session() as session:
        scan = session.get(Scan, result.scan_id)
    assert scan is not None
    assert scan.outcome == "ok"
    assert scan.final_url == "http://mintera-login.test/"  # https failed, http answered
    assert scan.title == "Mintera Bank | Login"
    assert scan.text_excerpt is not None
    assert "Verify your account" in scan.text_excerpt
    assert scan.forms[0]["action"] == "http://mintera-login.test/collect"
    assert scan.error is not None  # the https attempt
    with database.worker_session() as session:
        favicon = session.scalars(
            select(Signal).where(Signal.scan_id == scan.id, Signal.name == "favicon_match")
        ).one()
    # The candidate icon's hashes, for favicon_phash_distance.
    icon = image_hashes((REPO_ROOT / "fixtures" / "sites" / "clone" / "favicon.ico").read_bytes())
    assert icon is not None
    assert favicon.value is True
    assert favicon.detail == {"phash": icon.phash, "dhash": icon.dhash}


def test_the_redirect_chain_is_kept(database: Database, client: SafeHttpClient) -> None:
    brand = _brand(database)
    result = run_precheck(
        database, client, _case(database, brand, "mintera-redirect.test"), now=lambda: NOW
    )
    with database.worker_session() as session:
        scan = session.get(Scan, result.scan_id)
    assert scan is not None
    assert scan.redirect_chain == ["http://mintera-redirect.test/"]
    assert scan.final_url == "http://mintera.test/"


def test_a_young_strong_match_goes_on_even_without_signs(
    database: Database, client: SafeHttpClient
) -> None:
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-mobile.test", age_days=3)
    result = run_precheck(database, client, case_id, now=lambda: NOW)
    assert result.decision is not None
    assert result.decision.route is Route.CAPTURE
    assert "young domain" in result.decision.reason


def test_a_login_address_page_turns_its_targets_into_candidates(
    database: Database, client: SafeHttpClient
) -> None:
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-verify.test")
    result = run_precheck(database, client, case_id, now=lambda: NOW)
    assert result.decision is not None
    assert result.decision.route is Route.CAPTURE
    # The brand-like button target and, because the page presents itself as Mintera Bank, the
    # third-party site its other button leads to; not the share link (K57, K71).
    assert len(result.new_match_ids) == 2
    with database.worker_session() as session:
        found = {}
        for match_id in result.new_match_ids:
            match = session.get(BrandMatch, match_id)
            assert match is not None
            domain = session.get(Domain, match.domain_id)
            assert domain is not None
            sighting = session.scalars(
                select(DomainSighting).where(DomainSighting.domain_id == match.domain_id)
            ).one()
            found[domain.name] = (match, sighting)
        cta = session.scalars(
            select(Signal).where(Signal.scan_id == result.scan_id, Signal.name == "cta_targets")
        ).one()
    assert set(found) == {"mintera-login.test", "quick-loans.example"}
    login_match, login_sighting = found["mintera-login.test"]
    assert login_match.score == 100
    assert login_match.reasons[0] == MatchReason.MANUAL
    assert login_sighting.source is SightingSource.MANUAL
    assert login_sighting.detail["via"] == "cta"
    assert login_sighting.detail["text"] == "Sign in"
    assert login_sighting.detail["url"] == "http://mintera-login.test/"
    assert found["quick-loans.example"][0].reasons == [MatchReason.MANUAL]
    assert cta.value == {"candidate": 1, "other": 2}
    targets = {target["host"]: target for target in cta.detail["targets"]}
    assert targets["quick-loans.example"]["candidate"] is True
    assert targets["www.facebook.com"]["candidate"] is False

    # Back in detected without a new detection: already pre-checked, nothing is repeated.
    with database.worker_session() as session:
        case = session.get(Case, case_id)
        assert case is not None
        case.state = CaseState.DETECTED
    again = run_precheck(database, client, case_id, now=lambda: NOW)
    assert again.skipped
    assert again.new_match_ids == []


def test_only_detected_and_real_cases_are_checked(
    database: Database, client: SafeHttpClient
) -> None:
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-parked.test")
    first = run_precheck(database, client, case_id, now=lambda: NOW)
    assert not first.skipped
    assert run_precheck(database, client, case_id, now=lambda: NOW).skipped  # now watching
    assert run_precheck(database, client, uuid.uuid4(), now=lambda: NOW).skipped


def test_the_actor_sends_suspicious_cases_to_the_browser_stage(
    monkeypatch: pytest.MonkeyPatch, database: Database, client: SafeHttpClient
) -> None:
    from kalkan.workers import discovery, scanning

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(scanning, "get_database", lambda: database)
    monkeypatch.setattr(scanning, "http_client", lambda: client)
    monkeypatch.setattr(scanning, "capture_redis", lambda: None)
    monkeypatch.setattr(
        scanning,
        "issue_case_capture",
        lambda db, redis, case_id, now: sent.append(("capture", str(case_id))),
    )
    monkeypatch.setattr(discovery.admit_match, "send", lambda m: sent.append(("admit", m)))
    brand = _brand(database)
    clone = _case(database, brand, "mintera-verify.test")
    parked = _case(database, brand, "mintera-parked.test")
    scanning.http_precheck.fn(str(clone))
    scanning.http_precheck.fn(str(parked))
    assert sent[0] == ("capture", str(clone))
    assert [kind for kind, _ in sent] == ["capture", "admit", "admit"]  # the lure page's targets


def test_the_last_failed_attempt_is_recorded(
    monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    from kalkan.workers import scanning

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("hostile page")

    monkeypatch.setattr(scanning, "get_database", lambda: database)
    monkeypatch.setattr(scanning, "run_precheck", broken)
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-login.test")
    monkeypatch.setattr(scanning, "final_attempt", lambda retries: False)
    with pytest.raises(RuntimeError):
        scanning.http_precheck.fn(str(case_id))  # retried by Dramatiq
    monkeypatch.setattr(scanning, "final_attempt", lambda retries: True)
    scanning.http_precheck.fn(str(case_id))
    with database.worker_session() as session:
        failed = session.scalars(
            select(CaseEvent).where(
                CaseEvent.case_id == case_id, CaseEvent.type == EventType.JOB_FAILED
            )
        ).one()
    assert failed.data["job"] == "http_precheck"
    assert "hostile page" in failed.data["error"]


def test_a_doorway_page_is_judged_by_where_it_sends(database: Database) -> None:
    # A near-empty "current login address" page that refreshes to another brand-like domain.
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = (
            b"<title>Mintera Bank</title>"
            b'<meta http-equiv="refresh" content="0; https://mintera-new.example/">'
        )
        return httpx2.Response(200, stream=httpx2.ByteStream(body))

    doorway = SafeHttpClient(
        resolver=lambda host: ["93.184.215.14"], transport=httpx2.MockTransport(handler)
    )
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-door.example")
    result = run_precheck(database, doorway, case_id, now=lambda: NOW)
    assert result.decision is not None
    assert result.decision.route is Route.CAPTURE
    assert "redirect to a brand-like domain" in result.decision.reason
    signals = _signals(database, result.scan_id)  # type: ignore[arg-type]
    assert signals["empty"] is True
    assert signals["brand_redirect"] is True
    with database.worker_session() as session:
        names = {
            session.get(Domain, session.get(BrandMatch, match_id).domain_id).name  # type: ignore[union-attr]
            for match_id in result.new_match_ids
        }
    assert names == {"mintera-new.example"}


def test_favicon_jobs_run_per_official_domain(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    test_settings: Settings,
    object_store: ObjectStore,
) -> None:
    from kalkan.workers import scanning

    sent: list[tuple[str, ...]] = []
    monkeypatch.setattr(scanning, "get_database", lambda: database)
    monkeypatch.setattr(scanning.refresh_brand_favicons, "send", lambda *a: sent.append(a))
    brand = _brand(database)
    with database.worker_session() as session:
        current = session.get(Brand, brand.id)
        assert current is not None
        admin.add_official_domain(session, current, "mintera2.test", actor="test")
    scanning.refresh_brand_favicons.fn(str(brand.id))
    assert sent == [(str(brand.id), "mintera.test"), (str(brand.id), "mintera2.test")]
    # A brand deleted in the meantime is skipped, not retried.
    monkeypatch.setattr(scanning, "get_settings", lambda: test_settings)
    scanning.refresh_brand_favicons.fn(str(uuid.uuid4()), "mintera.test")


def test_cli_brand_favicons(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    test_settings: Settings,
    object_store: ObjectStore,
    client: SafeHttpClient,
) -> None:
    from typer.testing import CliRunner

    from kalkan import cli

    monkeypatch.setattr(cli, "get_database", lambda: database)
    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)
    monkeypatch.setattr(cli, "SafeHttpClient", lambda **_: nullcontext(client))
    brand = _brand(database)
    result = CliRunner().invoke(cli.app, ["brand", "favicons", "--brand", str(brand.id)])
    assert result.exit_code == 0, result.output
    assert "Stored 1 new favicon(s)" in result.output
    unknown = CliRunner().invoke(cli.app, ["brand", "favicons", "--brand", str(uuid.uuid4())])
    assert unknown.exit_code == 1
    assert "no brand with id" in unknown.output
