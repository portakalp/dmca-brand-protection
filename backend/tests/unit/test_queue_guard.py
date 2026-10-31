"""QueueGuard: a message runs only an actor of its own queue (architecture.md §24)."""

from __future__ import annotations

from typing import Any

import dramatiq
from dramatiq.broker import MessageProxy
from dramatiq.brokers.stub import StubBroker

from kalkan.workers.broker import QueueGuard


def _broker() -> tuple[StubBroker, list[str]]:
    broker = StubBroker()
    broker.add_middleware(QueueGuard())
    ran: list[str] = []

    @dramatiq.actor(broker=broker, queue_name="persist", actor_name="persist_capture")
    def persist_capture(job_id: str) -> None:
        ran.append(f"persist:{job_id}")

    @dramatiq.actor(broker=broker, queue_name="discovery", actor_name="czds_scan")
    def czds_scan() -> None:
        ran.append("czds")

    return broker, ran


def _run(broker: StubBroker) -> None:
    worker = dramatiq.Worker(broker, worker_timeout=50)
    worker.start()
    try:
        broker.join("persist", fail_fast=False)
        broker.join("discovery", fail_fast=False)
        worker.join()
    finally:
        worker.stop()


def _forge(broker: StubBroker, actor: str, options: dict[str, Any] | None = None) -> None:
    broker.enqueue(
        dramatiq.Message(
            queue_name="persist", actor_name=actor, args=("job",), kwargs={}, options=options or {}
        )
    )


def test_a_message_naming_another_queues_actor_is_skipped() -> None:
    broker, ran = _broker()
    broker.enqueue(
        dramatiq.Message(
            queue_name="persist", actor_name="czds_scan", args=(), kwargs={}, options={}
        )
    )
    _run(broker)
    assert ran == []


def test_follow_up_actors_are_stripped_on_untrusted_queues() -> None:
    broker, ran = _broker()
    chained = {"queue_name": "discovery", "actor_name": "czds_scan", "args": [], "kwargs": {}}
    _forge(broker, "persist_capture", {"pipe_target": chained, "on_success": "czds_scan"})
    _run(broker)
    assert ran == ["persist:job"]  # persist ran, nothing was chained after it


def test_trusted_queues_keep_their_options() -> None:
    message: dramatiq.Message[Any] = dramatiq.Message(
        queue_name="discovery",
        actor_name="czds_scan",
        args=(),
        kwargs={},
        options={"on_success": "x"},
    )
    broker, _ = _broker()
    QueueGuard().before_process_message(broker, MessageProxy(message))
    assert message.options == {"on_success": "x"}
