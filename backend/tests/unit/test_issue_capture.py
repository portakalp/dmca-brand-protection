"""Issuing capture jobs: the trusted record, the pending marker and the message."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import dramatiq
import pytest
from redis import Redis

from kalkan.domain.enums import Profile
from kalkan.scanning.capture_contract import (
    JOB_KEY,
    JOB_TTL_SECONDS,
    MAX_URL,
    PENDING_KEY,
    PENDING_TTL_SECONDS,
    CaptureJob,
    CaptureRecord,
    Purpose,
)
from kalkan.scanning.capture_jobs import (
    country_for,
    issue_capture,
    load_record,
    release_pending,
    send_job,
)

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
CUSTOMER = uuid.UUID("0193a000-0000-7000-8000-000000000001")
BRAND = uuid.UUID("0193a000-0000-7000-8000-000000000002")
CASE = uuid.UUID("0193a000-0000-7000-8000-000000000003")


class FakeRedis:
    """The few commands capture_jobs uses."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.ttls: dict[str, int | None] = {}

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = value.encode()
        self.ttls[key] = ex
        return True

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    def delete(self, *keys: str) -> int:
        return sum(self.values.pop(key, None) is not None for key in keys)

    def exists(self, key: str) -> int:
        return int(key in self.values)


def _issue(redis: FakeRedis, sent: list[CaptureJob], **values: Any) -> CaptureJob | None:
    arguments: dict[str, Any] = {
        "purpose": Purpose.CASE,
        "customer_id": CUSTOMER,
        "brand_id": BRAND,
        "case_id": CASE,
        "urls": ["https://mintera-login.test/", "http://mintera-login.test/"],
        "country": "PL",
        "now": NOW,
        "send": sent.append,
    }
    arguments.update(values)
    return issue_capture(cast(Redis, redis), **arguments)


def test_a_case_job_is_recorded_marked_and_sent() -> None:
    redis, sent = FakeRedis(), list[CaptureJob]()
    job = _issue(redis, sent)
    assert job is not None
    assert sent == [job]
    assert job.urls == ["https://mintera-login.test/", "http://mintera-login.test/"]
    assert (job.country, job.profiles, job.issued_at) == (
        "PL",
        [Profile.DESKTOP, Profile.MOBILE],
        NOW,
    )

    key = JOB_KEY.format(job_id=job.job_id)
    record = CaptureRecord.model_validate_json(redis.values[key])
    assert redis.ttls[key] == JOB_TTL_SECONDS
    assert (record.case_id, record.customer_id, record.brand_id) == (CASE, CUSTOMER, BRAND)
    assert set(record.scan_ids) == {Profile.DESKTOP, Profile.MOBILE}
    assert len(set(record.scan_ids.values())) == 2
    assert record.urls == job.urls
    assert load_record(cast(Redis, redis), job.job_id) == record

    pending = PENDING_KEY.format(case_id=CASE)
    assert redis.values[pending] == str(job.job_id).encode()
    assert redis.ttls[pending] == PENDING_TTL_SECONDS
    # Nothing the browser receives names the case, the customer or the scans (K82).
    message = job.model_dump(mode="json")
    assert set(message) == {"job_id", "purpose", "urls", "country", "profiles", "issued_at"}


def test_a_case_with_a_live_job_gets_no_second_one() -> None:
    redis, sent = FakeRedis(), list[CaptureJob]()
    first = _issue(redis, sent)
    assert first is not None
    assert _issue(redis, sent) is None
    assert sent == [first]
    release_pending(cast(Redis, redis), CASE, uuid.uuid4())  # another job's id: kept
    assert _issue(redis, sent) is None
    release_pending(cast(Redis, redis), CASE, first.job_id)
    assert _issue(redis, sent) is not None


def test_a_failed_send_releases_the_marker() -> None:
    redis = FakeRedis()

    def broken(job: CaptureJob) -> None:
        raise ConnectionError("broker down")

    with pytest.raises(ConnectionError):
        _issue(redis, [], send=broken)
    assert PENDING_KEY.format(case_id=CASE) not in redis.values


def test_reference_jobs_have_no_marker() -> None:
    redis, sent = FakeRedis(), list[CaptureJob]()
    first = _issue(redis, sent, purpose=Purpose.REFERENCE, case_id=None)
    second = _issue(redis, sent, purpose=Purpose.REFERENCE, case_id=None)
    assert first is not None
    assert second is not None
    assert not any(key.startswith("capture:pending:") for key in redis.values)
    with pytest.raises(ValueError, match="case id"):
        _issue(redis, sent, case_id=None)


def test_entry_urls_are_deduplicated_and_capped() -> None:
    redis, sent = FakeRedis(), list[CaptureJob]()
    urls = [f"https://mintera-{n}.test/" for n in range(7)]
    job = _issue(redis, sent, urls=["https://x.test/" + "a" * MAX_URL, urls[0], *urls])
    assert job is not None
    assert job.urls == urls[:5]


def test_country_is_the_first_target_market() -> None:
    assert country_for(["pl", "TR"]) == "PL"
    assert country_for([]) is None
    assert country_for(["POL"]) is None


def test_an_unreadable_record_is_no_record() -> None:
    redis = FakeRedis()
    job_id = uuid.uuid4()
    assert load_record(cast(Redis, redis), job_id) is None
    redis.values[JOB_KEY.format(job_id=job_id)] = b"{not json"
    assert load_record(cast(Redis, redis), job_id) is None


def test_send_job_queues_browser_capture_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    enqueued: list[dramatiq.Message[Any]] = []

    class FakeBroker:
        def enqueue(self, message: dramatiq.Message[Any]) -> None:
            enqueued.append(message)

    monkeypatch.setattr(dramatiq, "get_broker", FakeBroker)
    job = CaptureJob(
        job_id=uuid.uuid4(),
        purpose=Purpose.CASE,
        urls=["https://mintera-login.test/"],
        country=None,
        profiles=[Profile.DESKTOP, Profile.MOBILE],
        issued_at=NOW,
    )
    send_job(job)
    [message] = enqueued
    assert (message.queue_name, message.actor_name) == ("capture", "browser_capture")
    assert message.kwargs == {}
    assert CaptureJob.model_validate(message.args[0]) == job
