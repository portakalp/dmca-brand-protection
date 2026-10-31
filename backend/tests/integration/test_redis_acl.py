"""worker-browser's Redis user runs Dramatiq and reaches nothing else (§24; K82).

The ACL file comes from the VM's own renderer (``vm/bin/load-secrets.sh --print-redis-acl``) and
is loaded, as on the VM, by a Redis of the VM's image that this module starts in Docker with
``--aclfile``; the shared development Redis has no users and is left alone.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import dramatiq
import pytest
import redis
import yaml
from dramatiq import Message, Worker
from dramatiq.brokers.redis import MAINTENANCE_SCALE, RedisBroker

REPO = Path(__file__).resolve().parents[3]
LOAD_SECRETS = REPO / "infra" / "compose" / "vm" / "bin" / "load-secrets.sh"
VM_COMPOSE = REPO / "infra" / "compose" / "docker-compose.vm.yml"
APP_PASSWORD = "app-test-password"
BROWSER_PASSWORD = "browser-test-password"
COUNTER = "kalkan:proxy:bytes:20261016"


def _run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout  # noqa: S603


@pytest.fixture(scope="module")
def address(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """``host:port`` of a Redis that runs with the VM's ACL file."""
    docker = shutil.which("docker")
    bash = shutil.which("bash")
    if docker is None or bash is None:
        pytest.fail("docker and bash are needed to start the Redis for the ACL test")
    hashes = [hashlib.sha256(p.encode()).hexdigest() for p in (APP_PASSWORD, BROWSER_PASSWORD)]
    acl = tmp_path_factory.mktemp("redis") / "users.acl"
    acl.write_text(_run(bash, str(LOAD_SECRETS), "--print-redis-acl", *hashes))
    acl.chmod(0o644)  # Redis runs as its own user in the container
    image = yaml.safe_load(VM_COMPOSE.read_text())["services"]["redis"]["image"]
    container = _run(
        docker, "run", "--detach", "--rm", "--publish", "127.0.0.1::6379",
        "--volume", f"{acl}:/etc/redis/users.acl:ro", image,
        "redis-server", "--aclfile", "/etc/redis/users.acl", "--save", "", "--appendonly", "no",
    ).strip()  # fmt: skip
    try:
        port = _run(docker, "port", container, "6379/tcp").splitlines()[0].rsplit(":", 1)[1]
        with redis.Redis(port=int(port), username="kalkan", password=APP_PASSWORD) as app:
            deadline = time.monotonic() + 30
            while True:
                try:
                    app.ping()
                    break
                except redis.ConnectionError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.2)
        yield f"127.0.0.1:{port}"
    finally:
        subprocess.run([docker, "rm", "--force", container], capture_output=True, check=False)  # noqa: S603


def _url(address: str, user: str, password: str) -> str:
    return f"redis://{user}:{password}@{address}/0"


@pytest.fixture
def app_url(address: str) -> str:
    return _url(address, "kalkan", APP_PASSWORD)


@pytest.fixture
def browser_url(address: str) -> str:
    return _url(address, "browser", BROWSER_PASSWORD)


@pytest.fixture
def pools() -> Iterator[list[redis.ConnectionPool]]:
    """Connection pools opened by a test, disconnected after it."""
    opened: list[redis.ConnectionPool] = []
    yield opened
    for pool in opened:
        pool.disconnect()


@pytest.fixture
def client(pools: list[redis.ConnectionPool]) -> Callable[..., redis.Redis]:
    def make(url: str | None = None, **parameters: Any) -> redis.Redis:
        made = redis.Redis.from_url(url) if url else redis.Redis(**parameters)
        pools.append(made.connection_pool)
        return made

    return make


@pytest.fixture
def broker(pools: list[redis.ConnectionPool]) -> Callable[..., RedisBroker]:
    def make(url: str, **options: Any) -> RedisBroker:
        made = RedisBroker(url=url, **options)
        pools.append(made.client.connection_pool)
        return made

    return make


