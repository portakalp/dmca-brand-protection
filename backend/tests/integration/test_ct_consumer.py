"""CT consumer against a fake certstream server, Postgres and Redis."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import redis
from prometheus_client import REGISTRY
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from websockets.asyncio.server import ServerConnection, serve

from kalkan.db.models import BrandMatch, Domain, DomainSighting
from kalkan.db.session import Database
from kalkan.discovery.catalog import MatcherCache
from kalkan.discovery.ct import SEEN_PREFIX, SEEN_SECONDS, Backoff, CtProcessor, consume
from kalkan.discovery.ct import threaded_handler as make_handler
from kalkan.domain.enums import SightingSource
from tests import factories
from tests.support import redis_url_for_tests

TOTAL = 1_000
FIRST_CONNECTION = 600  # the server drops the client after this many messages
MATCHING_HOSTS = [f"mintera-{n:02d}.com" for n in range(25)]
# Seen only after the reconnect.
LATE_HOSTS = [f"mintera-late-{index}.com" for index in (650, 750, 850, 950)]


def _message(index: int, domains: list[str]) -> dict[str, Any]:
    return {
        "message_type": "certificate_update",
        "data": {
            "cert_index": index,
            "seen": 1_792_000_000.0 + index,
            "update_type": "PrecertLogEntry",
            "source": {"name": "Test Log 2026", "url": "https://ct.example/log"},
            "leaf_cert": {
                "all_domains": domains,
                "issuer": {"O": "Let's Encrypt", "CN": "R12"},
            },
        },
    }


def _messages() -> list[dict[str, Any]]:
    """1,000 certificate updates; 30 distinct matching hosts, most of them seen twice."""
    messages = []
    for index in range(TOTAL):
        domains = [f"shop{index}.example.com", f"www.shop{index}.example.com"]
        if index % 20 == 0:  # 50 messages carry 25 matching hosts, each twice
            domains.append(MATCHING_HOSTS[(index // 20) % 25])
        if index == 7:  # one certificate, one host: wildcard and www fold into it
            domains += ["*.mintera-login.top", "www.mintera-login.top", "mintera-login.top"]
        if index % 50 == 3:  # the demo brand is never matched (K46)
            domains.append(f"demobank-{index}.com")
        if index == 11:
            domains += ["not a host", "203.0.113.5", 42]  # type: ignore[list-item]
        if f"mintera-late-{index}.com" in LATE_HOSTS:
            domains.append(f"mintera-late-{index}.com")
        messages.append(_message(index, domains))
    return messages


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Queued:
    def __init__(self) -> None:
        self.ids: list[uuid.UUID] = []

    def __call__(self, ids: Sequence[uuid.UUID]) -> None:
        self.ids += ids


@pytest.fixture
def seen_store() -> Iterator[redis.Redis]:
    client = redis.Redis.from_url(redis_url_for_tests())
    for key in client.scan_iter(f"{SEEN_PREFIX}*"):
        client.delete(key)
    yield client
    for key in client.scan_iter(f"{SEEN_PREFIX}*"):
        client.delete(key)
    client.close()


async def _run(url_holder: dict[str, Any], processor: CtProcessor) -> int:
    messages = _messages()
    connections = 0

    async def server_handler(connection: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        if connections == 1:
            await connection.send("{not json")
            for message in messages[:FIRST_CONNECTION]:
                await connection.send(json.dumps(message))
            await connection.close(code=1011, reason="test drop")
        else:
            await connection.send(json.dumps({"message_type": "heartbeat"}))
            for message in messages[FIRST_CONNECTION:]:
                await connection.send(json.dumps(message))
            await connection.wait_closed()

    stop = asyncio.Event()
    handled = 0
    with ThreadPoolExecutor(max_workers=1) as executor:
        inner = make_handler(processor, executor)

        async def handle(message: dict[str, Any]) -> None:
            nonlocal handled
            await inner(message)
            if message.get("message_type") == "certificate_update":
                handled += 1
                if handled == TOTAL:
                    stop.set()

        async with serve(server_handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            url_holder["url"] = f"ws://127.0.0.1:{port}/"
            await asyncio.wait_for(
                consume(
                    url_holder["url"],
                    handle,
                    stop=stop,
                    backoff=Backoff(initial=0.01, maximum=0.05),
                ),
                timeout=60,
            )
    assert handled == TOTAL
    return connections


def test_thousand_messages_with_a_reconnect(database: Database, seen_store: redis.Redis) -> None:
    with database.worker_session() as session:
        owner = factories.customer(session, "mint")
        factories.brand(session, owner)
        demo = factories.customer(session, "demo", is_demo=True)
        demo_brand = factories.brand(session, demo)
        demo_brand.keywords = ["demobank"]

    queued = Queued()
    processor = CtProcessor(database, MatcherCache(database), seen_store, queued)
    events_before = _sample("ct_events_total")
    keyword_before = _sample("matches_total", source="ct", reason="keyword")
    connections = asyncio.run(_run({}, processor))

    assert connections == 2
    expected_hosts = {*MATCHING_HOSTS, *LATE_HOSTS, "mintera-login.top"}
    with database.worker_session() as session:
        matches = session.scalar(select(func.count()).select_from(BrandMatch))
        sightings = session.scalars(select(DomainSighting)).all()
        names = set(session.scalars(select(Domain.name)))
    assert matches == len(expected_hosts) == 30
    assert set(LATE_HOSTS) <= names  # recorded after the reconnect
    assert len(queued.ids) == 30
    assert len(set(queued.ids)) == 30
    assert len(sightings) == 30
    assert _sample("ct_events_total") - events_before == TOTAL
    assert _sample("matches_total", source="ct", reason="keyword") - keyword_before == 30
    assert {sighting.source for sighting in sightings} == {SightingSource.CT}
    assert {sighting.detail["log"] for sighting in sightings} == {"Test Log 2026"}
    assert {sighting.detail["issuer"] for sighting in sightings} == {"Let's Encrypt"}

    keys = {
        key.decode().removeprefix(SEEN_PREFIX) for key in seen_store.scan_iter(f"{SEEN_PREFIX}*")
    }
    assert keys == expected_hosts
    assert 0 < seen_store.ttl(f"{SEEN_PREFIX}mintera-00.com") <= SEEN_SECONDS


class FlakyDatabase:
    """The test database, but the first worker transaction fails."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self.failures = 1

    def worker_session(self) -> Any:
        if self.failures:
            self.failures -= 1
            raise OperationalError("SELECT 1", {}, Exception("connection lost"))
        return self._database.worker_session()


