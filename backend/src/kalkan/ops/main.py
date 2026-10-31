"""ASGI entry point: ``uvicorn kalkan.ops.main:app`` (port 8081, §19)."""

from kalkan.config import get_settings
from kalkan.logging import configure_logging
from kalkan.ops.app import create_app
from kalkan.sentry import init_sentry

SERVICE = "ops"

settings = get_settings()
settings.require_for(SERVICE)
configure_logging(SERVICE, settings.log_level)
init_sentry(settings, SERVICE)

app = create_app(settings)
