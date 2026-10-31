"""Whitelist matching (architecture.md §9). Pure functions.

An entry is a host; it covers that host and every subdomain of it, nothing else:
``mintera.pl`` covers ``www.mintera.pl`` but not ``mintera.pl.evil.com``, and
``partners.mintera-referrals.com`` does not cover ``mintera-referrals.com``. Public suffixes and
the shared platform roots of the PSL's private section (``github.io``, ``pages.dev``) cannot
be entries, because they would cover everyone's sites. Official domains always count as
whitelisted.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from kalkan.discovery.psl import split_host
from kalkan.domain.hostnames import InvalidHostnameError, normalize_hostname


@dataclass(frozen=True)
class WhitelistRule:
    pattern: str  # punycode host
    brand_id: uuid.UUID | None  # None: every brand of the customer
    official: bool = False


def validate_pattern(value: str) -> str:
    """The pattern as a punycode host, or :class:`InvalidHostnameError`."""
    host = normalize_hostname(value)
    if host.startswith("www."):
        host = host[4:]
    _, label, suffix = split_host(host)
    if not label or not suffix:
        raise InvalidHostnameError(
            f"{value!r} is a public suffix or a shared platform root, which would cover "
            "other people's sites"
        )
    return host


@lru_cache(maxsize=65_536)
def _registered(host: str) -> str:
    _, label, suffix = split_host(host)
    return f"{label}.{suffix}"


def covers(pattern: str, host: str) -> bool:
    """``host`` is ``pattern`` or a subdomain of it, without crossing a public suffix.

    ``amazonaws.com`` must not cover ``evil.s3.amazonaws.com``: ``s3.amazonaws.com`` is a
    shared platform root, so that host is someone else's registered domain.
    """
    if host != pattern and not host.endswith("." + pattern):
        return False
    return _registered(host) == _registered(pattern)


def find_rule(
    host: str, brand_id: uuid.UUID, rules: Iterable[WhitelistRule]
) -> WhitelistRule | None:
    """The rule that covers ``host`` for ``brand_id``; official domains are preferred."""
    found = [
        rule
        for rule in rules
        if (rule.brand_id is None or rule.brand_id == brand_id) and covers(rule.pattern, host)
    ]
    found.sort(key=lambda rule: (not rule.official, rule.pattern))
    return found[0] if found else None