@pytest.fixture
def app(app_url: str, client: Callable[..., redis.Redis]) -> Iterator[redis.Redis]:
    """worker-default's view (user kalkan): empties the database and the ACL log around a test."""
    made = client(app_url)
    made.flushall()
    made.acl_log_reset()
    yield made
    made.flushall()


def _denials(app: redis.Redis) -> list[str]:
    return [f"{entry['reason']}: {entry['object']}" for entry in app.acl_log()]


def _message(queue: str, actor: str, *args: object) -> Message[object]:
    return Message(queue_name=queue, actor_name=actor, args=args, kwargs={}, options={})


def test_only_the_two_users_can_connect(
    address: str, app: redis.Redis, client: Callable[..., redis.Redis]
) -> None:
    host, port = address.split(":")
    with pytest.raises(redis.AuthenticationError):
        client(host=host, port=int(port)).ping()
    with pytest.raises(redis.AuthenticationError):
        client(host=host, port=int(port), username="browser", password="wrong").ping()
    assert app.ping()


def test_the_browser_user_runs_every_dramatiq_broker_command(
    app: redis.Redis,
    app_url: str,
    browser_url: str,
    broker: Callable[..., RedisBroker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Lua stack size is cached per process; the browser user must be able to measure it.
    monkeypatch.setattr(RedisBroker, "_max_unpack_size_val", None)
    issuer = broker(app_url)
    browser = broker(browser_url, maintenance_chance=0)
    assert browser.scripts["maxstack"]() > 0
    for n in range(3):
        issuer.enqueue(_message("capture", "browser_capture", n))

    # Fetch (prefetch 3), ack, nack (dead-letter queue) and requeue.
    consumer = browser.consume("capture", prefetch=3, timeout=100)
    first, second, third = (next(consumer) for _ in range(3))
    assert first is not None
    assert second is not None
    assert third is not None
    consumer.ack(first)
    consumer.nack(second)
    consumer.requeue([third])
    assert app.llen("dramatiq:capture") == 1
    assert app.zcard("dramatiq:capture.XQ") == 1

    # Maintenance: a worker that fetched a message and died. Another browser process with
    # maintenance on every call puts the message back, drops the dead worker's heartbeat and
    # expires the dead-lettered message (dead_message_ttl=0).
    dead = browser.consume("capture", prefetch=1, timeout=100)
    assert next(dead) is not None
    time.sleep(0.05)
    survivor = broker(
        browser_url,
        maintenance_chance=MAINTENANCE_SCALE,
        heartbeat_timeout=1,
        dead_message_ttl=0,
    )
    revived = next(survivor.consume("capture", prefetch=1, timeout=100))
    assert revived is not None
    assert revived.options["redis_message_id"] == third.options["redis_message_id"]
    assert app.zcard("dramatiq:capture.XQ") == 0
    assert app.zscore("dramatiq:__heartbeats__", browser.broker_id) is None

    # Delayed messages and messages for worker-default's queues, sent by name.
    browser.enqueue(_message("capture", "browser_capture", 9), delay=60_000)
    browser.enqueue(_message("persist", "persist_capture", "job"))
    assert app.hlen("dramatiq:capture.DQ.msgs") == 1
    assert app.hlen("dramatiq:persist.msgs") == 1
    assert _denials(app) == []


def test_a_browser_worker_consumes_defers_retries_and_hands_off(
    app: redis.Redis,
    app_url: str,
    browser_url: str,
    broker: Callable[..., RedisBroker],
    client: Callable[..., redis.Redis],
) -> None:
    browser = broker(browser_url, maintenance_chance=MAINTENANCE_SCALE)
    counter = client(browser_url)

    def capture(job_id: str, deferred: bool) -> None:
        if not deferred:
            # Fail closed: a new delayed message, no retry used.
            capture_actor.send_with_options(args=(job_id, True), delay=50)
            return
        # The daily proxy byte counter, as kalkan.workers.capture keeps it.
        for _ in range(2):
            used = int(counter.get(COUNTER) or 0)
            counter.incrby(COUNTER, 750)
            counter.expire(COUNTER, 3600)
        assert used == 750
        browser.enqueue(_message("persist", "persist_capture", job_id))

    def broken(job_id: str) -> None:
        raise RuntimeError(job_id)

    capture_actor = dramatiq.actor(
        broker=browser, actor_name="browser_capture", queue_name="capture", max_retries=0
    )(capture)
    dramatiq.actor(
        broker=browser, actor_name="broken_capture", queue_name="capture",
        max_retries=1, min_backoff=10, max_backoff=10,
    )(broken)  # fmt: skip

    issuer = broker(app_url)
    issuer.enqueue(_message("capture", "browser_capture", "job-1", False))
    issuer.enqueue(_message("capture", "broken_capture", "job-2"))

    worker = Worker(browser, queues={"capture"}, worker_threads=1, worker_timeout=100)
    worker.start()
    try:
        deadline = time.monotonic() + 20
        # job-1 handed off after its deferral; job-2 dead-lettered after its one retry.
        while app.hlen("dramatiq:persist.msgs") < 1 or app.zcard("dramatiq:capture.XQ") < 1:
            assert time.monotonic() < deadline, _denials(app)
            time.sleep(0.05)
    finally:
        worker.stop()

    dead = [Message.decode(cast(bytes, data)) for data in app.hvals("dramatiq:capture.XQ.msgs")]
    assert [(m.actor_name, m.options.get("retries")) for m in dead] == [("broken_capture", 1)]
    assert int(app.get(COUNTER) or 0) == 1500
    assert 0 < app.ttl(COUNTER) <= 3600
    assert _denials(app) == []


@pytest.mark.parametrize(
    "command",
    [
        ("GET", "capture:job:00000000-0000-0000-0000-000000000000"),
        ("SET", "capture:pending:x", "1"),
        ("DEL", "capture:job:x"),
        ("HGETALL", "dramatiq-results:x"),
        ("SET", COUNTER, "0"),  # the counter: GET, INCRBY and EXPIRE only
        ("PERSIST", COUNTER),
        ("MULTI",),
        ("GET", "dramatiq:capture"),  # queue keys: only through the scripts' commands
        ("INCRBY", "dramatiq:x", "1"),
        ("EVAL", "return 1", "0"),
        ("SCRIPT", "FLUSH"),
        ("FLUSHALL",),
        ("FLUSHDB",),
        ("CONFIG", "GET", "*"),
        ("ACL", "LIST"),
        ("CLIENT", "LIST"),
        ("INFO",),
        ("MODULE", "LIST"),
        ("SLOWLOG", "GET"),
        ("PUBLISH", "dramatiq", "x"),
        ("SELECT", "1"),
    ],
    ids=lambda command: " ".join(command[:2]),
)
def test_the_browser_user_reaches_nothing_else(
    app: redis.Redis, browser_url: str, client: Callable[..., redis.Redis], command: tuple[str, ...]
) -> None:
    with pytest.raises(redis.exceptions.NoPermissionError):
        client(browser_url).execute_command(*command)


@pytest.mark.parametrize(
    "script",
    [
        "return redis.call('get', 'capture:job:x')",
        "return redis.call('hset', 'dramatiq-results:x', 'f', 'v')",
        "return redis.call('get', 'dramatiq:capture')",
        "return redis.call('flushall')",
    ],
)
def test_scripts_run_with_the_browser_users_permissions(
    app: redis.Redis, browser_url: str, client: Callable[..., redis.Redis], script: str
) -> None:
    browser = client(browser_url)
    sha = browser.script_load(script)
    with pytest.raises(redis.ResponseError, match=r"(?i)permission"):
        browser.evalsha(sha, 0)
