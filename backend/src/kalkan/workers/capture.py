"""The ``capture`` queue, consumed by worker-browser (architecture.md §7, §10 stage 2).

``browser_capture(job)`` receives a :class:`CaptureJob` from a trusted issuer
(``kalkan.scanning.capture_jobs``). worker-browser has no database and no cloud credentials
(K6): it captures the pages into the hand-off folder and sends ``persist_capture(job_id)``
by name to worker-default, which records everything else.

Waiting is not a failure (§7). Without a proxy for the job, or over the daily proxy volume, no
page is opened: the same job is sent again with a delay as a new message, so no retry is used,
and ``capture_deferrals_total{reason}`` counts it for the alarm. A Chromium that cannot start
waits the same way (``browser_unavailable``), so a broken worker never fails a case. Every
profile failing uses the one retry; on the last attempt the browser hands off
``final_attempt=true`` instead, and worker-default moves the case to review (``capture_failed``).

Playwright is imported only when a page is captured, so this module loads without it.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import dramatiq
from pydantic import ValidationError
from redis import Redis, RedisError

from kalkan.config import Settings, get_settings
from kalkan.logging import get_logger
from kalkan.scanning.capture_contract import (
    ARTIFACTS_ROOT,
    CAPTURE_QUEUE,
    PENDING_TTL_SECONDS,
    PERSIST_ACTOR,
    PERSIST_QUEUE,
    PROXY_BYTES_KEY,
    PROXY_BYTES_TTL_SECONDS,
    CaptureJob,
    CaptureMeta,
    ProfileResult,
    job_dir,
)
from kalkan.scanning.metrics import metrics
from kalkan.scanning.proxy import ProxyChoice, new_session, select_proxy
from kalkan.workers.broker import final_attempt

QUEUE = CAPTURE_QUEUE
MAX_RETRIES = 1
NO_PROXY_DELAY = timedelta(minutes=30)
LIMIT_JITTER = timedelta(minutes=30)
GB = 10**9
# Older jobs are dropped: their record and pending marker are about to expire, and the
# reconciler issues a new job for the case (K90).
MAX_JOB_AGE = timedelta(seconds=PENDING_TTL_SECONDS) - timedelta(hours=2)
# The hand-off folder; tests point it elsewhere.
ROOT = ARTIFACTS_ROOT

RunJob = Callable[..., tuple[Path, CaptureMeta]]

log = get_logger(__name__)


@dramatiq.actor(queue_name=QUEUE, max_retries=MAX_RETRIES, time_limit=120_000)
def browser_capture(job: dict[str, Any]) -> None:
    """Capture ``job`` with every profile (§10) and hand it to ``persist_capture``."""
    handle(
        job,
        settings=get_settings(),
        redis=broker_redis(),
        final=final_attempt(MAX_RETRIES),
        now=datetime.now(UTC),
        root=ROOT,
    )


def broker_redis() -> Redis:
    """The broker's Redis connection (the browser's ACL user allows ``kalkan:proxy:*``)."""
    client = getattr(dramatiq.get_broker(), "client", None)
    if isinstance(client, Redis):
        return client
    url = get_settings().redis_url
    if url is None:  # pragma: no cover - require_for("worker-browser") checked it at boot
        raise RuntimeError("REDIS_URL is required")
    return Redis.from_url(url)


def handle(
    raw: object,
    *,
    settings: Settings,
    redis: Redis,
    final: bool,
    now: datetime,
    root: Path,
    run: RunJob | None = None,
) -> None:
    """One delivery of a ``browser_capture`` message."""
    if not isinstance(raw, dict):
        # The K70 skeleton sent a case id; the reconciler issues a proper job for the case.
        log.warning("capture.legacy_message_dropped")
        return
    try:
        job = CaptureJob.model_validate(raw)
    except ValidationError as exc:
        log.warning("capture.invalid_job_dropped", errors=exc.error_count())
        return
    issued = job.issued_at if job.issued_at.tzinfo else job.issued_at.replace(tzinfo=UTC)
    if now - issued > MAX_JOB_AGE:
        log.warning("capture.stale_job_dropped", job_id=str(job.job_id), issued_at=str(issued))
        return
    if job_dir(job.job_id, root).exists():
        # A redelivery after the hand-off: worker-default has not taken the folder yet.
        log.info("capture.already_handed_off", job_id=str(job.job_id))
        send_persist(job)
        return
    proxy = select_proxy(
        settings.proxy_map, job.country, settings.proxy_default_country, session=new_session()
    )
    if proxy is None:
        defer(job, NO_PROXY_DELAY, reason="no_proxy", country=job.country)
        return
    limit = settings.proxy_daily_gb_limit * GB
    if used_bytes(redis, now) >= limit:
        midnight = datetime.combine(now.date() + timedelta(days=1), time(0), tzinfo=UTC)
        jitter = timedelta(milliseconds=secrets.randbelow(int(LIMIT_JITTER.total_seconds() * 1000)))
        defer(job, midnight - now + jitter, reason="proxy_daily_limit", country=proxy.country)
        return
    capture(job, proxy, redis=redis, final=final, root=root, run=run)


def capture(
    job: CaptureJob,
    proxy: ProxyChoice,
    *,
    redis: Redis,
    final: bool,
    root: Path,
    run: RunJob | None = None,
) -> None:
    from kalkan.scanning.capture import BrowserUnavailableError, CaptureFailedError, run_job

    log.info(
        "capture.started",
        job_id=str(job.job_id),
        purpose=job.purpose.value,
        proxy_country=proxy.country,
        proxy=proxy.endpoint,
        final_attempt=final,
    )
    try:
        folder, meta = (run or run_job)(job, proxy, root=root, final=final)
    except BrowserUnavailableError as exc:
        # Chromium did not start (e.g. no usable sandbox): no case should fail for it. The job
        # waits like a missing proxy and the deferral metric raises the alarm (§7).
        log.error("capture.browser_unavailable", job_id=str(job.job_id), error=str(exc))
        defer(job, NO_PROXY_DELAY, reason="browser_unavailable", country=proxy.country)
        return
    except CaptureFailedError as exc:
        account(redis, exc.results)
        log.warning("capture.attempt_failed_will_retry", job_id=str(job.job_id), error=str(exc))
        raise
    account(redis, meta.profiles)
    send_persist(job)
    log.info(
        "capture.handed_off",
        job_id=str(job.job_id),
        outcomes={result.profile.value: result.outcome.value for result in meta.profiles},
        final_attempt=meta.final_attempt,
        folder=folder.name,
    )


def send_persist(job: CaptureJob) -> None:
    """Queue ``persist_capture(job_id)`` by name: the actor lives in worker-default."""
    message: dramatiq.Message[None] = dramatiq.Message(
        queue_name=PERSIST_QUEUE,
        actor_name=PERSIST_ACTOR,
        args=(str(job.job_id),),
        kwargs={},
        options={},
    )
    dramatiq.get_broker().enqueue(message)


def defer(job: CaptureJob, delay: timedelta, *, reason: str, country: str | None) -> None:
    """Send ``job`` again after ``delay`` as a new message (no retry used; §7)."""
    browser_capture.send_with_options(
        args=(job.model_dump(mode="json"),), delay=max(0, int(delay.total_seconds() * 1000))
    )
    metrics().deferrals.labels(reason).inc()
    log.warning(
        "capture.deferred",
        job_id=str(job.job_id),
        reason=reason,
        country=country,
        delay_seconds=int(delay.total_seconds()),
    )


def day_key(at: datetime) -> str:
    return PROXY_BYTES_KEY.format(day=at.astimezone(UTC).strftime("%Y%m%d"))


def used_bytes(redis: Redis, at: datetime) -> int:
    """Approximate proxy bytes of ``at``'s UTC day (both browser processes add to it)."""
    value = redis.get(day_key(at))
    return int(value) if value else 0


def account(redis: Redis, results: Sequence[ProfileResult]) -> None:
    """Count outcomes and add the attempt's proxy bytes to the day's counter."""
    for result in results:
        metrics().captures.labels(result.outcome.value).inc()
    total = sum(result.proxy_bytes for result in results)
    if total <= 0:
        return
    metrics().proxy_bytes.inc(total)
    key = day_key(datetime.now(UTC))
    try:
        redis.incrby(key, total)
        redis.expire(key, PROXY_BYTES_TTL_SECONDS)
    except RedisError:
        log.warning("capture.proxy_bytes_not_counted", bytes=total, exc_info=True)
