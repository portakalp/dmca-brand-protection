"""Local load check: a CT burst through the pipeline (docs/load-test.md).

``make load`` (or ``uv run python -m tests.load.ct_burst --help``). Never against prod.

What runs for real, against the ``kalkan_test`` database and Redis database 14 of
``make up-infra``: the CT processor (matcher, ``record_discovery``, the seen store), admission,
enrichment and the HTTP pre-check on worker-default's actors with its 8 threads, and the
capture records in Redis. What is fake, with the latencies below: DNS, RDAP and Web Risk
answers, the candidate sites (every one serves the fixture clone), the browser (2 processes
of 1 thread, as on the VM) and Claude (4 threads). Fake captures do not persist; each one
queues a fake AI call, which is the load the AI stage would see.

Every ``--sample`` seconds it prints the queue depths, this process's CPU and memory and the
Postgres and Redis containers' ``docker stats``; at the end a JSON summary goes to ``--out``.
The ``kalkan_test`` database is emptied afterwards (do not run it next to ``make test``).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import resource
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# A command-line tool: its progress goes to stdout.
# ruff: noqa: T201

LOAD_REDIS_DB = 14
REPO = Path(__file__).resolve().parents[3]
CLONE = (REPO / "fixtures/sites/clone/index.html").read_bytes()
FAKE_IP = "203.0.113.10"
WORDS = ("login", "secure", "verify", "account", "pay", "card", "app", "promo", "refund", "club")
NOISE = ("shop", "news", "cloud", "mail", "blog", "photo", "store", "travel", "home", "media")


def _url(url: str, *, database: str | None = None, redis_db: int | None = None) -> str:
    parts = urlsplit(url)
    if database is not None:
        return urlunsplit(parts._replace(path=f"/{database}"))
    return urlunsplit(parts._replace(path=f"/{redis_db}"))


def configure_environment() -> None:
    """Point every setting at the test database and the load Redis database, before
    ``get_settings`` is first called by any module."""
    from kalkan.config import get_settings

    settings = get_settings()
    assert settings.database_url_worker is not None
    assert settings.database_url_migrate is not None
    assert settings.redis_url is not None
    os.environ["DATABASE_URL_WORKER"] = _url(
        settings.database_url_worker.get_secret_value(), database="kalkan_test"
    )
    os.environ["DATABASE_URL_MIGRATE"] = _url(
        settings.database_url_migrate.get_secret_value(), database="kalkan_test"
    )
    os.environ["REDIS_URL"] = _url(settings.redis_url, redis_db=LOAD_REDIS_DB)
    os.environ.pop("WEBRISK_API_KEY", None)
    get_settings.cache_clear()


@dataclass
class Latency:
    dns: float
    rdap: float
    http: float
    capture: float
    ai: float


class FakeResolver:
    def __init__(self, latency: float) -> None:
        self.latency = latency

    def resolve(self, name: str, rdtype: str) -> list[str]:
        time.sleep(self.latency)
        if rdtype == "A":
            return [FAKE_IP]
        if rdtype == "NS":
            return ["ns1.fakehost.example.", "ns2.fakehost.example."]
        if rdtype == "TXT" and name.endswith("cymru.com"):
            return ['"64500 | 203.0.113.0/24 | PL | ripencc | 2010-01-01"']
        return []


class FakeRdap:
    def __init__(self, latency: float) -> None:
        self.latency = latency

    def domain(self, name: str) -> dict[str, Any] | None:
        time.sleep(self.latency)
        return None

    def ip(self, address: str) -> dict[str, Any] | None:
        time.sleep(self.latency)
        return None

    def asn(self, number: int) -> dict[str, Any] | None:
        time.sleep(self.latency)
        return None


@dataclass
class Counters:
    lock: threading.Lock = field(default_factory=threading.Lock)
    certificates: int = 0
    matches: int = 0
    captures: int = 0
    ai_calls: int = 0

    def add(self, name: str, value: int = 1) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + value)


def install_fakes(latency: Latency, counters: Counters) -> None:
    """Replace the network clients of worker-default's actors and declare the browser and AI
    actors this process plays."""
    import dramatiq
    import httpx2

    from kalkan.ai.queue import send_by_name
    from kalkan.enrichment import service
    from kalkan.enrichment.cloudflare import load_ranges
    from kalkan.net.safe_http import SafeHttpClient
    from kalkan.scanning.capture_jobs import CAPTURE_ACTOR, CAPTURE_QUEUE
    from kalkan.workers import enrichment, scanning
    from kalkan.workers.enrichment import _redis

    def sources() -> service.Sources:
        redis = _redis()
        return service.Sources(
            resolver=FakeResolver(latency.dns),
            rdap=FakeRdap(latency.rdap),
            cloudflare_ranges=lambda: load_ranges(redis),
            cache=redis,
        )

    def site(request: httpx2.Request) -> httpx2.Response:
        time.sleep(latency.http)
        return httpx2.Response(
            200, headers={"content-type": "text/html"}, stream=httpx2.ByteStream(CLONE)
        )

    import ipaddress

    client = SafeHttpClient(
        allow_cidrs=[ipaddress.ip_network("203.0.113.0/24")],
        resolver=lambda host: [FAKE_IP],
        transport=httpx2.MockTransport(site),
    )
    enrichment.sources.cache_clear()
    scanning.http_client.cache_clear()
    enrichment.sources = sources  # type: ignore[assignment]
    scanning.http_client = lambda: client  # type: ignore[assignment]

    @dramatiq.actor(actor_name=CAPTURE_ACTOR, queue_name=CAPTURE_QUEUE, max_retries=0)
    def fake_capture(job: dict[str, Any]) -> None:
        # Two profiles per job (desktop and mobile), as the browser stage does.
        time.sleep(latency.capture * len(job.get("profiles", [1])))
        counters.add("captures")
        send_by_name("ai_triage", case_id=job["job_id"])

    @dramatiq.actor(actor_name="ai_triage", queue_name="ai", max_retries=0)
    def fake_ai(case_id: str) -> None:
        time.sleep(latency.ai)
        counters.add("ai_calls")


def prepare_database() -> None:
    from alembic import command
    from alembic.config import Config

    from kalkan.config import get_settings
    from kalkan.db.engine import create_db_engine

    url = get_settings().database_url_migrate
    assert url is not None
    engine = create_db_engine(url)
    config = Config(str(REPO / "backend/alembic.ini"))
    config.set_main_option("script_location", str(REPO / "backend/alembic"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    engine.dispose()


def empty_database() -> None:
    from sqlalchemy import text

    from kalkan.config import get_settings
    from kalkan.db.engine import create_db_engine
    from kalkan.db.models import GLOBAL_TABLES, TENANT_TABLES

    url = get_settings().database_url_migrate
    assert url is not None
    engine = create_db_engine(url)
    tables = [name for name in (*TENANT_TABLES, *GLOBAL_TABLES) if name != "settings"]
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {', '.join(tables)}, customers CASCADE"))
    engine.dispose()


def load_customer() -> None:
    from kalkan.db.models import Brand, Customer
    from kalkan.db.session import get_database
    from kalkan.domain.enums import CustomerStatus

    with get_database().worker_session() as session:
        customer = Customer(name="Load Bank", slug="load-bank", status=CustomerStatus.TRIAL)
        session.add(customer)
        session.flush()
        session.add(
            Brand(
                customer_id=customer.id,
                name="LoadBank",
                keywords=["loadbank"],
                target_markets=["PL"],
                languages=["pl", "en"],
            )
        )


def certificate(names: list[str], index: int) -> dict[str, Any]:
    return {
        "message_type": "certificate_update",
        "data": {
            "update_type": "X509LogEntry",
            "cert_index": index,
            "seen": time.time(),
            "source": {"name": "load"},
            "leaf_cert": {"all_domains": names, "issuer": {"O": "Load CA"}},
        },
    }


def feed(args: argparse.Namespace, counters: Counters, stop: threading.Event) -> threading.Thread:
    """The CT burst: ``--noise`` certificates a second, ``--matches-per-hour`` of them for the
    brand, through the real CT processor."""
    from redis import Redis

    from kalkan.config import get_settings
    from kalkan.db.session import get_database
    from kalkan.discovery.catalog import MatcherCache
    from kalkan.discovery.ct import CtProcessor
    from kalkan.workers.discovery import queue_admit

    redis_url = get_settings().redis_url
    assert redis_url is not None
    database = get_database()
    processor = CtProcessor(
        database, MatcherCache(database), Redis.from_url(redis_url), queue_admit
    )
    rng = random.Random(19)  # noqa: S311 - made-up domain names, not secrets
    match_every = 3600 / args.matches_per_hour

    def run() -> None:
        started = time.monotonic()
        next_match = started
        index = 0
        while not stop.is_set() and time.monotonic() - started < args.minutes * 60:
            second = time.monotonic()
            for _ in range(args.noise):
                index += 1
                name = f"{rng.choice(NOISE)}{rng.randint(1, 10**9)}.com"
                processor.process(certificate([name, f"www.{name}"], index))
                counters.add("certificates")
            while next_match <= time.monotonic():
                index += 1
                name = f"loadbank-{rng.choice(WORDS)}{index}.{rng.choice(('com', 'net', 'xyz'))}"
                counters.add("matches", len(processor.process(certificate([name], index))))
                counters.add("certificates")
                next_match += match_every
            stop.wait(max(0.0, 1 - (time.monotonic() - second)))

    thread = threading.Thread(target=run, name="ct-feed", daemon=True)
    thread.start()
    return thread


def start_workers() -> list[Any]:
    import dramatiq
    from dramatiq import Worker

    from kalkan.workers.specs import WORKERS

    broker = dramatiq.get_broker()
    default = WORKERS["default"]
    workers = [
        Worker(broker, queues=set(default.queues), worker_threads=default.threads),
        Worker(broker, queues={"capture"}, worker_threads=WORKERS["browser"].processes),
        Worker(broker, queues={"ai"}, worker_threads=WORKERS["ai"].threads),
    ]
    for worker in workers:
        worker.start()
    return workers


def container_stats() -> dict[str, str]:
    try:
        out = subprocess.run(
            [  # noqa: S607 - docker from PATH, local only
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.Name}} {{.CPUPerc}} {{.MemUsage}}",
                "kalkan-postgres-1",
                "kalkan-redis-1",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    stats = {}
    for line in out.splitlines():
        name, cpu, memory = line.split(" ", 2)
        stats[name.removeprefix("kalkan-").removesuffix("-1")] = f"{cpu} {memory.split(' / ')[0]}"
    return stats


def rss_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


def cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def sample(redis: Any, counters: Counters, started: float, last: list[float]) -> dict[str, Any]:
    from kalkan.ops.status import queue_depths

    now = time.monotonic()
    cpu = cpu_seconds()
    cpu_percent = 100 * (cpu - last[1]) / max(now - last[0], 1e-6)
    last[:] = [now, cpu]
    depths = queue_depths(redis) or []
    return {
        "t": round(now - started),
        "certificates": counters.certificates,
        "matches": counters.matches,
        "captures": counters.captures,
        "ai_calls": counters.ai_calls,
        "queues": {d.name: d.waiting + d.delayed for d in depths if d.waiting or d.delayed},
        "dead": {d.name: d.dead for d in depths if d.dead},
        "process_cpu_percent": round(cpu_percent),
        "process_rss_mb": round(rss_mb()),
        "containers": container_stats(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--matches-per-hour", type=float, default=10_000)
    parser.add_argument("--noise", type=int, default=50, help="other certificates per second")
    parser.add_argument("--minutes", type=float, default=10, help="length of the burst")
    parser.add_argument("--drain", type=float, default=5, help="minutes to watch afterwards")
    parser.add_argument("--sample", type=float, default=30, help="seconds between samples")
    parser.add_argument("--dns", type=float, default=0.05, help="fake DNS latency, seconds")
    parser.add_argument("--rdap", type=float, default=0.8, help="fake RDAP latency, seconds")
    parser.add_argument("--http", type=float, default=0.5, help="fake site latency, seconds")
    parser.add_argument("--capture", type=float, default=8, help="seconds per browser profile")
    parser.add_argument("--ai", type=float, default=6, help="seconds per Claude call")
    parser.add_argument("--out", type=Path, default=REPO / ".local/load/ct_burst.json")
    parser.add_argument("--keep", action="store_true", help="keep the rows for a look afterwards")
    args = parser.parse_args()

    configure_environment()
    from redis import Redis

    from kalkan.config import get_settings
    from kalkan.logging import configure_logging
    from kalkan.workers.broker import setup_broker
    from kalkan.workers.specs import WORKERS

    configure_logging("load", "WARNING")
    redis_url = get_settings().redis_url
    assert redis_url is not None
    redis = Redis.from_url(redis_url)
    redis.flushdb()
    setup_broker(redis_url, prometheus=False)
    import importlib

    for module in WORKERS["default"].actor_modules:
        importlib.import_module(module)
    counters = Counters()
    install_fakes(Latency(args.dns, args.rdap, args.http, args.capture, args.ai), counters)
    prepare_database()
    empty_database()
    load_customer()

    stop = threading.Event()
    samples: list[dict[str, Any]] = []
    workers = start_workers()
    started = time.monotonic()
    last = [started, cpu_seconds()]
    feeder = feed(args, counters, stop)
    print(
        f"load: {args.matches_per_hour:.0f} matches/h, {args.noise} certificates/s, "
        f"{args.minutes} min burst + {args.drain} min drain",
        flush=True,
    )
    try:
        while time.monotonic() - started < (args.minutes + args.drain) * 60:
            time.sleep(args.sample)
            row = sample(redis, counters, started, last)
            samples.append(row)
            print(json.dumps(row), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        feeder.join(timeout=10)
        for worker in workers:
            worker.stop(timeout=5_000)
        summary = {
            "at": datetime.now(UTC).isoformat(),
            "args": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
            "samples": samples,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, indent=2) + "\n")
        print(f"load: summary in {args.out}", flush=True)
        if not args.keep:
            empty_database()
            redis.flushdb()


if __name__ == "__main__":
    main()
