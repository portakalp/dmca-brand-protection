"""ASGI entry point: ``uvicorn kalkan.api.main:app``."""

from kalkan.api.app import create_app
from kalkan.config import get_settings
from kalkan.logging import configure_logging
from kalkan.sentry import init_sentry

SERVICE = "api"

settings = get_settings()
settings.require_for(SERVICE)
configure_logging(SERVICE, settings.log_level)
init_sentry(settings, SERVICE)

app = create_app(settings)
