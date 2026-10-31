"""Sentry error reporting (architecture.md §23). Without ``SENTRY_DSN`` nothing is initialised.

Events carry no local variables and no request bodies, the edge header is scrubbed and
evidence-link tokens are removed from request URLs, so secrets never reach Sentry.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit, urlunsplit

import sentry_sdk
from sentry_sdk.integrations import Integration
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber
from sentry_sdk.types import Event, Hint

from kalkan.config import Settings
from kalkan.logging import redact_path

# Header and variable names scrubbed in addition to Sentry's defaults.
SCRUBBED_KEYS = [
    "x-edge-auth",
    "x_edge_auth",
    "edge_shared_secret",
    "kalkan_session",
    "smtp_url",
]


def _redact_request_url(event: Event, _: Hint) -> Event:
    request = event.get("request")
    url = request.get("url") if isinstance(request, dict) else None
    if isinstance(request, dict) and isinstance(url, str):
        parts = urlsplit(url)
        request["url"] = urlunsplit(parts._replace(path=redact_path(parts.path)))
    return event


def init_sentry(
    settings: Settings, service: str, *, integrations: Sequence[Integration] = ()
) -> bool:
    """Initialise Sentry for this process. Returns whether Sentry is active."""
    if settings.sentry_dsn is None:
        return False
    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.env,
        release=settings.git_sha,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        event_scrubber=EventScrubber(denylist=DEFAULT_DENYLIST + SCRUBBED_KEYS, recursive=True),
        before_send=_redact_request_url,
        traces_sample_rate=0.0,
        integrations=list(integrations),
    )
    sentry_sdk.set_tag("service", service)
    return True
