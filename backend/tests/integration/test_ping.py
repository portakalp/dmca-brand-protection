"""A message goes through Redis to a Dramatiq worker and its result comes back."""

from __future__ import annotations

from dramatiq import Worker
from dramatiq.brokers.redis import RedisBroker

from kalkan.workers.ping import ping
from kalkan.workers.specs import WORKERS


def test_ping_round_trip_through_redis(redis_broker: RedisBroker) -> None:
    worker = Worker(redis_broker, queues=set(WORKERS["default"].queues), worker_threads=1)
    worker.start()
    try:
        message = ping.send("hello")
        assert message.get_result(block=True, timeout=10_000) == "pong:hello"
    finally:
        worker.stop()
