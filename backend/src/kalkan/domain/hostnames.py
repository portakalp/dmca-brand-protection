"""Host name validation for official domains and whitelist patterns.

Patterns are stored as lower-case punycode hosts. Public-suffix and shared-platform checks
live in the whitelist matcher.
"""

from __future__ import annotations

import re

import idna

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
MAX_HOST_LENGTH = 253


class InvalidHostnameError(ValueError):
    pass


def normalize_hostname(value: str) -> str:
    """Return ``value`` as a lower-case punycode host, or raise :class:`InvalidHostnameError`."""
    host = value.strip().lower().rstrip(".")
    if not host:
        raise InvalidHostnameError("empty host name")
    if any(char in host for char in "/:@?# \t*"):
        raise InvalidHostnameError(f"not a bare host name: {value!r}")
    try:
        # IDNA 2008 with UTS 46 mapping (non-transitional), as registries and CT logs use.
        ascii_host = idna.encode(host, uts46=True).decode("ascii")
    except (idna.IDNAError, UnicodeError) as exc:
        raise InvalidHostnameError(f"invalid international host name: {value!r}") from exc
    labels = ascii_host.split(".")
    if len(labels) < 2 or len(ascii_host) > MAX_HOST_LENGTH:
        raise InvalidHostnameError(f"not a domain name: {value!r}")
    if not all(_LABEL.match(label) for label in labels):
        raise InvalidHostnameError(f"invalid characters in host name: {value!r}")
    if labels[-1].isdigit():
        raise InvalidHostnameError(f"IP addresses are not host names: {value!r}")
    return ascii_host
