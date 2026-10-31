"""Capture jobs, persist_capture, reference screenshots and the capture reconciler rule
against the test database, Redis and fake-gcs.

Job folders are written here with the ``capture_contract`` models, as the browser would; no
browser is needed.
"""

from __future__ import annotations

import hashlib
import os
import struct
import uuid
import zlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import dramatiq
import pytest
import redis
from sqlalchemy import func, select

from kalkan import admin
from kalkan.config import Settings
from kalkan.db.models import (
    Artifact,
    Brand,
    BrandAsset,
    Case,
    CaseEvent,
    Customer,
    JobOutbox,
    ReviewTask,
    Scan,
    Signal,
)
from kalkan.db.outbox import REFRESH_BRAND_REFERENCES
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
    OfficialDomainKind,
    Profile,
    ScanStage,
    SightingSource,
    TaskKind,
    TaskStatus,
)
from kalkan.enrichment.admission import admit
from kalkan.scanning import persist
from kalkan.scanning.capture_contract import (
    FILES,
    META_FILE,
    PENDING_KEY,
    ButtonInfo,
    CaptureJob,
    CaptureMeta,
    CaptureRecord,
    FormInfo,
    InputInfo,
    LinkInfo,
    Outcome,
    ProfileResult,
    Purpose,
    file_name,
)
from kalkan.scanning.capture_jobs import issue_case_capture, load_record
from kalkan.scanning.persist import PNG_MAGIC, PersistResult, PersistStatus
from kalkan.scanning.references import NoReferenceError, issue_reference_capture
from kalkan.scheduler import outbox, reconciler
from kalkan.storage.gcs import ObjectExistsError, ObjectStore, sha256_hex
from tests import factories
from tests.support import redis_url_for_tests

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
BUCKET = "kalkan-test-evidence"
SERVICES_HINT = "start the services with `make up-infra`"


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    rows = b"".join(b"\x00" + b"\xc0\x10\x30" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"".join(
        (
            PNG_MAGIC,
            chunk(b"IHDR", header),
            chunk(b"IDAT", zlib.compress(rows)),
            chunk(b"IEND", b""),
        )
    )


def _never(job: CaptureJob) -> None:
    pytest.fail(f"no job expected, got {job.job_id}")


SIZES = {
    ArtifactKind.SCREENSHOT: {Profile.DESKTOP: (1280, 800), Profile.MOBILE: (390, 844)},
    ArtifactKind.SCREENSHOT_FULL: {Profile.DESKTOP: (1280, 2400), Profile.MOBILE: (390, 3000)},
}
CONTENT: dict[ArtifactKind, Callable[[Profile], bytes]] = {
    ArtifactKind.SCREENSHOT: lambda p: _png(*SIZES[ArtifactKind.SCREENSHOT][p]),
    ArtifactKind.SCREENSHOT_FULL: lambda p: _png(*SIZES[ArtifactKind.SCREENSHOT_FULL][p]),
    ArtifactKind.HTML: lambda p: (
        f"<html><title>Mintera Bank | Login</title>{p.value}</html>".encode()
    ),
    ArtifactKind.HAR: lambda p: b'{"log": {"version": "1.2", "entries": []}}',
}


# --- fixtures and helpers ---------------------------------------------------------------------


def _flush(client: redis.Redis) -> None:
    for key in client.scan_iter("capture:*"):
        client.delete(key)


@pytest.fixture
def capture_redis() -> Iterator[redis.Redis]:
    """The test Redis database (db 15) without capture records or markers."""
    client = redis.Redis.from_url(redis_url_for_tests())
    try:
        client.ping()
    except redis.ConnectionError:
        pytest.fail(f"cannot connect to Redis; {SERVICES_HINT}")
    _flush(client)
    yield client
    _flush(client)
    client.close()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Stands in for /artifacts-tmp."""
    folder = tmp_path / "artifacts-tmp"
    folder.mkdir()
    return folder


def _brand(database: Database, slug: str = "mint", **values: Any) -> Brand:
    with database.worker_session() as session:
        owner = factories.customer(session, slug, **values)
        brand = factories.brand(session, owner)
        brand.target_markets = ["PL", "TR"]
        admin.add_official_domain(session, brand, "mintera.test", actor="test")
        return brand


def _case(database: Database, brand: Brand, host: str, at: datetime = NOW) -> uuid.UUID:
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


def _demo_case(database: Database, brand: Brand, at: datetime = NOW) -> uuid.UUID:
    """Admission skips the demo workspace (K46); its cases come from the seed."""
    with database.worker_session() as session:
        owner = session.get_one(Customer, brand.customer_id)
        case = factories.case(session, owner, session.get_one(Brand, brand.id))
        session.add(
            CaseEvent(
                customer_id=owner.id, case_id=case.id, at=at, type=EventType.DETECTED, actor="t"
            )
        )
        return case.id


def _issue(
    database: Database, client: redis.Redis, case_id: uuid.UUID
) -> tuple[CaptureJob, CaptureRecord]:
    sent: list[CaptureJob] = []
    job = issue_case_capture(database, client, case_id, now=NOW, send=sent.append)
    assert job is not None
    assert sent == [job]
    record = load_record(client, job.job_id)
    assert record is not None
    return job, record


def _result(
    profile: Profile,
    outcome: Outcome = Outcome.OK,
    files: tuple[ArtifactKind, ...] = (ArtifactKind.SCREENSHOT, ArtifactKind.HTML),
    **values: Any,
) -> ProfileResult:
    defaults: dict[str, Any] = {
        "profile": profile,
        "outcome": outcome,
        "started_at": NOW,
        "finished_at": NOW + timedelta(seconds=20),
        "files": list(files),
    }
    if outcome is not Outcome.ERROR:
        defaults |= {
            "http_status": 200,
            "final_url": "https://mintera-login.test/",
            "redirect_chain": ["http://mintera-login.test/"],
            "title": "Mintera Bank | Login",
            "text_excerpt": "Mintera Bank login. Deposit and sign in.",
        }
    return ProfileResult.model_validate(defaults | values)


def _write(
    root: Path,
    job_id: uuid.UUID,
    results: list[ProfileResult],
    *,
    final: bool = False,
    error: str | None = None,
    extra: dict[str, bytes] | None = None,
) -> Path:
    folder = root / str(job_id)
    folder.mkdir()
    for result in results:
        for kind in result.files:
            (folder / file_name(kind, result.profile)).write_bytes(CONTENT[kind](result.profile))
    for name, data in (extra or {}).items():
        (folder / name).write_bytes(data)
    meta = CaptureMeta(
        job_id=job_id,
        final_attempt=final,
        proxy_country="PL",
        browser_version="Chromium 147.0",
        profiles=results,
        error=error,
    )
    (folder / META_FILE).write_text(meta.model_dump_json())
    return folder


def _persist(
    database: Database, client: redis.Redis, store: ObjectStore, root: Path, job_id: uuid.UUID
) -> PersistResult:
    return persist.persist_capture(
        database, client, store, BUCKET, job_id, now=lambda: NOW, root=root
    )


def _rows(database: Database, model: Any, case_id: uuid.UUID) -> list[Any]:
    with database.worker_session() as session:
        return list(session.scalars(select(model).where(model.case_id == case_id)))


def _events(database: Database, case_id: uuid.UUID) -> list[CaseEvent]:
    with database.worker_session() as session:
        return list(
            session.scalars(
                select(CaseEvent).where(CaseEvent.case_id == case_id).order_by(CaseEvent.at)
            )
        )


def _state(database: Database, case_id: uuid.UUID) -> CaseState:
    with database.worker_session() as session:
        return session.get_one(Case, case_id).state


def _open_task(database: Database, case_id: uuid.UUID) -> ReviewTask | None:
    with database.worker_session() as session:
        return session.scalar(
            select(ReviewTask).where(
                ReviewTask.case_id == case_id,
                ReviewTask.kind == TaskKind.REVIEW,
                ReviewTask.status == TaskStatus.OPEN,
            )
        )


CLONE_DESKTOP: dict[str, Any] = {
    "forms": [
        FormInfo(
            action="https://mintera-login.test/collect",
            method="post",
            inputs=[
                InputInfo(type="password", name="password", kind="password"),
                InputInfo(type="text", name="card", autocomplete="cc-number", kind="card_number"),
            ],
            buttons=[ButtonInfo(text="Deposit and sign in", type="submit", intent="deposit")],
        )
    ],
    "buttons": [ButtonInfo(text="Wpłać teraz", type="button")],
    "links": [
        LinkInfo(url="https://mintera-new.example/", text="New address", tag="a"),
        LinkInfo(url="https://quick-loans.example/?ref=mnt", text="Claim refund", tag="onclick"),
        LinkInfo(url="https://www.facebook.com/sharer", text="Share", tag="a"),
        LinkInfo(url="https://mintera.test/", text="Home", tag="a"),
    ],
}


# --- persist_capture: success, challenge, failure ---------------------------------------------


def test_a_capture_is_stored_with_matching_hashes(
    database: Database, capture_redis: redis.Redis, object_store: ObjectStore, root: Path
) -> None:
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-login.test")
    job, record = _issue(database, capture_redis, case_id)
    every = tuple(FILES)
    desktop = _result(Profile.DESKTOP, files=every, **CLONE_DESKTOP)
    mobile = _result(Profile.MOBILE, files=(ArtifactKind.SCREENSHOT, ArtifactKind.HTML))
    folder = _write(root, job.job_id, [desktop, mobile], extra={"evil.sh": b"rm -rf /"})
    # An earlier attempt uploaded one file, then failed: the object is reused.
    early = paths_for(record, Profile.DESKTOP, ArtifactKind.SCREENSHOT)
    object_store.put(BUCKET, early, CONTENT[ArtifactKind.SCREENSHOT](Profile.DESKTOP), "image/png")

    result = _persist(database, capture_redis, object_store, root, job.job_id)

    assert result.status is PersistStatus.STORED
    assert result.compute_signals
    assert result.case_id == case_id
    scans = {scan.profile: scan for scan in _rows(database, Scan, case_id)}
    assert {profile: scan.id for profile, scan in scans.items()} == record.scan_ids
    assert {scan.stage for scan in scans.values()} == {ScanStage.BROWSER}
    assert {scan.proxy_country for scan in scans.values()} == {"PL"}
    assert scans[Profile.DESKTOP].outcome == "ok"
    assert scans[Profile.DESKTOP].title == "Mintera Bank | Login"
    inputs = scans[Profile.DESKTOP].forms[0]["inputs"]
    assert [field["kind"] for field in inputs] == ["password", "card_number"]
    assert [button["text"] for button in scans[Profile.DESKTOP].buttons] == ["Wpłać teraz"]
    assert scans[Profile.MOBILE].buttons == []

    artifacts = _rows(database, Artifact, case_id)
    assert len(artifacts) == len(every) + 2  # evil.sh is ignored
    for artifact in artifacts:
        assert artifact.profile is not None
        spec = FILES[artifact.kind]
        data = CONTENT[artifact.kind](artifact.profile)
        expected = paths_for(record, artifact.profile, artifact.kind)
        assert artifact.storage_key == f"gs://{BUCKET}/{expected}"
        assert artifact.scan_id == record.scan_ids[artifact.profile]
        assert artifact.sha256 == hashlib.sha256(data).hexdigest()
        assert sha256_hex(object_store.read_bytes(artifact.storage_key)) == artifact.sha256
        assert object_store.stored_sha256(artifact.storage_key) == artifact.sha256
        assert (artifact.bytes, artifact.content_type) == (len(data), spec.content_type)

    events = _events(database, case_id)
    captured = [event for event in events if event.type is EventType.CAPTURED]
    assert len(captured) == 1
    assert captured[0].data["outcomes"] == {"desktop": "ok", "mobile": "ok"}
    assert _state(database, case_id) is CaseState.DETECTED

    # Buttons and links: the brand-like one and the third-party button become candidates (K57).
    with database.worker_session() as session:
        signal = session.scalar(
            select(Signal).where(
                Signal.scan_id == record.scan_ids[Profile.DESKTOP], Signal.name == "cta_targets"
            )
        )
        assert signal is not None
        chosen = {t["host"] for t in signal.detail["targets"] if t["candidate"]}
        assert chosen == {"mintera-new.example", "quick-loans.example"}
        assert signal.value["official"] == 1
    assert len(result.new_match_ids) == 2

    assert not folder.exists()
    assert capture_redis.get(PENDING_KEY.format(case_id=case_id)) is None

    # A redelivered message stores nothing twice.
    _write(root, job.job_id, [desktop, mobile])
    again = _persist(database, capture_redis, object_store, root, job.job_id)
    assert again.status is PersistStatus.SKIPPED
    assert len(_rows(database, Artifact, case_id)) == len(artifacts)
    assert len(_rows(database, Scan, case_id)) == 2
    assert not (root / str(job.job_id)).exists()


def paths_for(record: CaptureRecord, profile: Profile, kind: ArtifactKind) -> str:
    assert record.case_id is not None
    scan_id = record.scan_ids[profile]
    return f"cases/{record.case_id}/{scan_id}/{kind.value}-{profile.value}.{FILES[kind].extension}"


def test_a_different_object_at_the_path_is_an_error(
    database: Database, capture_redis: redis.Redis, object_store: ObjectStore, root: Path
) -> None:
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-login.test")
    job, record = _issue(database, capture_redis, case_id)
    _write(root, job.job_id, [_result(Profile.DESKTOP), _result(Profile.MOBILE)])
    path = paths_for(record, Profile.DESKTOP, ArtifactKind.HTML)
    object_store.put(BUCKET, path, b"something else", "text/html")
    with pytest.raises(ObjectExistsError):  # retried by Dramatiq
        _persist(database, capture_redis, object_store, root, job.job_id)
    assert (root / str(job.job_id)).exists()
    assert capture_redis.get(PENDING_KEY.format(case_id=case_id)) is not None


def test_a_challenge_sends_the_case_to_review(
    database: Database, capture_redis: redis.Redis, object_store: ObjectStore, root: Path
) -> None:
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-challenge.test")
    job, _ = _issue(database, capture_redis, case_id)
    challenge = _result(
        Profile.DESKTOP,
        Outcome.CHALLENGE,
        http_status=403,
        title="Just a moment...",
        challenge="cf-mitigated: challenge",
    )
    _write(root, job.job_id, [challenge, _result(Profile.MOBILE, Outcome.CHALLENGE)])

    result = _persist(database, capture_redis, object_store, root, job.job_id)

    assert result.status is PersistStatus.CHALLENGE
    assert not result.compute_signals
    assert _state(database, case_id) is CaseState.IN_REVIEW
    task = _open_task(database, case_id)
    assert task is not None
    assert task.reason == "challenge"
    types = [event.type for event in _events(database, case_id)]
    assert EventType.CAPTURED in types
    assert EventType.REVIEW_REQUESTED in types
    assert {scan.outcome for scan in _rows(database, Scan, case_id)} == {"challenge"}
    assert len(_rows(database, Artifact, case_id)) == 4


def test_the_browsers_last_failed_attempt_ends_in_review(
    database: Database, capture_redis: redis.Redis, object_store: ObjectStore, root: Path
) -> None:
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-loop.test")
    job, _ = _issue(database, capture_redis, case_id)
    failed = [
        _result(profile, Outcome.ERROR, files=(), error="net::ERR_TOO_MANY_REDIRECTS")
        for profile in (Profile.DESKTOP, Profile.MOBILE)
    ]
    _write(root, job.job_id, failed, final=True)

    result = _persist(database, capture_redis, object_store, root, job.job_id)

    assert result.status is PersistStatus.FAILED
    assert {scan.outcome for scan in _rows(database, Scan, case_id)} == {"error"}
    events = _events(database, case_id)
    [job_failed] = [event for event in events if event.type is EventType.JOB_FAILED]
    assert job_failed.data["job"] == "browser_capture"
    assert "ERR_TOO_MANY_REDIRECTS" in job_failed.data["error"]
    assert EventType.CAPTURED not in [event.type for event in events]
    assert _state(database, case_id) is CaseState.IN_REVIEW
    task = _open_task(database, case_id)
    assert task is not None
    assert task.reason == "capture_failed"
    assert capture_redis.get(PENDING_KEY.format(case_id=case_id)) is None


def test_one_failed_profile_still_counts_as_a_capture(
    database: Database, capture_redis: redis.Redis, object_store: ObjectStore, root: Path
) -> None:
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-mobile.test")
    job, _ = _issue(database, capture_redis, case_id)
    # The browser reported only the desktop profile; mobile gets an error scan.
    _write(root, job.job_id, [_result(Profile.DESKTOP)], error="mobile context crashed")
    result = _persist(database, capture_redis, object_store, root, job.job_id)
    assert result.status is PersistStatus.STORED
    scans = {scan.profile: scan for scan in _rows(database, Scan, case_id)}
    assert scans[Profile.MOBILE].outcome == "error"
    assert scans[Profile.MOBILE].error == "mobile context crashed"


# --- persist_capture: hostile or stale input --------------------------------------------------


def _symlinked_screenshot(folder: Path) -> None:
    target = folder.parent / "worker-env"
    target.write_text("DATABASE_URL_WORKER=postgresql://secret")
    name = folder / file_name(ArtifactKind.SCREENSHOT, Profile.DESKTOP)
    name.unlink()
    name.symlink_to(target)


def _oversized_html(folder: Path) -> None:
    with (folder / file_name(ArtifactKind.HTML, Profile.MOBILE)).open("wb") as html:
        html.truncate(FILES[ArtifactKind.HTML].max_bytes + 1)


def _not_a_png(folder: Path) -> None:
    (folder / file_name(ArtifactKind.SCREENSHOT, Profile.MOBILE)).write_bytes(b"GIF89a")


def _foreign_meta(folder: Path) -> None:
    meta = CaptureMeta.model_validate_json((folder / META_FILE).read_bytes())
    forged = meta.model_copy(update={"job_id": uuid.uuid4()})
    (folder / META_FILE).write_text(forged.model_dump_json())


def _garbage_meta(folder: Path) -> None:
    (folder / META_FILE).write_text('{"job_id": "' + "x" * 50 + '", "profiles": 7}')


def _missing_file(folder: Path) -> None:
    (folder / file_name(ArtifactKind.HTML, Profile.DESKTOP)).unlink()


def _linked_folder(folder: Path) -> None:
    moved = folder.parent / "elsewhere"
    folder.rename(moved)
    folder.symlink_to(moved)


def _fifo_meta(folder: Path) -> None:
    (folder / META_FILE).unlink()
    os.mkfifo(folder / META_FILE)


@pytest.mark.parametrize(
    "spoil",
    [
        _symlinked_screenshot,
        _oversized_html,
        _not_a_png,
        _foreign_meta,
        _garbage_meta,
        _missing_file,
        _linked_folder,
        _fifo_meta,
    ],
)
def test_an_invalid_job_folder_is_not_stored_and_not_retried(
    database: Database,
    capture_redis: redis.Redis,
    object_store: ObjectStore,
    root: Path,
    spoil: Callable[[Path], None],
) -> None:
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-login.test")
    job, record = _issue(database, capture_redis, case_id)
    folder = _write(root, job.job_id, [_result(Profile.DESKTOP), _result(Profile.MOBILE)])
    spoil(folder)

    result = _persist(database, capture_redis, object_store, root, job.job_id)

    assert result.status is PersistStatus.INVALID
    assert _rows(database, Scan, case_id) == []
    assert _rows(database, Artifact, case_id) == []
    [failed] = [e for e in _events(database, case_id) if e.type is EventType.JOB_FAILED]
    assert failed.data["job"] == "persist_capture"
    assert failed.data["error"].startswith("invalid capture:")
    task = _open_task(database, case_id)
    assert task is not None
    assert task.reason == "capture_failed"
    assert not os.path.lexists(folder)
    assert capture_redis.get(PENDING_KEY.format(case_id=case_id)) is None
    # Nothing was uploaded before the check failed.
    assert not list(object_store._client.list_blobs(BUCKET, prefix=f"cases/{record.case_id}/"))


def test_a_job_without_a_record_is_dropped(
    database: Database, capture_redis: redis.Redis, object_store: ObjectStore, root: Path
) -> None:
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-login.test")
    forged = uuid.uuid4()
    folder = _write(root, forged, [_result(Profile.DESKTOP), _result(Profile.MOBILE)])
    result = _persist(database, capture_redis, object_store, root, forged)
    assert result.status is PersistStatus.UNKNOWN
    assert not folder.exists()
    assert _rows(database, Scan, case_id) == []
    assert _state(database, case_id) is CaseState.DETECTED


def test_a_case_that_left_detected_gets_nothing(
    database: Database, capture_redis: redis.Redis, object_store: ObjectStore, root: Path
) -> None:
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-login.test")
    job, _ = _issue(database, capture_redis, case_id)
    with database.worker_session() as session:
        case = session.get_one(Case, case_id)
        transition(session, case, CaseState.DISMISSED, actor="ops@test", reason="ours")
    folder = _write(root, job.job_id, [_result(Profile.DESKTOP), _result(Profile.MOBILE)])
    result = _persist(database, capture_redis, object_store, root, job.job_id)
    assert result.status is PersistStatus.SKIPPED
    assert _rows(database, Scan, case_id) == []
    assert not folder.exists()
    assert capture_redis.get(PENDING_KEY.format(case_id=case_id)) is None


# --- the actor --------------------------------------------------------------------------------


@pytest.fixture
def persist_actor(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    capture_redis: redis.Redis,
    object_store: ObjectStore,
    root: Path,
) -> list[tuple[str, str]]:
    from kalkan.workers import discovery
    from kalkan.workers import persist as worker

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(worker, "get_database", lambda: database)
    monkeypatch.setattr(worker, "capture_redis", lambda: capture_redis)
    monkeypatch.setattr(worker, "evidence_store", lambda: (object_store, BUCKET))
    monkeypatch.setattr(worker, "ROOT", root)
    monkeypatch.setattr(worker.compute_signals, "send", lambda c: sent.append(("signals", c)))
    monkeypatch.setattr(worker.hash_brand_assets, "send", lambda b: sent.append(("hash", b)))
    monkeypatch.setattr(discovery.admit_match, "send", lambda m: sent.append(("admit", m)))
    return sent


def test_the_actor_queues_signals_and_new_candidates(
    database: Database,
    capture_redis: redis.Redis,
    root: Path,
    persist_actor: list[tuple[str, str]],
) -> None:
    from kalkan.workers.persist import persist_capture

    brand = _brand(database)
    case_id = _case(database, brand, "mintera-login.test")
    job, _ = _issue(database, capture_redis, case_id)
    _write(root, job.job_id, [_result(Profile.DESKTOP, **CLONE_DESKTOP), _result(Profile.MOBILE)])
    persist_capture.fn(str(job.job_id))
    assert persist_actor[0] == ("signals", str(case_id))
    assert [kind for kind, _ in persist_actor] == ["signals", "admit", "admit"]
    persist_capture.fn("../../etc/passwd")  # not a job id: dropped quietly


def test_the_actors_last_failed_attempt_ends_in_review(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    capture_redis: redis.Redis,
    root: Path,
    persist_actor: list[tuple[str, str]],
) -> None:
    from kalkan.workers import persist as worker

    def broken(*args: object, **kwargs: object) -> PersistResult:
        raise ConnectionError("storage unavailable")

    monkeypatch.setattr(worker.persist, "persist_capture", broken)
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-login.test")
    job, _ = _issue(database, capture_redis, case_id)
    folder = _write(root, job.job_id, [_result(Profile.DESKTOP), _result(Profile.MOBILE)])
    monkeypatch.setattr(worker, "final_attempt", lambda retries: False)
    with pytest.raises(ConnectionError):
        worker.persist_capture.fn(str(job.job_id))  # retried by Dramatiq
    assert _state(database, case_id) is CaseState.DETECTED
    monkeypatch.setattr(worker, "final_attempt", lambda retries: True)
    worker.persist_capture.fn(str(job.job_id))
    [failed] = [e for e in _events(database, case_id) if e.type is EventType.JOB_FAILED]
    assert failed.data["job"] == "persist_capture"
    assert "storage unavailable" in failed.data["error"]
    task = _open_task(database, case_id)
    assert task is not None
    assert task.reason == "capture_failed"
    assert not folder.exists()
    assert capture_redis.get(PENDING_KEY.format(case_id=case_id)) is None
    assert persist_actor == []


# --- issuing case jobs ------------------------------------------------------------------------


def test_case_jobs_start_where_the_precheck_got_an_answer(
    database: Database, capture_redis: redis.Redis
) -> None:
    brand = _brand(database)
    case_id = _case(database, brand, "mintera-login.test")
    with database.worker_session() as session:
        session.add(
            Scan(
                customer_id=brand.customer_id,
                case_id=case_id,
                stage=ScanStage.HTTP,
                started_at=NOW,
                final_url="http://mintera-login.test/home",
                redirect_chain=["http://mintera-login.test/"],
            )
        )
    job, record = _issue(database, capture_redis, case_id)
    assert job.urls[:3] == [
        "http://mintera-login.test/",
        "https://mintera-login.test/",
        "https://www.mintera-login.test/",
    ]
    assert job.country == "PL"
    assert job.purpose is Purpose.CASE
    assert (record.customer_id, record.brand_id, record.case_id) == (
        brand.customer_id,
        brand.id,
        case_id,
    )
    # One live job per case.
    assert issue_case_capture(database, capture_redis, case_id, now=NOW, send=_never) is None
    # Only detected cases of real customers get one.
    demo = _brand(database, "demo", is_demo=True)
    demo_case = _demo_case(database, demo)
    assert issue_case_capture(database, capture_redis, demo_case, now=NOW, send=_never) is None
    assert issue_case_capture(database, capture_redis, uuid.uuid4(), now=NOW) is None


# --- reference screenshots --------------------------------------------------------------------


def test_reference_screenshots_become_brand_assets(
    database: Database, capture_redis: redis.Redis, object_store: ObjectStore, root: Path
) -> None:
    brand = _brand(database)
    sent: list[CaptureJob] = []
    job, domain = issue_reference_capture(
        database, capture_redis, brand.id, now=NOW, send=sent.append
    )
    assert domain == "mintera.test"
    assert sent == [job]
    assert job.urls == ["https://mintera.test/", "http://mintera.test/"]
    assert job.purpose is Purpose.REFERENCE
    assert not list(capture_redis.scan_iter("capture:pending:*"))

    official: dict[str, Any] = {"final_url": "https://mintera.test/", "title": "Mintera Bank"}
    results = [
        _result(Profile.DESKTOP, **official),
        _result(Profile.MOBILE, Outcome.CHALLENGE, **official),
    ]
    folder = _write(root, job.job_id, results)
    result = _persist(database, capture_redis, object_store, root, job.job_id)
    assert result.status is PersistStatus.REFERENCE
    assert result.hashed_brand_id == brand.id  # hash_brand_assets comes next
    assert not folder.exists()

    with database.worker_session() as session:
        assets = list(session.scalars(select(BrandAsset).where(BrandAsset.brand_id == brand.id)))
    [reference] = [a for a in assets if a.kind is not BrandAssetKind.FAVICON]
    assert reference.kind is BrandAssetKind.REFERENCE_DESKTOP  # a challenge is no reference
    expected = f"gs://{BUCKET}/brands/{brand.id}/refs/20261102T090000Z/desktop.png"
    assert reference.storage_key == expected
    data = CONTENT[ArtifactKind.SCREENSHOT](Profile.DESKTOP)
    assert reference.sha256 == sha256_hex(data) == object_store.stored_sha256(expected)
    assert reference.source_url == "https://mintera.test/"
    assert reference.text_excerpt == "Mintera Bank login. Deposit and sign in."
    assert (reference.phash, reference.dhash) == (None, None)

    _write(root, job.job_id, results)  # redelivered: nothing new
    again = _persist(database, capture_redis, object_store, root, job.job_id)
    assert again.hashed_brand_id is None
    with database.worker_session() as session:
        count = session.scalar(
            select(func.count()).where(
                BrandAsset.brand_id == brand.id,
                BrandAsset.kind == BrandAssetKind.REFERENCE_DESKTOP,
            )
        )
    assert count == 1


def test_stored_references_queue_their_hashes(
    database: Database,
    capture_redis: redis.Redis,
    root: Path,
    persist_actor: list[tuple[str, str]],
) -> None:
    from kalkan.workers.persist import persist_capture

    brand = _brand(database)
    job, _ = issue_reference_capture(database, capture_redis, brand.id, now=NOW, send=_never_ref)
    official: dict[str, Any] = {"final_url": "https://mintera.test/", "title": "Mintera Bank"}
    _write(root, job.job_id, [_result(Profile.DESKTOP, **official)])
    persist_capture.fn(str(job.job_id))
    assert persist_actor == [("hash", str(brand.id))]


def _never_ref(job: CaptureJob) -> None:
    """Reference jobs are sent by the caller; the test only needs the record."""


def test_only_real_brands_with_a_primary_domain_get_references(
    monkeypatch: pytest.MonkeyPatch, database: Database, capture_redis: redis.Redis
) -> None:
    from kalkan.workers import scanning

    brand = _brand(database)
    demo = _brand(database, "demo", is_demo=True)
    with database.worker_session() as session:
        owner = factories.customer(session, "mirrors")
        mirrors_only = factories.brand(session, owner)
        admin.add_official_domain(
            session, mirrors_only, "mintera-m.test", actor="test", kind=OfficialDomainKind.MIRROR
        )
        sleeping = factories.brand(session, owner)
        sleeping.active = False
        admin.add_official_domain(session, sleeping, "mintera-s.test", actor="test")
    for brand_id, reason in (
        (demo.id, "demo"),
        (mirrors_only.id, "no primary"),
        (sleeping.id, "inactive"),
        (uuid.uuid4(), "no brand"),
    ):
        with pytest.raises(NoReferenceError, match=reason):
            issue_reference_capture(database, capture_redis, brand_id, now=NOW, send=_never)

    # The weekly job fans out to the brands that qualify.
    sent: list[str] = []
    monkeypatch.setattr(scanning, "get_database", lambda: database)
    monkeypatch.setattr(scanning.refresh_brand_references, "send", sent.append)
    scanning.refresh_brand_references.fn()
    assert sent == [str(brand.id)]

    issued: list[uuid.UUID] = []
    monkeypatch.setattr(scanning, "capture_redis", lambda: capture_redis)

    def issue(database: Database, client: Any, brand_id: uuid.UUID, now: datetime) -> Any:
        issued.append(brand_id)
        return issue_reference_capture(database, client, brand_id, now=now, send=lambda job: None)

    monkeypatch.setattr(scanning, "issue_reference_capture", issue)
    scanning.refresh_brand_references.fn(str(brand.id))
    scanning.refresh_brand_references.fn(str(demo.id))  # skipped with a log line
    assert issued == [brand.id, demo.id]


def test_a_primary_official_domain_queues_references(database: Database) -> None:
    brand = _brand(database)
    with database.worker_session() as session:
        current = session.get_one(Brand, brand.id)
        admin.add_official_domain(
            session, current, "mintera-old.test", actor="test", kind=OfficialDomainKind.LEGACY
        )
        payloads = list(
            session.scalars(
                select(JobOutbox.payload).where(JobOutbox.kind == REFRESH_BRAND_REFERENCES)
            )
        )
    assert payloads == [{"brand_id": str(brand.id)}]

    sent: list[tuple[str, dict[str, str]]] = []
    outbox.dispatch(database, lambda actor, **kw: sent.append((actor, kw)), lambda: NOW)
    assert ("refresh_brand_references", {"brand_id": str(brand.id)}) in sent


def test_cli_brand_references(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    test_settings: Settings,
    capture_redis: redis.Redis,
) -> None:
    from typer.testing import CliRunner

    from kalkan import cli

    enqueued: list[dramatiq.Message[Any]] = []

    class FakeBroker:
        def enqueue(self, message: dramatiq.Message[Any]) -> None:
            enqueued.append(message)

    monkeypatch.setattr(cli, "get_database", lambda: database)
    settings = test_settings.model_copy(update={"redis_url": redis_url_for_tests()})
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(dramatiq, "get_broker", FakeBroker)
    brand = _brand(database)
    result = CliRunner().invoke(cli.app, ["brand", "references", "--brand", str(brand.id)])
    assert result.exit_code == 0, result.output
    assert "of mintera.test" in result.output
    [message] = enqueued
    job = CaptureJob.model_validate(message.args[0])
    assert load_record(capture_redis, job.job_id) is not None
    missing = CliRunner().invoke(cli.app, ["brand", "references", "--brand", str(uuid.uuid4())])
    assert missing.exit_code == 1
    assert "no brand with id" in missing.output


# --- the reconciler ---------------------------------------------------------------------------


def _prechecked(
    database: Database, brand: Brand, case_id: uuid.UUID, at: datetime, route: str
) -> None:
    with database.worker_session() as session:
        session.add(
            Scan(
                customer_id=brand.customer_id, case_id=case_id, stage=ScanStage.HTTP, started_at=at
            )
        )
        session.add(
            CaseEvent(
                customer_id=brand.customer_id,
                case_id=case_id,
                at=at,
                type=EventType.PRECHECKED,
                actor="system",
                data={"route": route},
            )
        )


def test_the_reconciler_issues_lost_capture_jobs(
    database: Database, capture_redis: redis.Redis
) -> None:
    brand = _brand(database)
    entered = NOW - timedelta(hours=6)
    hosts = (
        "lost",
        "recent",
        "captured",
        "rerouted",
        "errored",
        "failed",
        "pending",
        "reviewed",
    )
    cases = {name: _case(database, brand, f"mintera-{name}.test", entered) for name in hosts}
    for name, case_id in cases.items():
        at = NOW - (timedelta(minutes=30) if name == "recent" else timedelta(hours=3))
        _prechecked(database, brand, case_id, at, "capture")
    _prechecked(database, brand, cases["rerouted"], NOW - timedelta(hours=2, minutes=30), "watch")
    with database.worker_session() as session:
        for name, outcome in (("captured", "ok"), ("errored", "error")):
            session.add(
                Scan(
                    customer_id=brand.customer_id,
                    case_id=cases[name],
                    stage=ScanStage.BROWSER,
                    outcome=outcome,
                    started_at=NOW - timedelta(hours=2),
                )
            )
        session.add(
            CaseEvent(
                customer_id=brand.customer_id,
                case_id=cases["failed"],
                at=NOW - timedelta(hours=1),
                type=EventType.JOB_FAILED,
                actor="system",
                data={"job": "browser_capture"},
            )
        )
        session.add(
            ReviewTask(
                customer_id=brand.customer_id,
                case_id=cases["reviewed"],
                kind=TaskKind.REVIEW,
                status=TaskStatus.OPEN,
            )
        )
    demo = _brand(database, "demo", is_demo=True)
    demo_case = _demo_case(database, demo, entered)
    _prechecked(database, demo, demo_case, NOW - timedelta(hours=3), "capture")
    capture_redis.set(PENDING_KEY.format(case_id=cases["pending"]), "an earlier job")

    jobs: list[CaptureJob] = []

    def capture(case_id: uuid.UUID) -> CaptureJob | None:
        return issue_case_capture(database, capture_redis, case_id, now=NOW, send=jobs.append)

    result = reconciler.reconcile(database, lambda *a, **k: None, NOW, capture=capture)

    assert result.captures == 2
    issued = set()
    for job in jobs:
        record = load_record(capture_redis, job.job_id)
        assert record is not None
        issued.add(record.case_id)
    assert issued == {cases["lost"], cases["errored"]}
    # Issued jobs are pending now: the next run issues nothing.
    again = reconciler.reconcile(database, lambda *a, **k: None, NOW, capture=capture)
    assert again.captures == 0
