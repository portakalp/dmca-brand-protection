"""The scheduler's state metrics (architecture.md §23, K173) against the test database and Redis."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import redis
from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy.exc import OperationalError

from kalkan.db.models import AiCall, ReviewTask
from kalkan.db.session import Database
from kalkan.domain.enums import AiPurpose, CaseState, TaskKind, TaskStatus
from kalkan.monitoring.exporter import StateCollector, registry
from tests import factories
from tests.support import redis_url_for_tests

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def queues() -> Iterator[redis.Redis]:
    client = redis.Redis.from_url(redis_url_for_tests())
    client.flushdb()
    yield client
    client.flushdb()


def scrape(collector: StateCollector) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    text = generate_latest(registry(collector)).decode()
    return {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(text)
        for sample in family.samples
    }


def collector(database: Database, client: redis.Redis) -> StateCollector:
    return StateCollector(
        database,
        client,
        ai_daily_alert_usd=Decimal(5),
        ai_monthly_budget_usd=Decimal(50),
        clock=lambda: NOW,
    )


def test_state_from_redis_and_the_database(database: Database, queues: redis.Redis) -> None:
    queues.hset("dramatiq:capture.msgs", mapping={"a": "1", "b": "2"})
    queues.zadd("dramatiq:notify.XQ", {"x": 1})
    with database.worker_session() as session:
        owner = factories.customer(session)
        case = factories.case(session, owner, factories.brand(session, owner), CaseState.NOTIFIED)
        session.add_all(
            [
                ReviewTask(
                    customer_id=owner.id,
                    case_id=case.id,
                    kind=TaskKind.MANUAL_CHANNEL,
                    created_at=NOW - timedelta(hours=30),
                ),
                ReviewTask(
                    customer_id=owner.id,
                    kind=TaskKind.REPLY,
                    status=TaskStatus.DONE,
                    created_at=NOW - timedelta(days=9),
                ),
                AiCall(
                    purpose=AiPurpose.TRIAGE,
                    model="m",
                    prompt_version="v",
                    cost_usd=Decimal("2.5"),
                    created_at=NOW - timedelta(hours=1),
                ),
                AiCall(
                    purpose=AiPurpose.TRIAGE,
                    model="m",
                    prompt_version="v",
                    cost_usd=Decimal("4"),
                    created_at=NOW - timedelta(days=2),
                ),
            ]
        )

    metrics = scrape(collector(database, queues))

    assert metrics[("queue_depth", (("queue", "capture"), ("state", "waiting")))] == 2
    assert metrics[("queue_depth", (("queue", "notify"), ("state", "dead")))] == 1
    assert metrics[("open_tasks", (("kind", "manual_channel"),))] == 1
    assert ("open_tasks", (("kind", "reply"),)) not in metrics  # done
    assert metrics[("open_task_oldest_seconds", (("kind", "manual_channel"),))] == 30 * 3600
    assert metrics[("cases", (("state", "notified"),))] == 1
    assert metrics[("ai_spend_today_usd", ())] == 2.5
    assert metrics[("ai_spend_month_usd", ())] == 6.5
    assert metrics[("ai_daily_alert_usd", ())] == 5
    assert metrics[("ai_monthly_budget_usd", ())] == 50
    assert metrics[("outbox_stuck", ())] == 0
    assert metrics[("exporter_source_up", (("source", "redis"),))] == 1
    assert metrics[("exporter_source_up", (("source", "database"),))] == 1


def test_a_failing_source_does_not_fail_the_scrape(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> None:
        raise OperationalError("SELECT 1", {}, Exception("down"))

    monkeypatch.setattr(database, "worker_session", broken)
    unreachable = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)

    metrics = scrape(collector(database, unreachable))

    assert metrics[("exporter_source_up", (("source", "redis"),))] == 0
    assert metrics[("exporter_source_up", (("source", "database"),))] == 0
    assert not [name for name, _ in metrics if name in {"queue_depth", "cases", "open_tasks"}]
    assert metrics[("ai_daily_alert_usd", ())] == 5
