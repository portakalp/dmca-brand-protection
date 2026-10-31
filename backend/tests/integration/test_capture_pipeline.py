"""The browser stage end to end (architecture.md §7, §10, §11).

Each test runs the real path: the pre-check (``http_precheck``) or another issuer records and
sends a capture job; a Dramatiq worker on the test broker runs ``browser_capture`` in-process
with a real Chromium that reaches the fixture sites only through the local dev-proxy
(127.0.0.1:18888); the browser hands its job folder to ``persist_capture``, which stores the
files in fake-gcs and the rows in the test database. One worker thread consumes the
``precheck``, ``capture`` and ``persist`` queues, standing in for worker-default and
worker-browser. Only the process wiring is replaced (database, Redis, bucket, hand-off folder,
settings), and Dramatiq's retry back-off is cut to 0.1 s; a test that scales a time limit says
so.

Acceptance criterion 4 (no cloud credentials or database URL in the browser container) is
checked on the compose files by ``tests/unit/test_compose_security.py``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import ipaddress
import json
import socket
import struct
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import dramatiq
import prometheus_client
import pytest
import redis
from dramatiq import Worker
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import TimeLimit
from sqlalchemy import select

from kalkan import admin
from kalkan.config import Settings
from kalkan.db.base import utcnow
from kalkan.db.models import Artifact, Brand, BrandAsset, Case, CaseEvent, Domain, ReviewTask, Scan
from kalkan.db.models import Signal as SignalRow
from kalkan.db.session import Database
from kalkan.discovery.matcher import MatchResult
from kalkan.discovery.normalize import view_host
from kalkan.discovery.record import Sighting, record_discovery
from kalkan.domain.enums import (
    ArtifactKind,
    BrandAssetKind,
    CaseState,
    EventType,
    MatchReason,
    Profile,
    ScanStage,
    SightingSource,
    TaskKind,
    TaskStatus,
)
from kalkan.enrichment.admission import admit
from kalkan.net.safe_http import NoSuchHostError, SafeHttpClient
from kalkan.scanning import capture
from kalkan.scanning.capture import BrowserRuntime
from kalkan.scanning.capture_contract import (
    CAPTURE_QUEUE,
    FILES,
    PENDING_KEY,
    PERSIST_QUEUE,
    SIGNALS_QUEUE,
    CaptureJob,
)
from kalkan.scanning.capture_jobs import CAPTURE_ACTOR, issue_case_capture, load_record
from kalkan.scheduler import outbox
from kalkan.scheduler.__main__ import send_message
from kalkan.storage.gcs import ObjectStore
from kalkan.workers import capture as browser_worker
from kalkan.workers import persist as persist_worker
from kalkan.workers import scanning as scanning_worker
from tests import factories
from tests.integration.test_browser import HANG, LocalProxy, html_page
from tests.support import make_settings, redis_url_for_tests

DEV_PROXY = ("127.0.0.1", 18888)
DEV_PROXY_URL = "http://127.0.0.1:18888"
FIXTURE_PORT = 18080
FIXTURE_HOSTS = {
    "mintera.test",
    "www.mintera.test",
    "mintera-login.test",
    "mintera-mobile.test",
    "mintera-challenge.test",
    "mintera-loop.test",
    "mintera-heavy.test",
}
BUCKET = "kalkan-test-evidence"
HINT = (
    "start the services with `make up-infra` (fixture sites, dev-proxy on 127.0.0.1:18888) and "
    "install Chromium with `uv run playwright install chromium`"
)
# worker-default's pre-check and persist queues, worker-browser's capture queue.
QUEUES = ("precheck", CAPTURE_QUEUE, PERSIST_QUEUE)
JOIN_MS = 120_000
PNG = b"\x89PNG\r\n\x1a\n"
SIZES = {Profile.DESKTOP: (1280, 800), Profile.MOBILE: (390, 844)}
REFERENCE = {
    Profile.DESKTOP: BrandAssetKind.REFERENCE_DESKTOP,
    Profile.MOBILE: BrandAssetKind.REFERENCE_MOBILE,
}


# --- the pipeline --------------------------------------------------------------------------------


@dataclass
class Pipeline:
    broker: RedisBroker
    database: Database
    redis: redis.Redis
    store: ObjectStore
    root: Path
    settings: Settings  # what worker-browser sees (PROXY_MAP)
    attempts: list[tuple[float, float]] = field(default_factory=list)  # run_job start, end
    launches: int = 0  # Chromium launches
    contexts: int = 0  # browser contexts (one per profile and attempt)
    kills: int = 0  # BrowserRuntime.kill calls

    def run(self, *, until: Callable[[], bool] | None = None) -> None:
        """Work the queues until they are empty (or ``until()`` holds), then stop."""
        worker = Worker(self.broker, queues=set(QUEUES), worker_threads=1)
        worker.start()
        try:
            if until is None:
                for queue in QUEUES:
                    self.broker.join(queue, timeout=JOIN_MS)
                return
            deadline = time.monotonic() + JOIN_MS / 1000
            while not until():
                assert time.monotonic() < deadline, "the pipeline did not settle"
                time.sleep(0.2)
        finally:
            worker.stop(timeout=10_000)

    def messages(self, queue: str) -> list[dict[str, Any]]:
        return [json.loads(raw) for raw in self.broker.client.hvals(f"dramatiq:{queue}.msgs")]


def _flush_capture_keys(client: redis.Redis) -> None:
    for key in client.scan_iter("capture:*"):
        client.delete(key)


@pytest.fixture(scope="module")
def http() -> Iterator[SafeHttpClient]:
    """Stage 1 reaches the fixture sites directly (*.test is 127.0.0.1, port 80 is 18080)."""
    try:
        socket.create_connection(("127.0.0.1", FIXTURE_PORT), timeout=2).close()
    except OSError:
        pytest.fail(f"the fixture sites are not running; {HINT}")

    def resolve(host: str) -> list[str]:
        if host in FIXTURE_HOSTS:
            return ["127.0.0.1"]
        raise NoSuchHostError(host)

    with SafeHttpClient(
        allow_cidrs=[ipaddress.ip_network("127.0.0.1/32")],
        resolver=resolve,
        port_map={80: FIXTURE_PORT, 443: FIXTURE_PORT + 363},  # nothing listens on 18443
    ) as client:
        yield client


@pytest.fixture(scope="module")
def chromium() -> None:
    try:
        socket.create_connection(DEV_PROXY, timeout=2).close()
    except OSError:
        pytest.fail(f"dev-proxy is not reachable at 127.0.0.1:18888; {HINT}")
    runtime = BrowserRuntime()
    try:
        runtime.browser()
    except Exception as exc:  # noqa: BLE001 - reported with the setup hint
        pytest.fail(f"Chromium does not start ({type(exc).__name__}); {HINT}")
    finally:
        runtime.close()


@pytest.fixture
def pipeline(
    monkeypatch: pytest.MonkeyPatch,
    chromium: None,
    http: SafeHttpClient,
    database: Database,
    test_settings: Settings,
    object_store: ObjectStore,
    redis_broker: RedisBroker,
    tmp_path: Path,
) -> Iterator[Pipeline]:
    client = redis.Redis.from_url(redis_url_for_tests())
    _flush_capture_keys(client)
    day = browser_worker.day_key(utcnow())  # the browser's proxy byte counter
    proxy_bytes = int(client.get(day) or 0)
    root = tmp_path / "artifacts-tmp"
    root.mkdir()
    line = Pipeline(
        redis_broker,
        database,
        client,
        object_store,
        root,
        make_settings(env="local", proxy_map={"PL": DEV_PROXY_URL}),
    )
    # worker-browser: settings with PROXY_MAP only, the shared hand-off folder.
    monkeypatch.setattr(browser_worker, "get_settings", lambda: line.settings)
    monkeypatch.setattr(browser_worker, "ROOT", root)
    # worker-default: the test database, Redis (db 15) and bucket, the same folder.
    monkeypatch.setattr(persist_worker, "ROOT", root)
    monkeypatch.setattr(persist_worker, "get_database", lambda: database)
    monkeypatch.setattr(persist_worker, "capture_redis", lambda: client)
    monkeypatch.setattr(persist_worker, "evidence_store", lambda: (object_store, BUCKET))
    monkeypatch.setattr(scanning_worker, "get_database", lambda: database)
    monkeypatch.setattr(scanning_worker, "get_settings", lambda: test_settings)
    monkeypatch.setattr(scanning_worker, "http_client", lambda: http)
    monkeypatch.setattr(scanning_worker, "capture_redis", lambda: client)
    # The one retry follows after 0.1-0.2 s instead of Dramatiq's 15 s default back-off.
    monkeypatch.setitem(browser_worker.browser_capture.options, "min_backoff", 100)
    monkeypatch.setitem(browser_worker.browser_capture.options, "max_backoff", 200)

    # The worker thread launches this runtime's Chromium; it is killed at the end.
    runtime = BrowserRuntime()
    monkeypatch.setattr(capture, "runtime", lambda: runtime)
    real_run, real_profile = capture.run_job, capture.capture_profile
    real_launch, real_kill = BrowserRuntime._launch, BrowserRuntime.kill

    def run_job(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            return real_run(*args, **kwargs)
        finally:
            line.attempts.append((started, time.monotonic()))

    def capture_profile(*args: Any, **kwargs: Any) -> Any:
        line.contexts += 1
        return real_profile(*args, **kwargs)

    def launch(self: BrowserRuntime) -> Any:
        line.launches += 1
        return real_launch(self)

    def kill(self: BrowserRuntime, *, driver: bool = False) -> None:
        line.kills += 1
        real_kill(self, driver=driver)

    monkeypatch.setattr(capture, "run_job", run_job)
    monkeypatch.setattr(capture, "capture_profile", capture_profile)
    monkeypatch.setattr(BrowserRuntime, "_launch", launch)
    monkeypatch.setattr(BrowserRuntime, "kill", kill)
    yield line
    real_kill(runtime, driver=True)
    _flush_capture_keys(client)
    client.decrby(day, max(0, int(client.get(day) or 0) - proxy_bytes))
    client.close()


@pytest.fixture
def time_limits(redis_broker: RedisBroker) -> None:
    """Dramatiq's time limits are enforced by a thread its CLI starts at process boot, which a
    test worker skips: it is started here (once per test session; it stays idle otherwise)."""
    for middleware in redis_broker.middleware:
        manager = getattr(middleware, "manager", None)
        if (
            isinstance(middleware, TimeLimit)
            and isinstance(manager, threading.Thread)
            and not manager.is_alive()
        ):
            manager.start()


# --- rows, objects and files ---------------------------------------------------------------------


def _brand(database: Database, *, markets: tuple[str, ...] = ("PL",)) -> Brand:
    with database.worker_session() as session:
        owner = factories.customer(session, "mint")
        brand = factories.brand(session, owner)
        brand.target_markets = list(markets)
        admin.add_official_domain(session, brand, "mintera.test", actor="test")
        return brand


def _case(database: Database, brand: Brand, host: str, *, age_days: int | None = None) -> uuid.UUID:
    now = utcnow()
    with database.worker_session() as session:
        recorded = record_discovery(
            session,
            view_host(host),
            [MatchResult(brand.id, brand.customer_id, 90, (MatchReason.KEYWORD,))],
            Sighting(SightingSource.CT, now),
        )
        if age_days is not None:
            domain = session.get_one(Domain, recorded.domain_id)
            domain.registered_at = now - timedelta(days=age_days)
        result = admit(session, recorded.new_match_ids[0], now=now, prefix="KLK")
    assert result.case_id is not None
    return result.case_id


def _state(database: Database, case_id: uuid.UUID) -> CaseState:
    with database.worker_session() as session:
        return session.get_one(Case, case_id).state


def _scans(database: Database, case_id: uuid.UUID) -> dict[Profile | None, Scan]:
    with database.worker_session() as session:
        scans = session.scalars(
            select(Scan).where(Scan.case_id == case_id, Scan.stage == ScanStage.BROWSER)
        ).all()
    assert len({scan.profile for scan in scans}) == len(scans)
    return {scan.profile: scan for scan in scans}


Stored = dict[tuple[Profile, ArtifactKind], Artifact]


def _artifacts(database: Database, case_id: uuid.UUID) -> Stored:
    with database.worker_session() as session:
        artifacts = session.scalars(select(Artifact).where(Artifact.case_id == case_id)).all()
    found = {(artifact.profile, artifact.kind): artifact for artifact in artifacts}
    assert len(found) == len(artifacts)
    return {(profile, kind): row for (profile, kind), row in found.items() if profile is not None}


def _events(database: Database, case_id: uuid.UUID, kind: EventType) -> list[CaseEvent]:
    with database.worker_session() as session:
        return list(
            session.scalars(
                select(CaseEvent)
                .where(CaseEvent.case_id == case_id, CaseEvent.type == kind)
                .order_by(CaseEvent.at)
            )
        )


def _open_review(database: Database, case_id: uuid.UUID) -> ReviewTask | None:
    with database.worker_session() as session:
        return session.scalar(
            select(ReviewTask).where(
                ReviewTask.case_id == case_id,
                ReviewTask.kind == TaskKind.REVIEW,
                ReviewTask.status == TaskStatus.OPEN,
            )
        )


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == PNG
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _stored(store: ObjectStore, key: str, sha256: str) -> bytes:
    """The object's bytes, after checking them against the recorded hash and its metadata."""
    data = store.read_bytes(key)
    assert hashlib.sha256(data).hexdigest() == sha256
    assert store.stored_sha256(key) == sha256
    return data


