from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import dramatiq
import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from kalkan.scheduler.__main__ import (
    HEARTBEAT_INTERVAL_SECONDS,
    LAST_SENT_KEY,
    build_scheduler,
    catch_up,
    heartbeat,
    previous_fire_time,
    send_actor,
)


def test_heartbeat_job_runs_every_minute() -> None:
    scheduler = build_scheduler()
    job = scheduler.get_job("heartbeat")
    assert job is not None
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval.total_seconds() == HEARTBEAT_INTERVAL_SECONDS == 60
    assert job.max_instances == 1


def test_heartbeat_touches_the_health_file(tmp_path: Path) -> None:
    target = tmp_path / "heartbeat"
    heartbeat(target)
    first = target.stat().st_mtime_ns
    heartbeat(target)
    assert target.exists()
    assert target.stat().st_mtime_ns >= first


def test_discovery_jobs_queue_their_actors_at_fixed_utc_times() -> None:
    scheduler = build_scheduler()
    expected = {
        "permutations_weekly": ("generate_permutations", "mon", "2", "0"),
        "permutation_dns_daily": ("check_permutation_dns", "*", "3", "0"),
        "czds_daily": ("czds_scan", "*", "6", "30"),
        "references_weekly": ("refresh_brand_references", "mon", "4", "0"),
        "capture_folders_daily": ("clean_capture_folders", "*", "5", "0"),
        "reports_monthly": ("build_reports", "*", "9", "0"),
    }
    for job_id, (actor, day_of_week, hour, minute) in expected.items():
        job = scheduler.get_job(job_id)
        assert job is not None, job_id
        assert job.func is send_actor
        assert job.args == (actor, job_id)
        fields = {field.name: str(field) for field in job.trigger.fields}
        assert (fields["day_of_week"], fields["hour"], fields["minute"]) == (
            day_of_week,
            hour,
            minute,
        )
        assert str(job.trigger.timezone) == "UTC"


def test_send_actor_uses_the_declared_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    class FakeActor:
        def __init__(self, name: str) -> None:
            self.name = name

        def send(self) -> None:
            sent.append(self.name)

    class FakeBroker:
        def get_actor(self, name: str) -> FakeActor:
            return FakeActor(name)

    monkeypatch.setattr(dramatiq, "get_broker", FakeBroker)
    log = FakeSentLog()
    send_actor("czds_scan", "czds_daily", log, datetime(2026, 10, 20, 6, 30, tzinfo=UTC))
    assert sent == ["czds_scan"]
    assert log.values == {"czds_daily": "2026-10-20T06:30:00+00:00"}


class FakeSentLog:
    def __init__(self, **values: str) -> None:
        self.values = dict(values)

    def hget(self, name: str, key: str) -> bytes | None:
        assert name == LAST_SENT_KEY
        value = self.values.get(key)
        return value.encode() if value is not None else None

    def hset(self, name: str, key: str, value: str) -> None:
        assert name == LAST_SENT_KEY
        self.values[key] = value


def test_previous_fire_time() -> None:
    trigger = CronTrigger(day_of_week="mon", hour=2, minute=0, timezone=UTC)
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)  # a Wednesday
    assert previous_fire_time(trigger, now) == datetime(2026, 10, 19, 2, 0, tzinfo=UTC)
    daily = CronTrigger(hour=6, minute=30, timezone=UTC)
    early = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    assert previous_fire_time(daily, early) == datetime(2026, 10, 20, 6, 30, tzinfo=UTC)


def test_catch_up_sends_missed_jobs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def fake_send(actor: str, job: str, log: FakeSentLog, now: datetime) -> None:
        sent.append(actor)
        log.hset(LAST_SENT_KEY, job, now.isoformat())

    monkeypatch.setattr("kalkan.scheduler.__main__.send_actor", fake_send)
    now = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
    log = FakeSentLog(
        cloudflare_ranges_daily="2026-10-21T01:00:00+00:00",
        permutations_weekly="2026-10-19T02:00:00+00:00",  # this week's run happened
        permutation_dns_daily="2026-10-20T03:00:00+00:00",  # today's 03:00 run was missed
        czds_daily="2026-10-21T06:30:01+00:00",  # today's run happened
    )
    assert catch_up(log, now) == ["permutation_dns_daily"]
    assert sent == ["check_permutation_dns"]
    assert catch_up(log, now) == []


def test_catch_up_only_records_jobs_on_a_fresh_start() -> None:
    log = FakeSentLog()
    now = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
    assert catch_up(log, now) == []
    assert set(log.values) == {
        "permutations_weekly",
        "permutation_dns_daily",
        "czds_daily",
        "cloudflare_ranges_daily",
        "references_weekly",
        "rechecks",
        "escalations_hourly",
        "reports_monthly",
        "capture_folders_daily",
    }


def test_outbox_and_reconciler_run_on_intervals() -> None:
    scheduler = build_scheduler()
    outbox = scheduler.get_job("outbox")
    reconciler = scheduler.get_job("reconciler")
    assert outbox is not None
    assert reconciler is not None
    assert outbox.trigger.interval.total_seconds() == 5
    assert reconciler.trigger.interval.total_seconds() == 600
    assert outbox.max_instances == reconciler.max_instances == 1


def test_reports_are_built_on_the_first_of_the_month() -> None:
    reports = build_scheduler().get_job("reports_monthly")
    assert reports is not None
    fields = {field.name: str(field) for field in reports.trigger.fields}
    assert (fields["day"], fields["hour"]) == ("1", "9")
    assert reports.args == ("build_reports", "reports_monthly")
