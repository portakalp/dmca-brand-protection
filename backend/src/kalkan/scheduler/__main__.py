"""Scheduler process: ``python -m kalkan.scheduler`` (architecture.md §7).

A single process. Jobs query the database for work that is due, so restarts are safe.
Jobs that do real work only queue an actor; the workers run it. Each send is recorded in
Redis (``scheduler:last_sent``); at start-up a job whose last scheduled time passed while
the scheduler was down is sent once (K59).
"""

from __future__ import annotations

import importlib
import signal
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from types import FrameType
from typing import Any, Protocol

import dramatiq
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from prometheus_client import start_http_server
from redis import Redis

from kalkan.ai.queue import BY_NAME, send_by_name, set_ai_enabled
from kalkan.config import ConfigError, get_settings
from kalkan.db.session import get_database
from kalkan.logging import configure_logging, get_logger
from kalkan.monitoring import exporter
from kalkan.scanning.capture_contract import CaptureJob
from kalkan.scanning.capture_jobs import issue_case_capture
from kalkan.scheduler import outbox, reconciler
from kalkan.sentry import init_sentry
from kalkan.workers.broker import setup_broker
from kalkan.workers.specs import WORKERS

SERVICE = "scheduler"
HEARTBEAT_INTERVAL_SECONDS = 60
# The container health check reads this file's modification time.
HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "kalkan-scheduler.heartbeat"

# (job id, actor name, trigger); times are UTC (§7; K59).
ACTOR_JOBS: tuple[tuple[str, str, CronTrigger], ...] = (
    (
        "permutations_weekly",
        "generate_permutations",
        CronTrigger(day_of_week="mon", hour=2, minute=0, timezone=UTC),
    ),
    ("permutation_dns_daily", "check_permutation_dns", CronTrigger(hour=3, minute=0, timezone=UTC)),
    # ICANN publishes the day's zone files after 06:00 UTC.
    ("czds_daily", "czds_scan", CronTrigger(hour=6, minute=30, timezone=UTC)),
    (
        "cloudflare_ranges_daily",
        "refresh_cloudflare_ranges",
        CronTrigger(hour=1, minute=0, timezone=UTC),
    ),
    # Official sites' reference screenshots (§7, §10).
    (
        "references_weekly",
        "refresh_brand_references",
        CronTrigger(day_of_week="mon", hour=4, minute=0, timezone=UTC),
    ),
    # The follow-up engine (§16): due checks every 15 minutes, escalations hourly.
    ("rechecks", "schedule_rechecks", CronTrigger(minute="*/15", timezone=UTC)),
    ("escalations_hourly", "check_escalations", CronTrigger(minute=5, timezone=UTC)),
    # Monthly reports (§21): last month's, in each customer's time zone.
    (
        "reports_monthly",
        "build_reports",
        CronTrigger(day=1, hour=9, minute=0, timezone=UTC),
    ),
    # Job folders the browser hand-off left behind (worker-default mounts the volume).
    (
        "capture_folders_daily",
        "clean_capture_folders",
        CronTrigger(hour=5, minute=0, timezone=UTC),
    ),
)
OUTBOX_INTERVAL_SECONDS = 5
RECONCILER_INTERVAL_SECONDS = 600

LAST_SENT_KEY = "scheduler:last_sent"
# Far enough back to find the previous run of the weekly job.
LOOKBACK = timedelta(days=8)

log = get_logger("kalkan.scheduler")


class SentLog(Protocol):
    def hget(self, name: str, key: str) -> Any: ...

    def hset(self, name: str, key: str, value: str) -> Any: ...


def heartbeat(path: Path = HEARTBEAT_FILE) -> None:
    path.touch()
    log.info("scheduler.heartbeat")


def send_message(actor_name: str, **kwargs: str) -> None:
    """Send one message to an actor declared in this process, or by name to one of another
    process (``kalkan.ai.queue.BY_NAME``; with AI off, AI actors go to their stand-ins)."""
    if actor_name in BY_NAME:
        send_by_name(actor_name, **kwargs)
        return
    dramatiq.get_broker().get_actor(actor_name).send(**kwargs)


def dispatch_outbox() -> None:
    outbox.dispatch(get_database(), send_message, lambda: datetime.now(UTC))


@cache
def capture_redis() -> Redis:
    """Capture job records and pending markers (``kalkan.scanning.capture_contract``)."""
    url = get_settings().redis_url
    if url is None:  # pragma: no cover - the scheduler requires REDIS_URL
        raise ConfigError("REDIS_URL is required")
    return Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)


