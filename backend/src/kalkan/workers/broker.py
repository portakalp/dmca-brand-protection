"""Dramatiq broker setup (architecture.md §7).

Actors bind to the global broker when their module is imported, so :func:`setup_broker`
must run before any actor module is imported.
"""

from __future__ import annotations

import dramatiq
from dramatiq.broker import Broker, MessageProxy
from dramatiq.brokers.redis import RedisBroker
from dramatiq.common import q_name
from dramatiq.middleware import CurrentMessage, Middleware, SkipMessage
from dramatiq.results import Results
from dramatiq.results.backends.redis import RedisBackend

from kalkan.logging import get_logger
from kalkan.scanning.capture_contract import PERSIST_QUEUE

_broker: RedisBroker | None = None

log = get_logger(__name__)


class QueueGuard(Middleware):
    """A message runs only an actor of the queue it was taken from, and messages on a queue an
    untrusted process can write to carry no follow-up actors (§24).

    worker-browser may write to ``persist``. Dramatiq resolves a message's actor by name alone,
    so without this a forged message there could start any worker-default actor, or chain one
    after ``persist_capture`` through the pipeline and callback options.
    """

    UNTRUSTED_QUEUES = frozenset({PERSIST_QUEUE})
    FOLLOW_UPS = ("pipe_target", "pipe_ignore", "on_success", "on_failure")

    def before_process_message(self, broker: Broker, message: MessageProxy) -> None:
        queue = q_name(message.queue_name)
        actor = broker.get_actor(message.actor_name)
        if actor.queue_name != queue:
            log.warning(
                "broker.message_on_wrong_queue",
                queue=queue,
                actor=message.actor_name,
                message_id=message.message_id,
            )
            raise SkipMessage("the actor does not belong to this queue")
        if queue in self.UNTRUSTED_QUEUES:
            for option in self.FOLLOW_UPS:
                message.options.pop(option, None)


def setup_broker(redis_url: str, *, prometheus: bool, results: bool = True) -> RedisBroker:
    """Create the process-wide Redis broker once and make it Dramatiq's global broker.

    ``prometheus`` adds the metrics middleware; only worker processes enable it, because its
    metrics are created when a worker process boots. ``results`` adds the result backend;
    worker-browser leaves it out, because its Redis user cannot reach the result keys.
    """
    global _broker
    if _broker is not None:
        return _broker

    broker = RedisBroker(url=redis_url)
    if prometheus:
        from dramatiq.middleware.prometheus import Prometheus

        broker.add_middleware(Prometheus())
    if results:
        broker.add_middleware(Results(backend=RedisBackend(url=redis_url)))
    # Lets an actor see its retry count, e.g. to act on its last attempt.
    broker.add_middleware(CurrentMessage())
    broker.add_middleware(QueueGuard())
    dramatiq.set_broker(broker)
    _broker = broker
    return broker


def final_attempt(max_retries: int) -> bool:
    """Whether the message being processed is its actor's last try (needs CurrentMessage)."""
    message = CurrentMessage.get_current_message()
    retries = int(message.options.get("retries", 0)) if message is not None else max_retries
    return retries >= max_retries
