"""Helpers shared by tests."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError
from pydantic_settings import SettingsError

from kalkan.config import Settings, get_settings

# Integration tests use their own Redis database so the dev stack's workers (db 0) never
# consume test messages.
TEST_REDIS_DB = 15


def make_settings(**values: Any) -> Settings:
    """Settings from explicit values and the process environment, never from the .env file."""
    return Settings(_env_file=None, **values)


def redis_url_for_tests() -> str:
    try:
        url = get_settings().redis_url
    except (ValidationError, SettingsError):
        url = None
    parts = urlsplit(url or "redis://localhost:6379/0")
    return urlunsplit(parts._replace(path=f"/{TEST_REDIS_DB}"))