def _kinds(scan: Scan) -> set[str]:
    return {item["kind"] for form in scan.forms for item in form["inputs"]}


def _deferrals() -> float:
    sample = prometheus_client.REGISTRY.get_sample_value(
        "capture_deferrals_total", {"reason": "no_proxy"}
    )
    return sample or 0.0


def _prechecked_to_capture(line: Pipeline, case_id: uuid.UUID) -> None:
    [prechecked] = _events(line.database, case_id, EventType.PRECHECKED)
    assert prechecked.data["route"] == "capture"


# --- acceptance 1 and 2: the clone ---------------------------------------------------------------


def test_a_clone_is_captured_with_both_profiles_and_stored_with_its_hashes(
    pipeline: Pipeline,
) -> None:
    brand = _brand(pipeline.database)
    case_id = _case(pipeline.database, brand, "mintera-login.test")
    day = browser_worker.day_key(utcnow())
    proxy_bytes = int(pipeline.redis.get(day) or 0)

    scanning_worker.http_precheck.send(str(case_id))
    pipeline.run()

    _prechecked_to_capture(pipeline, case_id)
    assert len(pipeline.attempts) == 1
    scans = _scans(pipeline.database, case_id)
    assert set(scans) == set(SIZES)
    for scan in scans.values():
        assert (scan.outcome, scan.http_status, scan.proxy_country) == ("ok", 200, "PL")
        assert scan.final_url == "http://mintera-login.test/"
        assert scan.title == "Mintera Bank | Login"
        # Acceptance 2: the form inventory finds the password and the card field.
        assert {"password", "card_number"} <= _kinds(scan)
        [form] = scan.forms
        assert form["action"] == "http://mintera-login.test/collect"
        assert [button["intent"] for button in form["buttons"]] == ["login"]

    # Acceptance 1: both screenshots in their sizes, HTML and HAR in fake-gcs, hashes match.
    artifacts = _artifacts(pipeline.database, case_id)
    assert set(artifacts) == {(profile, kind) for profile in SIZES for kind in FILES}
    for (profile, kind), artifact in artifacts.items():
        scan = scans[profile]
        assert artifact.scan_id == scan.id
        spec = FILES[kind]
        path = f"cases/{case_id}/{scan.id}/{kind.value}-{profile.value}.{spec.extension}"
        assert artifact.storage_key == f"gs://{BUCKET}/{path}"
        data = _stored(pipeline.store, artifact.storage_key, artifact.sha256)
        assert (artifact.bytes, artifact.content_type) == (len(data), spec.content_type)
        if kind is ArtifactKind.SCREENSHOT:
            assert _png_size(data) == SIZES[profile]
        elif kind is ArtifactKind.SCREENSHOT_FULL:
            width, height = _png_size(data)
            assert width == SIZES[profile][0]
            assert SIZES[profile][1] <= height <= 10_000
        elif kind is ArtifactKind.HTML:
            assert b"<title>Mintera Bank | Login</title>" in data
            assert b'type="password"' in data
        else:
            entries = json.loads(data)["log"]["entries"]
            [page] = [e for e in entries if e["request"]["url"] == "http://mintera-login.test/"]
            assert page["response"]["status"] == 200
            assert "text" not in page["response"]["content"]  # content: omit

    [captured] = _events(pipeline.database, case_id, EventType.CAPTURED)
    assert captured.data["outcomes"] == {"desktop": "ok", "mobile": "ok"}
    assert captured.data["proxy_country"] == "PL"
    with pipeline.database.worker_session() as session:
        cta = session.scalars(
            select(SignalRow.scan_id).where(
                SignalRow.case_id == case_id, SignalRow.name == "cta_targets"
            )
        ).all()
    assert {scan.id for scan in scans.values()} <= set(cta)
    # compute_signals is next; the case waits for it in detected.
    [signals] = pipeline.messages(SIGNALS_QUEUE)
    assert (signals["actor_name"], signals["args"]) == ("compute_signals", [str(case_id)])
    assert _state(pipeline.database, case_id) is CaseState.DETECTED
    assert pipeline.redis.get(PENDING_KEY.format(case_id=case_id)) is None
    assert list(pipeline.root.iterdir()) == []
    assert int(pipeline.redis.get(day) or 0) > proxy_bytes


