"""``browser_capture`` without a browser: deferrals, drops, hand-off and accounting."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import REGISTRY

from kalkan.domain.enums import Profile
from kalkan.scanning.capture import CaptureFailedError
from kalkan.scanning.capture_contract import (
    PROXY_BYTES_TTL_SECONDS,
    CaptureJob,
    CaptureMeta,
    Outcome,
    ProfileResult,
    Purpose,
)
from kalkan.workers import capture as worker
from tests.support import make_settings

pytestmark = pytest.mark.usefixtures("clean_env")

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


class FakeRedis:
    def __init__(self, values: dict[str, int] | None = None) -> None:
        self.values = dict(values or {})
        self.expires: dict[str, int] = {}

    def get(self, key: str) -> bytes | None:
        return str(self.values[key]).encode() if key in self.values else None

    def incrby(self, key: str, amount: int) -> int:
        self.values[key] = self.values.get(key, 0) + amount
        return self.values[key]

    def expire(self, key: str, seconds: int) -> bool:
        self.expires[key] = seconds
        return True


class Sent:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.deferred: list[tuple[dict[str, Any], int]] = []
        self.persisted: list[uuid.UUID] = []
        monkeypatch.setattr(worker.browser_capture, "send_with_options", self._defer)
        monkeypatch.setattr(worker, "send_persist", lambda job: self.persisted.append(job.job_id))

    def _defer(self, *, args: tuple[Any, ...], delay: int) -> None:
        self.deferred.append((args[0], delay))


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Sent:
    return Sent(monkeypatch)


def _job(**values: Any) -> dict[str, Any]:
    job = CaptureJob(
        job_id=uuid.uuid4(),
        purpose=Purpose.CASE,
        urls=["http://mintera-login.test/"],
        country="TR",
        profiles=[Profile.DESKTOP, Profile.MOBILE],
        issued_at=NOW - timedelta(minutes=5),
    )
    return job.model_copy(update=values).model_dump(mode="json")


def _result(profile: Profile, outcome: Outcome, proxy_bytes: int) -> ProfileResult:
    return ProfileResult(
        profile=profile,
        outcome=outcome,
        started_at=NOW,
        finished_at=NOW,
        proxy_bytes=proxy_bytes,
    )


class FakeRun:
    def __init__(self, root: Path, *, fail: bool = False) -> None:
        self.root = root
        self.fail = fail
        self.calls: list[tuple[CaptureJob, Any, bool]] = []

    def __call__(
        self, job: CaptureJob, proxy: Any, *, root: Path, final: bool
    ) -> tuple[Path, CaptureMeta]:
        self.calls.append((job, proxy, final))
        results = [
            _result(Profile.DESKTOP, Outcome.ERROR if self.fail else Outcome.OK, 1_500),
            _result(Profile.MOBILE, Outcome.ERROR, 500),
        ]
        if self.fail:
            raise CaptureFailedError("every profile failed", results=results)
        folder = root / str(job.job_id)
        folder.mkdir()
        return folder, CaptureMeta(job_id=job.job_id, proxy_country=proxy.country, profiles=results)


def _counter(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _handle(raw: object, run: FakeRun, redis: FakeRedis | None = None, **settings: Any) -> None:
    values: dict[str, Any] = {"env": "local", "proxy_map": {"PL": "http://127.0.0.1:18888"}}
    values.update(settings)
    worker.handle(
        raw,
        settings=make_settings(**values),
        redis=redis or FakeRedis(),  # type: ignore[arg-type]
        final=False,
        now=NOW,
        root=run.root,
        run=run,
    )


def test_without_a_proxy_no_page_opens_and_the_job_waits(tmp_path: Path, sent: Sent) -> None:
    run = FakeRun(tmp_path)
    job = _job()
    before = _counter("capture_deferrals_total", reason="no_proxy")

    _handle(job, run, proxy_map={})

    assert run.calls == []  # Playwright is never reached
    assert sent.deferred == [(job, 30 * 60 * 1000)]  # the same job, as a new message
    assert sent.persisted == []
    assert list(tmp_path.iterdir()) == []
    assert _counter("capture_deferrals_total", reason="no_proxy") == before + 1


def test_no_default_country_proxy_also_waits(tmp_path: Path, sent: Sent) -> None:
    run = FakeRun(tmp_path)
    _handle(_job(country="RU"), run, proxy_map={"TR": "http://tr:1"}, proxy_default_country="PL")
    assert run.calls == []
    assert len(sent.deferred) == 1


def test_the_default_country_proxy_is_used(tmp_path: Path, sent: Sent) -> None:
    run = FakeRun(tmp_path)
    _handle(_job(country="RU"), run)
    [(_, proxy, final)] = run.calls
    assert proxy.country == "PL"
    assert final is False
    assert sent.deferred == []


def test_over_the_daily_limit_the_job_waits_for_the_next_day(tmp_path: Path, sent: Sent) -> None:
    run = FakeRun(tmp_path)
    redis = FakeRedis({"kalkan:proxy:bytes:20261020": 2 * 10**9})
    before = _counter("capture_deferrals_total", reason="proxy_daily_limit")

    _handle(_job(), run, redis)

    assert run.calls == []
    [(_, delay)] = sent.deferred
    until_midnight = int(timedelta(hours=15).total_seconds() * 1000)
    assert until_midnight <= delay < until_midnight + 30 * 60 * 1000
    assert _counter("capture_deferrals_total", reason="proxy_daily_limit") == before + 1


def test_under_the_daily_limit_the_job_runs(tmp_path: Path, sent: Sent) -> None:
    run = FakeRun(tmp_path)
    redis = FakeRedis({"kalkan:proxy:bytes:20261020": 2 * 10**9 - 1})
    _handle(_job(), run, redis)
    assert len(run.calls) == 1


def test_a_capture_is_handed_off_and_counted(tmp_path: Path, sent: Sent) -> None:
    run = FakeRun(tmp_path)
    redis = FakeRedis()
    job = _job()
    ok_before = _counter("captures_total", outcome="ok")
    bytes_before = _counter("proxy_bytes_total")

    _handle(job, run, redis)

    [(captured, proxy, _)] = run.calls
    assert str(captured.job_id) == job["job_id"]
    assert proxy.country == "PL"  # no TR proxy: the default country's
    assert sent.persisted == [captured.job_id]
    key = worker.day_key(datetime.now(UTC))
    assert redis.values[key] == 2_000
    assert redis.expires[key] == PROXY_BYTES_TTL_SECONDS
    assert _counter("captures_total", outcome="ok") == ok_before + 1
    assert _counter("proxy_bytes_total") == bytes_before + 2_000


def test_a_failed_attempt_counts_its_bytes_and_raises_for_a_retry(
    tmp_path: Path, sent: Sent
) -> None:
    run = FakeRun(tmp_path, fail=True)
    redis = FakeRedis()
    with pytest.raises(CaptureFailedError):
        _handle(_job(), run, redis)
    assert sent.persisted == []
    assert sum(redis.values.values()) == 2_000


def test_a_redelivered_job_only_sends_persist_again(tmp_path: Path, sent: Sent) -> None:
    run = FakeRun(tmp_path)
    job = _job()
    (tmp_path / job["job_id"]).mkdir()
    _handle(job, run)
    assert run.calls == []
    assert [str(job_id) for job_id in sent.persisted] == [job["job_id"]]


@pytest.mark.parametrize(
    "raw",
    [
        "0199f0f0-0000-7000-8000-000000000000",  # the K70 skeleton's case id
        {"job_id": "not a uuid"},
        {**_job(), "case_id": "smuggled"},
        {**_job(), "profiles": []},
    ],
)
def test_bad_messages_are_dropped(tmp_path: Path, sent: Sent, raw: object) -> None:
    run = FakeRun(tmp_path)
    _handle(raw, run)
    assert run.calls == []
    assert sent.deferred == []
    assert sent.persisted == []


def test_an_old_job_is_dropped(tmp_path: Path, sent: Sent) -> None:
    run = FakeRun(tmp_path)
    _handle(_job(issued_at=NOW - timedelta(hours=49)), run, proxy_map={})
    assert run.calls == []
    assert sent.deferred == []


def test_persist_is_sent_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[Any] = []

    class Broker:
        def enqueue(self, message: Any) -> None:
            messages.append(message)

    monkeypatch.setattr(worker.dramatiq, "get_broker", Broker)
    job = CaptureJob.model_validate(_job())
    worker.send_persist(job)
    [message] = messages
    assert (message.queue_name, message.actor_name) == ("persist", "persist_capture")
    assert message.args == (str(job.job_id),)


def test_the_actor_options() -> None:
    assert worker.browser_capture.queue_name == "capture"
    assert worker.browser_capture.actor_name == "browser_capture"
    assert worker.browser_capture.options["max_retries"] == 1
    assert worker.browser_capture.options["time_limit"] == 120_000


def test_a_browser_that_cannot_start_defers_the_job(tmp_path: Path, sent: Sent) -> None:
    from kalkan.scanning.capture import BrowserUnavailableError

    class NoBrowser(FakeRun):
        def __call__(self, *args: Any, **kwargs: Any) -> tuple[Path, CaptureMeta]:
            raise BrowserUnavailableError("No usable sandbox!")

    job = _job()
    before = _counter("capture_deferrals_total", reason="browser_unavailable")
    _handle(job, NoBrowser(tmp_path))
    assert sent.deferred == [(job, 30 * 60 * 1000)]  # waits; no retry used, no case fails
    assert _counter("capture_deferrals_total", reason="browser_unavailable") == before + 1
    assert sent.persisted == []
