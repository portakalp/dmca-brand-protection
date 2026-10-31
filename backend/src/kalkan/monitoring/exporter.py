"""The scheduler's metrics: state read from Redis and the database (architecture.md §23, K173).

The worker counters only see what one process did since it started; the alarms need the state
of the system instead: how many messages wait in each queue, how old the oldest open operator
task is, today's AI spend against its alarm line. The scheduler is one long-lived process with
both connections, so it serves these on :data:`PORT`; every scrape reads them afresh. A failing
source does not fail the scrape: its gauges are left out and ``exporter_source_up`` drops to 0,
which has its own alarm.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal

from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector, CollectorRegistry
from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from kalkan.ai.cost import month_spend
from kalkan.db.models import AiCall, Case, Notice, ReviewTask
from kalkan.db.session import Database
from kalkan.domain.enums import TaskStatus
from kalkan.logging import get_logger
from kalkan.ops.status import queue_depths, stuck_outbox

PORT = 9400

log = get_logger(__name__)


def day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _gauge(name: str, doc: str, value: float | Decimal) -> GaugeMetricFamily:
    return GaugeMetricFamily(name, doc, value=float(value))


class StateCollector(Collector):
    def __init__(
        self,
        database: Database,
        redis: Redis,
        *,
        ai_daily_alert_usd: Decimal,
        ai_monthly_budget_usd: Decimal,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database = database
        self.redis = redis
        self.ai_daily_alert_usd = ai_daily_alert_usd
        self.ai_monthly_budget_usd = ai_monthly_budget_usd
        self.clock = clock

    def collect(self) -> Iterator[Metric]:
        up = GaugeMetricFamily(
            "exporter_source_up", "Whether the source answered this scrape.", labels=["source"]
        )
        queues = list(self._queues())
        up.add_metric(["redis"], 1 if queues else 0)
        yield from queues
        try:
            with self.database.worker_session() as session:
                database = list(self._database(session, self.clock()))
        except SQLAlchemyError as exc:
            log.warning("exporter.database_failed", error=type(exc).__name__)
            database = []
        up.add_metric(["database"], 1 if database else 0)
        yield from database
        yield up
        yield _gauge(
            "ai_daily_alert_usd",
            "AI spend per UTC day that raises the alarm.",
            self.ai_daily_alert_usd,
        )
        yield _gauge(
            "ai_monthly_budget_usd",
            "The monthly AI budget (AI_MONTHLY_BUDGET_USD).",
            self.ai_monthly_budget_usd,
        )

    def _queues(self) -> Iterator[Metric]:
        depths = queue_depths(self.redis)
        if depths is None:
            return
        gauge = GaugeMetricFamily(
            "queue_depth",
            "Messages per Dramatiq queue: waiting (not yet acknowledged), delayed, dead.",
            labels=["queue", "state"],
        )
        for depth in depths:
            gauge.add_metric([depth.name, "waiting"], depth.waiting)
            gauge.add_metric([depth.name, "delayed"], depth.delayed)
            gauge.add_metric([depth.name, "dead"], depth.dead)
        yield gauge

    def _database(self, session: Session, now: datetime) -> Iterator[Metric]:
        tasks = GaugeMetricFamily("open_tasks", "Open review and operator tasks.", labels=["kind"])
        oldest = GaugeMetricFamily(
            "open_task_oldest_seconds", "Age of the oldest open task.", labels=["kind"]
        )
        rows = session.execute(
            select(ReviewTask.kind, func.count(), func.min(ReviewTask.created_at))
            .where(ReviewTask.status == TaskStatus.OPEN)
            .group_by(ReviewTask.kind)
        ).all()
        for kind, count, first in rows:
            tasks.add_metric([kind.value], count)
            oldest.add_metric([kind.value], (now - first).total_seconds())
        yield tasks
        yield oldest

        cases = GaugeMetricFamily("cases", "Cases by state.", labels=["state"])
        for state, count in session.execute(
            select(Case.state, func.count()).group_by(Case.state)
        ).all():
            cases.add_metric([state.value], count)
        yield cases

        notices = GaugeMetricFamily(
            "notices",
            "Notices by channel, status and dry run.",
            labels=["channel", "status", "dry_run"],
        )
        for channel, status, dry_run, count in session.execute(
            select(Notice.channel, Notice.status, Notice.dry_run, func.count()).group_by(
                Notice.channel, Notice.status, Notice.dry_run
            )
        ).all():
            notices.add_metric([channel.value, status.value, str(dry_run).lower()], count)
        yield notices

        today = session.scalar(
            select(func.coalesce(func.sum(AiCall.cost_usd), 0)).where(
                AiCall.created_at >= day_start(now)
            )
        )
        yield _gauge("ai_spend_today_usd", "AI spend of the current UTC day.", Decimal(today or 0))
        yield _gauge(
            "ai_spend_month_usd", "AI spend of the current UTC month.", month_spend(session, now)[1]
        )
        yield _gauge(
            "outbox_stuck", "Outbox rows the dispatcher gave up on.", stuck_outbox(session)
        )


def registry(collector: StateCollector) -> CollectorRegistry:
    result = CollectorRegistry(auto_describe=False)
    result.register(collector)
    return result
