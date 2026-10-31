"""Boot code shared by the Dramatiq entry modules of each worker process."""

from __future__ import annotations

import importlib

from dramatiq.brokers.redis import RedisBroker
from sentry_sdk.integrations.dramatiq import DramatiqIntegration

from kalkan.ai.queue import set_ai_enabled
from kalkan.config import ConfigError, get_settings
from kalkan.logging import configure_logging, get_logger
from kalkan.sentry import init_sentry
from kalkan.workers.broker import setup_broker
from kalkan.workers.specs import WORKERS


def boot_worker(name: str, *, results: bool = True) -> RedisBroker:
    """Configure this worker process and import the actors it owns.

    ``results`` keeps Dramatiq's result backend (see :func:`setup_broker`).
    """
    spec = WORKERS[name]
    settings = get_settings()
    settings.require_for(spec.service)
    configure_logging(spec.service, settings.log_level)
    init_sentry(settings, spec.service, integrations=[DramatiqIntegration()])
    if settings.redis_url is None:  # pragma: no cover - require_for already checked it
        raise ConfigError("REDIS_URL is required")
    broker = setup_broker(settings.redis_url, prometheus=True, results=results)
    # Where this process sends AI work: worker-ai, or the stand-ins when AI is off.
    set_ai_enabled(settings.use_ai)
    get_logger(__name__).info("worker.ai_mode", service=spec.service, ai=settings.use_ai)
    for module in spec.actor_modules:
        importlib.import_module(module)
    return broker
