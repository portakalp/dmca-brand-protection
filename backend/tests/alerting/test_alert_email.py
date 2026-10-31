"""One alarm end to end on the local stack (architecture.md §25): ``make alert-test``.

The scheduler of ``make up`` exports today's AI spend from the development database; the
monitoring profile's Prometheus evaluates the VM's rules every 5 seconds and its Alertmanager
mails Mailpit. An ``ai_calls`` row above ALERT_AI_DAILY_USD fires AiDailySpendHigh; the row is
deleted afterwards, so the alarm resolves.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx2
import pytest
from sqlalchemy import delete

from kalkan.config import get_settings
from kalkan.db.models import AiCall
from kalkan.db.session import Database
from kalkan.domain.enums import AiPurpose

MAILPIT = "http://localhost:8025/api/v1"
PROMETHEUS = "http://localhost:9090/api/v1"
ALERT = "AiDailySpendHigh"
DEADLINE_SECONDS = 120


@pytest.fixture
def http() -> Iterator[httpx2.Client]:
    with httpx2.Client(timeout=5) as client:
        try:
            client.get(f"{PROMETHEUS}/status/buildinfo").raise_for_status()
        except httpx2.HTTPError:
            pytest.fail(
                "Prometheus does not answer on :9090; run `make alert-test` (make up first)"
            )
        yield client


@pytest.fixture
def spend() -> Iterator[uuid.UUID]:
    """A spend far above the alarm line in the development database, removed afterwards."""
    database = Database(get_settings())
    row = AiCall(
        purpose=AiPurpose.EVAL,
        model="alert-test",
        prompt_version="alert-test",
        cost_usd=Decimal(10_000),
        error="make alert-test",
    )
    with database.worker_session() as session:
        session.add(row)
    yield row.id
    with database.worker_session() as session:
        session.execute(delete(AiCall).where(AiCall.id == row.id))
    database.dispose()


def wait_for(check: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(2)
    pytest.fail(f"timed out waiting for {what}")


def test_an_alarm_reaches_mailpit(http: httpx2.Client, spend: uuid.UUID) -> None:
    started = datetime.now(UTC) - timedelta(seconds=5)

    def firing() -> bool:
        alerts = http.get(f"{PROMETHEUS}/alerts").json()["data"]["alerts"]
        return any(a["labels"]["alertname"] == ALERT and a["state"] == "firing" for a in alerts)

    def mailed() -> bool:
        found = http.get(f"{MAILPIT}/search", params={"query": f'subject:"{ALERT}"'}).json()
        return any(
            "FIRING" in message["Subject"] and datetime.fromisoformat(message["Created"]) >= started
            for message in found["messages"]
        )

    wait_for(firing, f"{ALERT} to fire in Prometheus")
    wait_for(mailed, f"the {ALERT} e-mail in Mailpit")