# --- acceptance 3: a challenge -------------------------------------------------------------------


def test_a_challenge_is_stored_and_the_case_goes_to_review(pipeline: Pipeline) -> None:
    brand = _brand(pipeline.database)
    case_id = _case(pipeline.database, brand, "mintera-challenge.test")

    scanning_worker.http_precheck.send(str(case_id))
    pipeline.run()

    _prechecked_to_capture(pipeline, case_id)
    scans = _scans(pipeline.database, case_id)
    assert {(scan.outcome, scan.http_status) for scan in scans.values()} == {("challenge", 403)}
    artifacts = _artifacts(pipeline.database, case_id)
    for profile in SIZES:
        screenshot = artifacts[profile, ArtifactKind.SCREENSHOT]
        data = _stored(pipeline.store, screenshot.storage_key, screenshot.sha256)
        assert _png_size(data) == SIZES[profile]
    assert _state(pipeline.database, case_id) is CaseState.IN_REVIEW
    task = _open_review(pipeline.database, case_id)
    assert task is not None
    assert task.reason == "challenge"
    assert len(_events(pipeline.database, case_id, EventType.CAPTURED)) == 1
    assert _events(pipeline.database, case_id, EventType.JOB_FAILED) == []
    assert pipeline.messages(SIGNALS_QUEUE) == []  # a person looks at it first
    assert len(pipeline.attempts) == 1  # never retried, waited out or solved


