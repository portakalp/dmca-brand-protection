"""Certificate Transparency consumer: ``python -m kalkan.discovery.ct`` (architecture.md §8).

Reads the lite stream of certstream-server-go (``/``), which carries the issuer and the log
name next to the domains. Every name is normalized and matched; a match is recorded at most
once per 24 hours per host (Redis key ``ct:seen:<host>``) and queued for ``admit_match``.
The "seen" check runs after matching, so Redis only holds matched hosts (K60). A key is
claimed before the write and released again if the write fails, so a failed transaction does
not hide the host for a day.

The client sends a WebSocket ping every 30 seconds, as the server requires, and reconnects
with exponential backoff and jitter. The backoff restarts only after a connection delivered
a message, so a server that accepts and drops at once still sees growing delays.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import signal
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import websockets
from redis import Redis
from websockets.exceptions import WebSocketException

from kalkan.config import ConfigError, get_settings
from kalkan.db.session import Database, get_database
from kalkan.discovery.catalog import MatcherCache
from kalkan.discovery.metrics import metrics
from kalkan.discovery.record import Sighting, record_discovery
from kalkan.domain.enums import SightingSource
from kalkan.logging import configure_logging, get_logger
from kalkan.sentry import init_sentry
from kalkan.workers.broker import setup_broker

SERVICE = "ct-consumer"
SEEN_SECONDS = 24 * 3600
SEEN_PREFIX = "ct:seen:"
PING_SECONDS = 30
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
# Failed messages are logged at most this often, with a count, so an outage cannot flood logs.
FAILURE_LOG_SECONDS = 10.0

# Named explicitly: the module also runs as ``__main__``.
log = get_logger("kalkan.discovery.ct")


class SeenStore(Protocol):
    def set(self, name: str, value: str, *, nx: bool, ex: int) -> Any: ...

    def delete(self, *names: str) -> Any: ...


@dataclass(frozen=True)
class Backoff:
    initial: float = 1.0
    maximum: float = 60.0
    jitter: float = 0.2

    def delay(self, attempt: int, rand: Callable[[], float] = random.random) -> float:
        base = min(self.maximum, self.initial * 2 ** min(attempt, 32))
        return float(base * (1 - self.jitter + 2 * self.jitter * rand()))


DEFAULT_BACKOFF = Backoff()


def _seen_at(data: dict[str, Any]) -> datetime:
    seen = data.get("seen")
    if isinstance(seen, int | float):
        return datetime.fromtimestamp(seen, UTC)
    return datetime.now(UTC)


def _issuer(leaf: dict[str, Any]) -> str | None:
    issuer = leaf.get("issuer") or {}
    return issuer.get("O") or issuer.get("CN") or issuer.get("aggregated")


class CtProcessor:
    """Turns one certstream message into recorded matches; synchronous, one thread."""

    def __init__(
        self,
        database: Database,
        matchers: MatcherCache,
        seen: SeenStore,
        queue_admit: Callable[[Sequence[uuid.UUID]], None],
    ) -> None:
        self._database = database
        self._matchers = matchers
        self._seen = seen
        self._queue_admit = queue_admit

    def process(self, message: dict[str, Any]) -> list[uuid.UUID]:
        if message.get("message_type") != "certificate_update":
            return []
        data = message.get("data") or {}
        leaf = data.get("leaf_cert") or {}
        names = leaf.get("all_domains") or []
        observed = metrics()
        observed.ct_events.inc()
        matcher = self._matchers.get()
        found = {}
        for name in names:
            if not isinstance(name, str):
                continue
            with observed.matcher_seconds.time():
                result = matcher.match_host(name)
            if result is not None and result[1]:
                found[result[0].host] = result
        if not found:
            return []
        detail = {
            "issuer": _issuer(leaf),
            "log": (data.get("source") or {}).get("name"),
            "cert_index": data.get("cert_index"),
            "update_type": data.get("update_type"),
        }
        sighting = Sighting(SightingSource.CT, _seen_at(data), detail)
        claimed = [
            host
            for host in sorted(found)
            if self._seen.set(SEEN_PREFIX + host, "1", nx=True, ex=SEEN_SECONDS)
        ]
        if not claimed:
            return []
        new_ids: list[uuid.UUID] = []
        try:
            with self._database.worker_session() as session:
                for host in claimed:
                    view, matches = found[host]
                    new_ids += record_discovery(session, view, matches, sighting).new_match_ids
        except Exception:
            # Nothing was written: let the next certificate for these hosts try again.
            self._seen.delete(*(SEEN_PREFIX + host for host in claimed))
            raise
        self._queue_admit(new_ids)
        return new_ids


async def consume(
    url: str,
    handle: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    stop: asyncio.Event,
    backoff: Backoff = DEFAULT_BACKOFF,
    on_connect: Callable[[], None] = lambda: None,
) -> None:
    """Read ``url`` until ``stop`` is set, reconnecting after every drop."""
    attempt = 0
    while not stop.is_set():
        try:
            async with websockets.connect(
                url,
                ping_interval=PING_SECONDS,
                ping_timeout=PING_SECONDS,
                max_size=MAX_MESSAGE_BYTES,
                open_timeout=10,
                # certstream runs next to us; never route through an environment proxy.
                proxy=None,
            ) as connection:
                log.info("ct.connected", url=url)
                on_connect()
                async for raw in connection:
                    attempt = 0
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        log.warning("ct.bad_message")
                        continue
                    if isinstance(message, dict):
                        await handle(message)
                    if stop.is_set():
                        return
                log.warning("ct.closed", url=url)
        except (OSError, TimeoutError, WebSocketException) as exc:
            log.warning("ct.disconnected", url=url, error=f"{type(exc).__name__}: {exc}")
        if stop.is_set():
            return
        delay = backoff.delay(attempt)
        attempt += 1
        log.info("ct.reconnecting", seconds=round(delay, 1), attempt=attempt)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)


def threaded_handler(
    processor: CtProcessor, executor: ThreadPoolExecutor
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Run the synchronous processor off the event loop, in one worker thread."""

    failures = 0
    last_logged = -FAILURE_LOG_SECONDS

    async def handle(message: dict[str, Any]) -> None:
        nonlocal failures, last_logged
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(executor, processor.process, message)
        except Exception:
            failures += 1
            now = time.monotonic()
            if now - last_logged >= FAILURE_LOG_SECONDS:
                log.exception("ct.process_failed", failures=failures)
                failures = 0
                last_logged = now

    return handle


