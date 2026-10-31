"""The system page: queue depths and this month's AI and proxy spend (architecture.md §7, §19).

Queue depths are read from Dramatiq's Redis keys: ``dramatiq:<queue>.msgs`` holds every message
of the queue not yet acknowledged (waiting or running), ``dramatiq:<queue>.DQ.msgs`` the delayed
ones (retries, deferrals), ``dramatiq:<queue>.XQ`` the dead letters. AI spend sums
``ai_calls``; proxy traffic sums the browser's daily byte counters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalkan.ai.cost import month_spend, month_start
from kalkan.db.models import JobOutbox
from kalkan.scanning.capture_contract import PROXY_BYTES_KEY
from kalkan.scheduler.outbox import MAX_ATTEMPTS

# Every queue of §7, in its order.
QUEUES = (
    "default",
    "discovery",
    "enrich",
    "precheck",
    "capture",
    "persist",
    "signals",
    "ai",
    "decision",
    "notify",
    "followup",
    "webhooks",
    "reports",
)
GB = 1024**3


@dataclass(frozen=True)
class QueueDepth:
    name: str
    waiting: int
    delayed: int
    dead: int


def queue_depths(redis: Redis) -> list[QueueDepth] | None:
    """``None`` when Redis does not answer."""
    try:
        pipe = redis.pipeline(transaction=False)
        for name in QUEUES:
            pipe.hlen(f"dramatiq:{name}.msgs")
            pipe.hlen(f"dramatiq:{name}.DQ.msgs")
            pipe.zcard(f"dramatiq:{name}.XQ")
        counts: list[Any] = pipe.execute()
    except RedisError:
        return None
    return [
        QueueDepth(name, int(counts[3 * i]), int(counts[3 * i + 1]), int(counts[3 * i + 2]))
        for i, name in enumerate(QUEUES)
    ]


@dataclass(frozen=True)
class AiSpend:
    calls: int
    cost_usd: Decimal


def ai_spend(session: Session, now: datetime) -> AiSpend:
    return AiSpend(*month_spend(session, now))


def proxy_gb(redis: Redis, now: datetime) -> tuple[float, float] | None:
    """``(this month, today)`` in GB; ``None`` when Redis does not answer."""
    start = month_start(now).date()
    today = now.astimezone(UTC).date()
    days = [start + timedelta(days=offset) for offset in range((today - start).days + 1)]
    try:
        values = redis.mget([PROXY_BYTES_KEY.format(day=day.strftime("%Y%m%d")) for day in days])
    except RedisError:
        return None
    numbers = [int(value or 0) for value in values]
    return sum(numbers) / GB, (numbers[-1] if numbers else 0) / GB


def stuck_outbox(session: Session) -> int:
    """Outbox rows the dispatcher gave up on (broken rows; K66)."""
    return int(
        session.scalar(
            select(func.count(JobOutbox.id)).where(
                JobOutbox.dispatched_at.is_(None), JobOutbox.attempts >= MAX_ATTEMPTS
            )
        )
        or 0
    )