def run_reconciler() -> None:
    database = get_database()
    now = datetime.now(UTC)

    def capture(case_id: uuid.UUID) -> CaptureJob | None:
        return issue_case_capture(database, capture_redis(), case_id, now=now)

    reconciler.reconcile(database, send_message, now, capture=capture)


def send_actor(
    actor_name: str,
    job_id: str,
    sent_log: SentLog | None = None,
    now: datetime | None = None,
) -> None:
    dramatiq.get_broker().get_actor(actor_name).send()
    if sent_log is not None:
        sent_log.hset(LAST_SENT_KEY, job_id, (now or datetime.now(UTC)).isoformat())
    log.info("scheduler.queued", actor=actor_name, job=job_id)


def previous_fire_time(trigger: CronTrigger, now: datetime) -> datetime | None:
    """The trigger's last fire time at or before ``now``, within :data:`LOOKBACK`."""
    probe = trigger.get_next_fire_time(None, now - LOOKBACK)
    last = None
    while probe is not None and probe <= now:
        last = probe
        probe = trigger.get_next_fire_time(probe, probe + timedelta(seconds=1))
    return last


def catch_up(sent_log: SentLog, now: datetime) -> list[str]:
    """Send the jobs whose last scheduled run was missed; returns their ids.

    A job seen for the first time is only recorded, so a fresh start does not run them all.
    """
    sent = []
    for job_id, actor_name, trigger in ACTOR_JOBS:
        raw = sent_log.hget(LAST_SENT_KEY, job_id)
        if raw is None:
            sent_log.hset(LAST_SENT_KEY, job_id, now.isoformat())
            continue
        last = datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else raw)
        previous = previous_fire_time(trigger, now)
        if previous is not None and last < previous:
            send_actor(actor_name, job_id, sent_log, now)
            sent.append(job_id)
    return sent


def build_scheduler(sent_log: SentLog | None = None) -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=UTC)
    scheduler.add_job(
        heartbeat,
        IntervalTrigger(seconds=HEARTBEAT_INTERVAL_SECONDS),
        id="heartbeat",
        next_run_time=datetime.now(UTC),
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        dispatch_outbox,
        IntervalTrigger(seconds=OUTBOX_INTERVAL_SECONDS),
        id="outbox",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_reconciler,
        IntervalTrigger(seconds=RECONCILER_INTERVAL_SECONDS),
        id="reconciler",
        coalesce=True,
        max_instances=1,
    )
    for job_id, actor_name, trigger in ACTOR_JOBS:
        scheduler.add_job(
            send_actor,
            trigger,
            args=[actor_name, job_id],
            kwargs={"sent_log": sent_log},
            id=job_id,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
    return scheduler


def main() -> None:
    settings = get_settings()
    settings.require_for(SERVICE)
    configure_logging(SERVICE, settings.log_level)
    init_sentry(settings, SERVICE)
    if settings.redis_url is None:  # pragma: no cover - require_for already checked it
        raise ConfigError("REDIS_URL is required")
    setup_broker(settings.redis_url, prometheus=False)
    # The outbox and the reconciler send AI work to worker-ai, or to worker-default's
    # stand-ins when AI is off (kalkan.ai.queue.WITHOUT_AI).
    set_ai_enabled(settings.use_ai)
    log.info("scheduler.ai_mode", ai=settings.use_ai)
    for module in WORKERS["default"].actor_modules:  # declares the actors the jobs send to
        importlib.import_module(module)

    sent_log = Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5)
    # State metrics for Prometheus (§23, K173); a daemon thread serves every scrape.
    start_http_server(
        exporter.PORT,
        registry=exporter.registry(
            exporter.StateCollector(
                get_database(),
                sent_log,
                ai_daily_alert_usd=settings.alert_ai_daily_usd,
                ai_monthly_budget_usd=settings.ai_monthly_budget_usd,
            )
        ),
    )
    missed = catch_up(sent_log, datetime.now(UTC))
    if missed:
        log.info("scheduler.caught_up", jobs=missed)
    scheduler = build_scheduler(sent_log)

    def stop(signum: int, _: FrameType | None) -> None:
        log.info("scheduler.stopping", signal=signal.Signals(signum).name)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("scheduler.starting")
    scheduler.start()


if __name__ == "__main__":
    main()