# --- acceptance 5: no proxy ----------------------------------------------------------------------


def test_without_a_proxy_no_page_is_opened_and_the_job_waits_without_a_retry(
    pipeline: Pipeline,
) -> None:
    pipeline.settings = make_settings(env="local", proxy_map={})
    brand = _brand(pipeline.database)
    case_id = _case(pipeline.database, brand, "mintera-login.test")
    deferrals = _deferrals()
    client = pipeline.broker.client

    def deferred() -> bool:
        return (
            client.hlen("dramatiq:precheck.msgs") == 0
            and client.hlen(f"dramatiq:{CAPTURE_QUEUE}.msgs") == 0
            and client.hlen(f"dramatiq:{CAPTURE_QUEUE}.DQ.msgs") == 1
        )

    scanning_worker.http_precheck.send(str(case_id))
    pipeline.run(until=deferred)

    _prechecked_to_capture(pipeline, case_id)
    # No Chromium, no context, no page: nothing was opened, through a proxy or around one.
    assert (pipeline.attempts, pipeline.launches, pipeline.contexts) == ([], 0, 0)
    [message] = pipeline.messages(f"{CAPTURE_QUEUE}.DQ")
    assert message["actor_name"] == CAPTURE_ACTOR
    job = CaptureJob.model_validate(message["args"][0])
    assert "retries" not in message["options"]  # sent again as a new message
    assert 29 * 60 < message["options"]["eta"] / 1000 - time.time() <= 30 * 60
    assert _deferrals() == deferrals + 1
    # Still the case's live job: no capture_failed, no review, nothing stored.
    assert load_record(pipeline.redis, job.job_id) is not None
    assert pipeline.redis.get(PENDING_KEY.format(case_id=case_id)) == str(job.job_id).encode()
    assert _state(pipeline.database, case_id) is CaseState.DETECTED
    assert _events(pipeline.database, case_id, EventType.JOB_FAILED) == []
    assert _open_review(pipeline.database, case_id) is None
    assert _scans(pipeline.database, case_id) == {}
    assert pipeline.messages(PERSIST_QUEUE) == []
    assert list(pipeline.root.iterdir()) == []

    # Once a proxy is configured, the same job (its eta brought forward) captures the case.
    pipeline.settings = make_settings(env="local", proxy_map={"PL": DEV_PROXY_URL})
    [raw] = client.hvals(f"dramatiq:{CAPTURE_QUEUE}.DQ.msgs")
    assert isinstance(raw, bytes)
    later = dramatiq.Message.decode(raw)
    pipeline.broker.flush(CAPTURE_QUEUE)
    options = {key: value for key, value in later.options.items() if key != "eta"}
    pipeline.broker.enqueue(dataclasses.replace(later, queue_name=CAPTURE_QUEUE, options=options))
    pipeline.run()

    assert len(pipeline.attempts) == 1
    assert {scan.outcome for scan in _scans(pipeline.database, case_id).values()} == {"ok"}
    [captured] = _events(pipeline.database, case_id, EventType.CAPTURED)
    assert captured.data["job_id"] == str(job.job_id)