def test_a_failed_write_releases_the_seen_keys(database: Database, seen_store: redis.Redis) -> None:
    with database.worker_session() as session:
        factories.brand(session, factories.customer(session, "mint"))
    matchers = MatcherCache(database)
    matchers.get()  # loaded before the database starts failing
    flaky = FlakyDatabase(database)
    queued = Queued()
    processor = CtProcessor(flaky, matchers, seen_store, queued)  # type: ignore[arg-type]
    message = _message(1, ["mintera-login.com", "shop.example.com"])

    with pytest.raises(OperationalError):
        processor.process(message)
    assert seen_store.get(f"{SEEN_PREFIX}mintera-login.com") is None
    assert len(processor.process(message)) == 1  # the next copy of the certificate is recorded
    assert len(queued.ids) == 1


def test_backoff_grows_while_connections_drop_at_once() -> None:
    attempts: list[int] = []

    async def run() -> int:
        stop = asyncio.Event()
        accepted = 0

        async def drop(connection: ServerConnection) -> None:
            nonlocal accepted
            accepted += 1
            await connection.close()

        class RecordingBackoff(Backoff):
            def delay(self, attempt: int, rand: Any = None) -> float:
                attempts.append(attempt)
                if len(attempts) == 4:
                    stop.set()
                return 0.01

        async def handle(_: dict[str, Any]) -> None:  # pragma: no cover - no message is sent
            raise AssertionError

        async with serve(drop, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            await asyncio.wait_for(
                consume(f"ws://127.0.0.1:{port}/", handle, stop=stop, backoff=RecordingBackoff()),
                timeout=10,
            )
        return accepted

    assert asyncio.run(run()) == 4
    assert attempts == [0, 1, 2, 3]


def test_consumer_gives_up_cleanly_when_stopped_during_backoff() -> None:
    async def run() -> int:
        stop = asyncio.Event()
        attempts = 0

        async def handle(_: dict[str, Any]) -> None:  # pragma: no cover - never connected
            raise AssertionError

        def on_connect() -> None:  # pragma: no cover - never connected
            raise AssertionError

        class CountingBackoff(Backoff):
            def delay(self, attempt: int, rand: Any = None) -> float:
                nonlocal attempts
                attempts += 1
                if attempts == 3:
                    stop.set()
                return 0.01

        # Port 9 (discard) on localhost refuses the connection immediately.
        await asyncio.wait_for(
            consume(
                "ws://127.0.0.1:9/",
                handle,
                stop=stop,
                backoff=CountingBackoff(),
                on_connect=on_connect,
            ),
            timeout=10,
        )
        return attempts

    assert asyncio.run(run()) == 3