HEARTBEAT_SECONDS = 30
# The heartbeat stops once no message was handled for this long; the stream never idles.
STALE_SECONDS = 120.0
# The container health check reads this file's modification time.
HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "kalkan-ct-consumer.heartbeat"
METRICS_PORT = 9300


class Progress:
    """When the consumer last handled a message."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.at = clock()

    def mark(self) -> None:
        self.at = self._clock()

    def recent(self) -> bool:
        return self._clock() - self.at < STALE_SECONDS


async def _heartbeat(stop: asyncio.Event, progress: Progress, path: Path = HEARTBEAT_FILE) -> None:
    while not stop.is_set():
        if progress.recent():
            path.touch()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)


async def run(url: str, processor: CtProcessor) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    progress = Progress()
    heartbeat = asyncio.create_task(_heartbeat(stop, progress))
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ct") as executor:
        inner = threaded_handler(processor, executor)

        async def handle(message: dict[str, Any]) -> None:
            await inner(message)
            progress.mark()

        await consume(url, handle, stop=stop)
    stop.set()
    await heartbeat
    log.info("ct.stopped")


def main() -> None:
    settings = get_settings()
    settings.require_for(SERVICE)
    configure_logging(SERVICE, settings.log_level)
    init_sentry(settings, SERVICE)
    if settings.redis_url is None or settings.certstream_url is None:  # pragma: no cover
        raise ConfigError("REDIS_URL and CERTSTREAM_URL are required")

    from prometheus_client import start_http_server

    metrics()  # the series exist from the start, not from the first certificate
    start_http_server(METRICS_PORT)
    setup_broker(settings.redis_url, prometheus=False)
    from kalkan.workers.discovery import queue_admit

    database = get_database()
    processor = CtProcessor(
        database,
        MatcherCache(database),
        Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5),
        queue_admit,
    )
    log.info("ct.starting", url=settings.certstream_url)
    asyncio.run(run(settings.certstream_url, processor))


if __name__ == "__main__":
    main()
