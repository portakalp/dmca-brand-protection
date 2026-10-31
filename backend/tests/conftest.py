"""Shared test setup.

Tests under ``tests/unit`` are marked ``unit`` and must not need external services. Tests
under ``tests/integration`` are marked ``integration`` and run against the docker compose
services started by ``make up-infra``. Tests under ``tests/benchmark`` are marked
``benchmark`` and only run with ``make bench``; ``tests/alerting`` (``alerting``) with
``make alert-test``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kalkan.config import Settings
from kalkan.workers.broker import setup_broker
from tests.support import redis_url_for_tests

# Actors bind to the global broker on import, so it is created before test collection.
# Creating it does not connect to Redis.
setup_broker(redis_url_for_tests(), prometheus=False)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        parts = Path(str(item.path)).parts
        if "integration" in parts:
            item.add_marker(pytest.mark.integration)
        elif "unit" in parts:
            item.add_marker(pytest.mark.unit)
        elif "benchmark" in parts:
            item.add_marker(pytest.mark.benchmark)
        elif "e2e" in parts:
            item.add_marker(pytest.mark.e2e)
        elif "alerting" in parts:
            item.add_marker(pytest.mark.alerting)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove every Kalkan setting from the process environment."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    return monkeypatch
