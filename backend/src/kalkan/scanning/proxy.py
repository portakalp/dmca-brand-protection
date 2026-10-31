"""Proxy templates of ``PROXY_MAP`` and the choice of a job's proxy (architecture.md §10, §26).

A ``PROXY_MAP`` value is a URL template ``http://user:pass@host:port``: http only (Chromium
passes credentials only to HTTP proxies), credentials percent-encoded, an explicit port and
nothing after it. ``{session}`` may appear anywhere in the user or password part; each job
attempt replaces it with a fresh token that both profiles share, so the provider keeps one exit
address for the attempt (a sticky session, K93).

Credentials are not logged, not stored and not part of ``repr``; only the country and
``host:port`` are. The browser never holds them either: ``proxy_forwarder`` adds them to the
requests it passes upstream (K93).
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import SplitResult, unquote, urlsplit

from pydantic import SecretStr

if TYPE_CHECKING:  # worker-default validates PROXY_MAP without Playwright installed
    from playwright.sync_api import ProxySettings

SESSION = "{session}"
# A token every provider accepts as a session id (8 lower-case hex characters).
SESSION_BYTES = 4


class ProxyTemplateError(ValueError):
    """A malformed ``PROXY_MAP`` value. The message never contains the value."""


@dataclass(frozen=True)
class ProxyChoice:
    """The proxy of one job attempt."""

    country: str
    server: str  # http://host:port, no credentials
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    @property
    def endpoint(self) -> str:
        """``host:port``: what logs and metadata may show of the proxy."""
        return self.server.removeprefix("http://")

    def playwright(self) -> ProxySettings:
        """Playwright's ``proxy`` option for a proxy without credentials."""
        if self.username is not None:
            raise ValueError("a proxy with credentials goes through proxy_forwarder")
        return {"server": self.server}


def new_session() -> str:
    return secrets.token_hex(SESSION_BYTES)


def _split(template: str, session: str) -> SplitResult:
    if any(char.isspace() for char in template):
        raise ProxyTemplateError("a proxy URL must not contain whitespace")
    userinfo = template.partition("://")[2].rpartition("@")[0]
    if template.count(SESSION) != userinfo.count(SESSION):
        raise ProxyTemplateError("{session} may only appear in the user or password part")
    filled = template.replace(SESSION, session)
    if "{" in filled or "}" in filled:
        raise ProxyTemplateError("unknown placeholder in a proxy URL (only {session})")
    try:
        parts = urlsplit(filled)
        port = parts.port
    except ValueError:
        raise ProxyTemplateError("a proxy URL needs a valid host and port") from None
    if parts.scheme != "http":
        raise ProxyTemplateError("a proxy URL must use http:// (credentials need an HTTP proxy)")
    if not parts.hostname or port is None:
        raise ProxyTemplateError("a proxy URL needs a host and an explicit port")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ProxyTemplateError("a proxy URL ends after host:port (percent-encode credentials)")
    if parts.password is not None and parts.username in (None, ""):
        raise ProxyTemplateError("a proxy URL with a password needs a user name")
    return parts


def validate_template(template: str) -> None:
    """Raise :class:`ProxyTemplateError` for a value ``PROXY_MAP`` must not hold."""
    _split(template, "0" * 2 * SESSION_BYTES)


def parse_template(country: str, template: str, session: str) -> ProxyChoice:
    parts = _split(template, session)
    host = parts.hostname or ""
    if ":" in host:  # an IPv6 literal
        host = f"[{host}]"
    server = f"http://{host}:{parts.port}"
    if not parts.username:
        return ProxyChoice(country=country, server=server)
    return ProxyChoice(
        country=country,
        server=server,
        username=unquote(parts.username),
        password=unquote(parts.password or ""),
    )


def select_proxy(
    proxy_map: Mapping[str, SecretStr],
    country: str | None,
    default_country: str | None,
    *,
    session: str,
) -> ProxyChoice | None:
    """The job country's proxy, else the default country's, else ``None`` (fail closed)."""
    for candidate in (country, default_country):
        if candidate is not None and candidate in proxy_map:
            return parse_template(candidate, proxy_map[candidate].get_secret_value(), session)
    return None
