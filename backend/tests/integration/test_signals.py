"""compute_signals, brand asset hashes and the case snapshot against the test
database and fake-gcs.

The case is the fixture clone (``mintera-login.test``) as the browser stage would have stored
it: two ``ok`` scans with the committed fixture screenshots, its forms and buttons. The
references are the official site's screenshots and text; no browser runs here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from kalkan import admin
from kalkan.config import REPO_ROOT
from kalkan.db.models import (
    Artifact,
    Brand,
    BrandAsset,
    Case,
    CaseEvent,
    Domain,
    Scan,
    Signal,
)
from kalkan.db.session import Database
from kalkan.discovery.matcher import MatchResult
from kalkan.discovery.normalize import view_host
from kalkan.discovery.record import Sighting, record_discovery
from kalkan.domain.case_state import transition
from kalkan.domain.enums import (
    ArtifactKind,
    BrandAssetKind,
    CaseState,
    EventType,
    MatchReason,
    Profile,
    ScanStage,
    SightingSource,
)
from kalkan.enrichment.admission import admit
from kalkan.scanning.capture_contract import ButtonInfo, FormInfo, InputInfo
from kalkan.scanning.html import parse_page
from kalkan.signals import compute
from kalkan.signals.assets import hash_brand_assets
from kalkan.signals.compute import ComputeStatus
from kalkan.signals.snapshot import SNAPSHOT_VERSION, CaseSnapshot, build_snapshot
from kalkan.signals.visual import image_hashes
from kalkan.storage.gcs import ObjectStore
from tests import factories

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
BUCKET = "kalkan-test-evidence"
SCREENSHOTS = REPO_ROOT / "fixtures" / "screenshots"
SITES = REPO_ROOT / "fixtures" / "sites"
REGISTERED = NOW - timedelta(days=5, hours=3)


def _text(site: str) -> tuple[str | None, str]:
    page = parse_page((SITES / site / "index.html").read_text(encoding="utf-8"), "https://x.test/")
    return page.title, page.text


def _put(store: ObjectStore, path: str, data: bytes, content_type: str) -> str:
    return store.put(BUCKET, f"{uuid.uuid4()}/{path}", data, content_type).key


def _brand(database: Database, store: ObjectStore) -> Brand:
    _, official_text = _text("official")
    with database.worker_session() as session:
        owner = factories.customer(session, f"signals-{uuid.uuid4().hex[:6]}")
        brand = factories.brand(session, owner)
        brand.keywords = ["mintera"]
        admin.add_official_domain(session, brand, "mintera.test", actor="test")
        for profile, kind in (
            (Profile.DESKTOP, BrandAssetKind.REFERENCE_DESKTOP),
            (Profile.MOBILE, BrandAssetKind.REFERENCE_MOBILE),
        ):
            data = (SCREENSHOTS / f"official-{profile.value}.png").read_bytes()
            session.add(
                BrandAsset(
                    customer_id=owner.id,
                    brand_id=brand.id,
                    kind=kind,
                    storage_key=_put(store, f"{profile.value}.png", data, "image/png"),
                    sha256="0" * 64,
                    text_excerpt=official_text,
                    source_url="https://mintera.test/",
                    captured_at=NOW - timedelta(days=1),
                )
            )
        icon = (SITES / "official" / "favicon.ico").read_bytes()
        session.add(
            BrandAsset(
                customer_id=owner.id,
                brand_id=brand.id,
                kind=BrandAssetKind.FAVICON,
                storage_key=_put(store, "favicon.ico", icon, "image/x-icon"),
                sha256="1" * 64,
                favicon_mmh3=1,
            )
        )
        return brand


def _clone_forms() -> list[dict[str, Any]]:
    form = FormInfo(
        action="https://mintera-login.test/login",
        method="post",
        inputs=[
            InputInfo(type="email", name="email", label="E-mail"),
            InputInfo(type="password", name="password", label="Password"),
            InputInfo(type="text", name="card", label="Card number"),
        ],
        buttons=[ButtonInfo(text="Deposit and sign in", type="submit")],
    )
    return [form.model_dump(mode="json")]


def _case(
    database: Database,
    store: ObjectStore,
    brand: Brand,
    *,
    mobile_site: str = "clone",
    mobile_shot: str = "clone",
) -> uuid.UUID:
    with database.worker_session() as session:
        recorded = record_discovery(
            session,
            view_host("mintera-login.test"),
            [MatchResult(brand.id, brand.customer_id, 90, (MatchReason.KEYWORD,))],
            Sighting(SightingSource.CT, NOW),
        )
        result = admit(session, recorded.new_match_ids[0], now=NOW, prefix="KLK")
        assert result.case_id is not None
        case = session.get_one(Case, result.case_id)
        domain = session.get_one(Domain, case.domain_id)
        domain.registered_at = REGISTERED
        domain.webrisk = {"threat_types": ["SOCIAL_ENGINEERING"], "expire_time": None}
        http = Scan(
            customer_id=case.customer_id,
            case_id=case.id,
            stage=ScanStage.HTTP,
            started_at=NOW,
            outcome="ok",
            final_url="https://mintera-login.test/",
        )
        session.add(http)
        session.flush()
        hashes = image_hashes((SITES / "clone" / "favicon.ico").read_bytes())
        assert hashes is not None
        session.add(
            Signal(
                customer_id=case.customer_id,
                case_id=case.id,
                scan_id=http.id,
                name="favicon_match",
                value=True,
                detail={"phash": hashes.phash, "dhash": hashes.dhash},
            )
        )
        for profile, site, shot in (
            (Profile.DESKTOP, "clone", "clone"),
            (Profile.MOBILE, mobile_site, mobile_shot),
        ):
            title, text = _text(site)
            scan = Scan(
                customer_id=case.customer_id,
                case_id=case.id,
                stage=ScanStage.BROWSER,
                profile=profile,
                started_at=NOW + timedelta(minutes=5),
                finished_at=NOW + timedelta(minutes=6),
                outcome="ok",
                http_status=200,
                final_url="https://mintera-login.test/",
                title=title,
                text_excerpt=text,
                forms=_clone_forms() if site == "clone" else [],
                buttons=[],
            )
            session.add(scan)
            session.flush()
            data = (SCREENSHOTS / f"{shot}-{profile.value}.png").read_bytes()
            session.add(
                Artifact(
                    customer_id=case.customer_id,
                    case_id=case.id,
                    scan_id=scan.id,
                    kind=ArtifactKind.SCREENSHOT,
                    profile=profile,
                    storage_key=_put(store, f"shot-{profile.value}.png", data, "image/png"),
                    sha256="2" * 64,
                    bytes=len(data),
                    content_type="image/png",
                )
            )
        return case.id


def _signals(database: Database, case_id: uuid.UUID) -> dict[tuple[str, str | None], Any]:
    with database.worker_session() as session:
        rows = session.execute(
            select(Signal.id, Signal.name, Signal.value, Signal.detail, Scan.profile)
            .outerjoin(Scan, Scan.id == Signal.scan_id)
            .where(Signal.case_id == case_id)
        )
        return {
            (name, profile.value if profile else None): (row_id, value, detail)
            for row_id, name, value, detail, profile in rows
        }


def _value(found: dict[tuple[str, str | None], Any], name: str, profile: str | None = None) -> Any:
    return found[(name, profile)][1]


# --- tests ------------------------------------------------------------------------------------


def test_the_clone_gets_its_signals_and_a_second_run_changes_nothing(
    database: Database, object_store: ObjectStore
) -> None:
    brand = _brand(database, object_store)
    case_id = _case(database, object_store, brand)

    status = compute.compute_signals(database, object_store, case_id)
    assert status is ComputeStatus.COMPUTED
    first = _signals(database, case_id)

    assert _value(first, "visual_similarity") >= 0.85
    assert _value(first, "phash_distance_desktop") <= 9
    assert _value(first, "phash_distance_mobile") <= 9
    assert _value(first, "favicon_phash_distance") == 0  # the same icon file
    assert _value(first, "text_similarity") >= 80
    assert _value(first, "cloaking_suspected") is False
    assert _value(first, "domain_age_days") == 5
    assert _value(first, "webrisk_listed") is True
    for profile in ("desktop", "mobile"):
        assert _value(first, "form_password", profile) is True
        assert _value(first, "form_card", profile) is True
        assert _value(first, "deposit_cta", profile) is True
        assert _value(first, "form_iban", profile) is False
        assert _value(first, "external_form_action", profile) is False
        assert _value(first, "brand_in_title", profile) is True
        assert _value(first, "redirects_to_official", profile) is False
    detail = first[("visual_similarity", None)][2]
    assert set(detail["profiles"]) == {"desktop", "mobile"}

    with database.worker_session() as session:
        assets = list(session.scalars(select(BrandAsset).where(BrandAsset.brand_id == brand.id)))
    assert all(asset.phash and asset.dhash for asset in assets)  # hashed on the way

    assert compute.compute_signals(database, object_store, case_id) is ComputeStatus.COMPUTED
    assert _signals(database, case_id) == first  # same ids, values and details


def test_cloaking_shows_when_mobile_is_another_site(
    database: Database, object_store: ObjectStore
) -> None:
    brand = _brand(database, object_store)
    case_id = _case(database, object_store, brand, mobile_site="mobile", mobile_shot="unrelated")
    compute.compute_signals(database, object_store, case_id)
    found = _signals(database, case_id)
    assert _value(found, "cloaking_suspected") is True
    assert _value(found, "form_password", "mobile") is False
    # The best profile counts: the desktop clone still looks like the brand.
    assert _value(found, "visual_similarity") >= 0.85
    assert _value(found, "phash_distance_mobile") > 25


def test_missing_references_give_empty_values_with_a_reason(
    database: Database, object_store: ObjectStore
) -> None:
    brand = _brand(database, object_store)
    with database.worker_session() as session:
        for asset in session.scalars(select(BrandAsset).where(BrandAsset.brand_id == brand.id)):
            session.delete(asset)
    case_id = _case(database, object_store, brand)
    compute.compute_signals(database, object_store, case_id)
    found = _signals(database, case_id)
    assert _value(found, "visual_similarity") is None
    assert found[("phash_distance_desktop", None)][2]["reason"] == "no_reference"
    assert _value(found, "favicon_phash_distance") is None
    assert _value(found, "text_similarity") is None
    assert _value(found, "form_password", "desktop") is True


def test_a_case_that_left_detected_or_was_not_captured_gets_nothing(
    database: Database, object_store: ObjectStore
) -> None:
    brand = _brand(database, object_store)
    case_id = _case(database, object_store, brand)
    with database.worker_session() as session:
        case = session.get_one(Case, case_id)
        transition(session, case, CaseState.IN_REVIEW, actor="test", reason="test")
    assert compute.compute_signals(database, object_store, case_id) is ComputeStatus.SKIPPED
    assert compute.compute_signals(database, object_store, uuid.uuid4()) is ComputeStatus.SKIPPED
    assert not [key for key in _signals(database, case_id) if key[0] != "favicon_match"]

    with database.worker_session() as session:
        owner = factories.customer(session, f"bare-{uuid.uuid4().hex[:6]}")
        bare = factories.case(session, owner, factories.brand(session, owner))
        bare_id = bare.id
    assert compute.compute_signals(database, object_store, bare_id) is ComputeStatus.SKIPPED


def test_brand_assets_are_hashed_once(database: Database, object_store: ObjectStore) -> None:
    brand = _brand(database, object_store)
    with database.worker_session() as session:
        session.add(
            BrandAsset(
                customer_id=brand.customer_id,
                brand_id=brand.id,
                kind=BrandAssetKind.FAVICON,
                storage_key=_put(object_store, "icon.svg", b"<svg></svg>", "image/svg+xml"),
                sha256="3" * 64,
            )
        )
    assert hash_brand_assets(database, object_store, brand.id) == 3  # the SVG is not hashed
    assert hash_brand_assets(database, object_store, brand.id) == 0
    with database.worker_session() as session:
        assets = {
            asset.kind: asset
            for asset in session.scalars(select(BrandAsset).where(BrandAsset.brand_id == brand.id))
            if asset.phash
        }
    reference = (SCREENSHOTS / "official-desktop.png").read_bytes()
    expected = image_hashes(reference, screenshot=True)
    assert expected is not None
    assert assets[BrandAssetKind.REFERENCE_DESKTOP].phash == expected.phash
    assert len(assets[BrandAssetKind.FAVICON].phash or "") == 16


def test_the_snapshot_holds_the_case_and_reads_back(
    database: Database, object_store: ObjectStore
) -> None:
    brand = _brand(database, object_store)
    case_id = _case(database, object_store, brand)
    compute.compute_signals(database, object_store, case_id)
    with database.worker_session() as session:
        snapshot = build_snapshot(session, case_id, now=NOW)
        assert build_snapshot(session, uuid.uuid4(), now=NOW) is None
    assert snapshot is not None
    assert snapshot.version == SNAPSHOT_VERSION
    assert snapshot.case.id == case_id
    assert snapshot.domain.name == "mintera-login.test"
    assert snapshot.domain.age_days == 5
    assert snapshot.domain.webrisk_threat_types == ["SOCIAL_ENGINEERING"]
    assert snapshot.match is not None
    assert snapshot.match.score == 90
    assert [o.domain for o in snapshot.brand.official_domains] == ["mintera.test"]
    assert [(s.stage, s.profile) for s in snapshot.scans] == [
        (ScanStage.HTTP, None),
        (ScanStage.BROWSER, Profile.DESKTOP),
        (ScanStage.BROWSER, Profile.MOBILE),
    ]
    assert all(s.screenshot_artifact_id for s in snapshot.scans[1:])
    assert snapshot.flag("form_password") is True
    assert snapshot.flag("favicon_match") is True
    assert snapshot.flag("form_iban") is False
    assert snapshot.flag("official_infrastructure") is None
    visual = snapshot.value("visual_similarity")
    assert isinstance(visual, float)
    assert visual >= 0.85
    kinds = {reference.kind for reference in snapshot.references}
    assert BrandAssetKind.REFERENCE_DESKTOP in kinds
    # Deterministic for the same rows and time, and a JSON round trip gives it back.
    with database.worker_session() as session:
        again = build_snapshot(session, case_id, now=NOW)
    assert again == snapshot
    assert CaseSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


@pytest.fixture
def signals_actor(
    monkeypatch: pytest.MonkeyPatch, database: Database, object_store: ObjectStore
) -> list[str]:
    from kalkan.workers import persist as persist_worker
    from kalkan.workers import signals as worker

    sent: list[str] = []
    monkeypatch.setattr(worker, "get_database", lambda: database)
    monkeypatch.setattr(persist_worker, "evidence_store", lambda: (object_store, BUCKET))
    monkeypatch.setattr(worker, "send_ai_triage", sent.append)
    return sent


def test_the_actor_queues_triage_and_records_its_last_failure(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    object_store: ObjectStore,
    signals_actor: list[str],
) -> None:
    from kalkan.workers import signals as worker

    brand = _brand(database, object_store)
    case_id = _case(database, object_store, brand)
    worker.compute_signals.fn(str(case_id))
    assert signals_actor == [str(case_id)]
    worker.hash_brand_assets.fn(str(brand.id))  # already hashed: a no-op

    def broken(*args: object, **kwargs: object) -> ComputeStatus:
        raise ConnectionError("storage unavailable")

    monkeypatch.setattr(worker.compute, "compute_signals", broken)
    monkeypatch.setattr(worker, "final_attempt", lambda retries: False)
    with pytest.raises(ConnectionError):
        worker.compute_signals.fn(str(case_id))
    monkeypatch.setattr(worker, "final_attempt", lambda retries: True)
    worker.compute_signals.fn(str(case_id))
    with database.worker_session() as session:
        failed = list(
            session.scalars(
                select(CaseEvent).where(
                    CaseEvent.case_id == case_id, CaseEvent.type == EventType.JOB_FAILED
                )
            )
        )
    assert [event.data["job"] for event in failed] == ["compute_signals"]
    assert signals_actor == [str(case_id)]


def _admitted(database: Database, brand: Brand, host: str, at: datetime) -> uuid.UUID:
    with database.worker_session() as session:
        recorded = record_discovery(
            session,
            view_host(host),
            [MatchResult(brand.id, brand.customer_id, 90, (MatchReason.KEYWORD,))],
            Sighting(SightingSource.CT, at),
        )
        result = admit(session, recorded.new_match_ids[0], now=at, prefix="KLK")
    assert result.case_id is not None
    return result.case_id


def _browser_scan(
    database: Database, case_id: uuid.UUID, at: datetime, *, signal: bool = False
) -> None:
    with database.worker_session() as session:
        case = session.get_one(Case, case_id)
        scan = Scan(
            customer_id=case.customer_id,
            case_id=case_id,
            stage=ScanStage.BROWSER,
            profile=Profile.DESKTOP,
            outcome="ok",
            started_at=at,
        )
        session.add(scan)
        session.flush()
        if signal:
            session.add(
                Signal(
                    customer_id=case.customer_id,
                    case_id=case_id,
                    scan_id=scan.id,
                    name="form_password",
                    value=False,
                )
            )


def test_the_reconciler_requeues_captures_without_signals(
    database: Database, object_store: ObjectStore
) -> None:
    from kalkan.db.models import ReviewTask
    from kalkan.domain.enums import TaskKind, TaskStatus
    from kalkan.scheduler import reconciler

    brand = _brand(database, object_store)
    entered = NOW - timedelta(hours=3)
    names = ("lost", "recent", "computed", "recaptured", "stale", "reviewed", "failed")
    cases = {name: _admitted(database, brand, f"mintera-{name}.test", entered) for name in names}
    hour_ago = NOW - timedelta(hours=1)
    for name in ("lost", "reviewed", "failed"):
        _browser_scan(database, cases[name], hour_ago)
    _browser_scan(database, cases["recent"], NOW - timedelta(minutes=10))
    _browser_scan(database, cases["computed"], hour_ago, signal=True)
    # An older capture never gets per-scan signals; the newest one has them.
    _browser_scan(database, cases["recaptured"], NOW - timedelta(hours=2))
    _browser_scan(database, cases["recaptured"], hour_ago, signal=True)
    # The newest capture of the profile has none: compute again.
    _browser_scan(database, cases["stale"], NOW - timedelta(hours=2), signal=True)
    _browser_scan(database, cases["stale"], hour_ago)
    with database.worker_session() as session:
        session.add(
            ReviewTask(
                customer_id=brand.customer_id,
                case_id=cases["reviewed"],
                kind=TaskKind.REVIEW,
                status=TaskStatus.OPEN,
            )
        )
        session.add(
            CaseEvent(
                customer_id=brand.customer_id,
                case_id=cases["failed"],
                at=NOW - timedelta(minutes=50),
                type=EventType.JOB_FAILED,
                actor="system",
                data={"job": "compute_signals"},
            )
        )
    sent: list[tuple[str, dict[str, str]]] = []
    result = reconciler.reconcile(
        database, lambda actor, **kwargs: sent.append((actor, kwargs)), NOW
    )
    signals = sorted(kwargs["case_id"] for actor, kwargs in sent if actor == "compute_signals")
    assert signals == sorted(str(cases[name]) for name in ("lost", "stale"))
    assert result.signals == 2
