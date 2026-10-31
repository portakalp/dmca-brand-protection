"""Is a domain behind Cloudflare? (architecture.md §9).

Yes when one of its IPs is in Cloudflare's published ranges or its name servers are
``*.ns.cloudflare.com``. The ranges come from Cloudflare's public API, are kept in Redis
and refreshed daily; a missing copy is fetched on first use. Cloudflare is a known provider,
outside ``net/safe_http.py`` (§10).
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable
from functools import lru_cache
from typing import Any, Protocol

import httpx2

IPS_URL = "https://api.cloudflare.com/client/v4/ips"
RANGES_KEY = "cloudflare:ranges"
NS_SUFFIX = ".ns.cloudflare.com"


class Store(Protocol):
    def get(self, name: str) -> Any: ...

    def set(self, name: str, value: str) -> Any: ...


def fetch_ranges(*, transport: httpx2.BaseTransport | None = None) -> list[str]:
    with httpx2.Client(transport=transport, timeout=10.0) as client:
        response = client.get(IPS_URL)
    response.raise_for_status()
    body = response.json()
    result = body.get("result") or {}
    ranges = [*result.get("ipv4_cidrs", []), *result.get("ipv6_cidrs", [])]
    if not body.get("success") or not ranges:
        raise ValueError("Cloudflare returned no IP ranges")
    for cidr in ranges:
        ipaddress.ip_network(cidr)
    return sorted(ranges)


def refresh_ranges(store: Store, *, transport: httpx2.BaseTransport | None = None) -> int:
    ranges = fetch_ranges(transport=transport)
    store.set(RANGES_KEY, json.dumps(ranges))
    return len(ranges)


def load_ranges(store: Store, *, transport: httpx2.BaseTransport | None = None) -> list[str]:
    cached = store.get(RANGES_KEY)
    if cached is None:
        refresh_ranges(store, transport=transport)
        cached = store.get(RANGES_KEY)
    ranges: list[str] = json.loads(cached)
    return ranges


@lru_cache(maxsize=8)
def _networks(ranges: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return tuple(ipaddress.ip_network(cidr) for cidr in ranges)


def in_ranges(address: str, ranges: Iterable[str]) -> bool:
    ip = ipaddress.ip_address(address)
    return any(ip in network for network in _networks(tuple(ranges)))


def cloudflare_nameserver(name: str) -> bool:
    return name.rstrip(".").lower().endswith(NS_SUFFIX)


def is_cloudflare(ips: Iterable[str], nameservers: Iterable[str], ranges: Iterable[str]) -> bool:
    ranges = tuple(ranges)
    return any(in_ranges(ip, ranges) for ip in ips) or any(
        cloudflare_nameserver(name) for name in nameservers
    )