# --- acceptance 6: the time limit ----------------------------------------------------------------


@pytest.mark.usefixtures("time_limits")
@pytest.mark.parametrize("limit", ["watchdog", "dramatiq"])
def test_a_job_over_its_time_limit_is_killed_retried_once_then_reviewed(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    """The page locks its renderer up after ``load``: the capture never ends by itself.

    ``watchdog``: production's order, scaled down. The attempt budget is 6 s (100 s), the
    watchdog kills Chromium 1 s later (8 s) and the driver after 1 more second (5 s), and
    Dramatiq's hard limit of 10 s (120 s) stays behind them.
    ``dramatiq``: the last resort on its own. Dramatiq's limit is 1 s; it is raised when the
    blocked screenshot call returns (its timeout cut to 5 s from 15 s), long before the
    watchdog (68 s).
    """
    if limit == "watchdog":
        monkeypatch.setattr(capture, "JOB_BUDGET_SECONDS", 6.0)
        monkeypatch.setattr(capture, "KILL_GRACE_SECONDS", 1.0)
        monkeypatch.setattr(capture, "DRIVER_KILL_SECONDS", 1.0)
        time_limit, expected = 10_000, "the browser was stopped after the time budget"
    else:
        monkeypatch.setattr(capture, "SCREENSHOT_TIMEOUT_MS", 5_000)
        time_limit, expected = 1_000, "TimeLimitExceeded"
    brand = _brand(pipeline.database)
    case_id = _case(pipeline.database, brand, "mintera-hang.test")

    def send(job: CaptureJob) -> None:
        """The issuer's message, with the per-message time limit."""
        message: dramatiq.Message[None] = dramatiq.Message(
            queue_name=CAPTURE_QUEUE,
            actor_name=CAPTURE_ACTOR,
            args=(job.model_dump(mode="json"),),
            kwargs={},
            options={"time_limit": time_limit},
        )
        pipeline.broker.enqueue(message)

    # The residential proxy's place: an in-process proxy that serves the hanging page.
    with LocalProxy({"mintera-hang.test": html_page(HANG)}) as proxy:
        pipeline.settings = make_settings(env="local", proxy_map={"PL": proxy.url()})
        job = issue_case_capture(
            pipeline.database, pipeline.redis, case_id, now=utcnow(), send=send
        )
        assert job is not None
        pipeline.run()

    # Killed (the page never lets go by itself), retried once, then given up.
    assert len(pipeline.attempts) == 2
    assert pipeline.kills >= 2
    for started, ended in pipeline.attempts:
        assert ended - started < 20
    scans = _scans(pipeline.database, case_id)
    assert set(scans) == set(SIZES)
    assert {scan.outcome for scan in scans.values()} == {"error"}
    [failed] = _events(pipeline.database, case_id, EventType.JOB_FAILED)
    assert failed.data["job"] == "browser_capture"
    assert expected in failed.data["error"]
    assert _events(pipeline.database, case_id, EventType.CAPTURED) == []
    # The second failure sends the case to review.
    assert _state(pipeline.database, case_id) is CaseState.IN_REVIEW
    task = _open_review(pipeline.database, case_id)
    assert task is not None
    assert task.reason == "capture_failed"
    assert pipeline.redis.get(PENDING_KEY.format(case_id=case_id)) is None
    assert pipeline.messages(SIGNALS_QUEUE) == []
    assert list(pipeline.root.iterdir()) == []


# --- the other fixture sites ---------------------------------------------------------------------


def test_a_site_that_shows_the_clone_only_on_mobile_is_captured_on_both(
    pipeline: Pipeline,
) -> None:
    brand = _brand(pipeline.database)
    # A recipe blog to stage 1; young, so it still goes on to the browser stage.
    case_id = _case(pipeline.database, brand, "mintera-mobile.test", age_days=3)

    scanning_worker.http_precheck.send(str(case_id))
    pipeline.run()

    _prechecked_to_capture(pipeline, case_id)
    scans = _scans(pipeline.database, case_id)
    desktop, mobile = scans[Profile.DESKTOP], scans[Profile.MOBILE]
    assert (desktop.outcome, mobile.outcome) == ("ok", "ok")
    assert desktop.title == "Weekend recipes"
    assert not {"password", "phone"} & _kinds(desktop)
    assert mobile.title is not None
    assert "Mintera Bank" in mobile.title
    assert {"password", "phone"} <= _kinds(mobile)
    artifacts = _artifacts(pipeline.database, case_id)
    for profile in SIZES:
        screenshot = artifacts[profile, ArtifactKind.SCREENSHOT]
        data = _stored(pipeline.store, screenshot.storage_key, screenshot.sha256)
        assert _png_size(data) == SIZES[profile]
    assert len(_events(pipeline.database, case_id, EventType.CAPTURED)) == 1
    assert len(pipeline.messages(SIGNALS_QUEUE)) == 1


def test_an_endless_redirect_fails_twice_and_the_case_goes_to_review(pipeline: Pipeline) -> None:
    brand = _brand(pipeline.database)
    case_id = _case(pipeline.database, brand, "mintera-loop.test")
    # Stage 1 gives up on the loop and watches the site; a job still comes from the issuer the
    # reconciler uses, with every entry URL (https, http, then www.).
    job = issue_case_capture(pipeline.database, pipeline.redis, case_id, now=utcnow())
    assert job is not None
    assert len(job.urls) == 4

    pipeline.run()

    assert len(pipeline.attempts) == 2
    scans = _scans(pipeline.database, case_id)
    assert {scan.outcome for scan in scans.values()} == {"error"}
    for scan in scans.values():
        assert scan.error is not None
        assert "ERR_TOO_MANY_REDIRECTS" in scan.error
    # The redirects are the evidence: only the HARs are kept.
    assert {kind for _, kind in _artifacts(pipeline.database, case_id)} == {ArtifactKind.HAR}
    [failed] = _events(pipeline.database, case_id, EventType.JOB_FAILED)
    assert failed.data["job"] == "browser_capture"
    assert "ERR_TOO_MANY_REDIRECTS" in failed.data["error"]
    assert _state(pipeline.database, case_id) is CaseState.IN_REVIEW
    task = _open_review(pipeline.database, case_id)
    assert task is not None
    assert task.reason == "capture_failed"


def test_a_heavy_page_is_captured_within_its_budget(pipeline: Pipeline) -> None:
    brand = _brand(pipeline.database)
    case_id = _case(pipeline.database, brand, "mintera-heavy.test")

    scanning_worker.http_precheck.send(str(case_id))
    pipeline.run()

    _prechecked_to_capture(pipeline, case_id)
    [(started, ended)] = pipeline.attempts
    assert ended - started < capture.JOB_BUDGET_SECONDS
    scans = _scans(pipeline.database, case_id)
    assert {scan.outcome for scan in scans.values()} == {"ok"}
    artifacts = _artifacts(pipeline.database, case_id)
    for profile, (width, _) in SIZES.items():
        full = artifacts[profile, ArtifactKind.SCREENSHOT_FULL]
        data = _stored(pipeline.store, full.storage_key, full.sha256)
        assert _png_size(data) == (width, 10_000)  # about 30,000 px tall, cut at 10,000
        har = artifacts[profile, ArtifactKind.HAR]
        entries = json.loads(_stored(pipeline.store, har.storage_key, har.sha256))["log"]["entries"]
        media = [entry for entry in entries if "/media/" in entry["request"]["url"]]
        assert media, "the page's video was never requested"
        for entry in media:  # aborted, never downloaded
            assert "BLOCKED_BY_CLIENT" in entry["response"].get("_failureText", "")


# --- official reference screenshots --------------------------------------------------------------


def test_a_new_official_domain_gets_reference_screenshots(pipeline: Pipeline) -> None:
    """The outbox row of the new primary domain issues a reference job (§10)."""
    brand = _brand(pipeline.database)  # adds mintera.test: favicons and references are queued
    sent: list[str] = []

    def send(actor: str, **kwargs: str) -> None:
        sent.append(actor)
        send_message(actor, **kwargs)

    outbox.dispatch(pipeline.database, send, utcnow)
    assert {"refresh_brand_favicons", "refresh_brand_references"} <= set(sent)

    pipeline.run()

    assert len(pipeline.attempts) == 1
    with pipeline.database.worker_session() as session:
        assets = list(session.scalars(select(BrandAsset).where(BrandAsset.brand_id == brand.id)))
    assert BrandAssetKind.FAVICON in {asset.kind for asset in assets}
    references = {asset.kind: asset for asset in assets if asset.kind in REFERENCE.values()}
    assert set(references) == set(REFERENCE.values())
    for profile, kind in REFERENCE.items():
        reference = references[kind]
        prefix = f"gs://{BUCKET}/brands/{brand.id}/refs/"
        assert reference.storage_key.startswith(prefix)
        assert reference.storage_key.endswith(f"/{profile.value}.png")
        data = _stored(pipeline.store, reference.storage_key, reference.sha256)
        assert _png_size(data) == SIZES[profile]
        # https has nothing behind it on the fixture host; http answered.
        assert reference.source_url == "http://mintera.test/"
        assert (reference.phash, reference.dhash) == (None, None)  # computed in 08
    assert list(pipeline.root.iterdir()) == []
