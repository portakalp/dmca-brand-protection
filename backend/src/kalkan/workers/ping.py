"""Diagnostic actor: proves a message travels through Redis to a worker and back."""

from __future__ import annotations

import dramatiq

from kalkan.logging import get_logger

log = get_logger(__name__)


@dramatiq.actor(queue_name="default", max_retries=0, time_limit=10_000, store_results=True)
def ping(payload: str = "ping") -> str:
    log.info("worker.ping", payload=payload)
    return f"pong:{payload}"
