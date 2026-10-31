"""RDAP for domains and IPs with whoisit (architecture.md §9; K22).

- Servers come only from IANA's bootstrap files, https only (whoisit's default). The
  registry's ``related`` link is not followed, because that URL comes from the response, not
  the bootstrap; registry responses already carry the registrar and its abuse contact.
  Redirects are followed only to the RIRs' own https RDAP servers; HTTP 429 is not retried
  inside the request (the job retries later) and every request times out after 6 seconds.
- The bootstrap is shared through Redis and refreshed daily: a fresh copy is downloaded into
  a separate object first, so a failed refresh keeps the old data.
- Every RDAP server has its own token bucket in Redis (2 requests per second, bursts of 5);
  a request waits up to 10 seconds for a token, then gives up as throttled.
- IP and ASN results are cached for 7 days; non-public IPs are never queried.
- A TLD without an RDAP service (e.g. .com.tr, .ru, .de) raises :class:`RdapUnavailableError`.

whoisit talks HTTP through ``requests``, so tests mock it with ``responses``.
"""

from __future__ import annotations

import ipaddress
import json
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import requests
import whoisit
import whoisit.utils
from requests.adapters import HTTPAdapter
from whoisit.bootstrap import Bootstrap
from whoisit.errors import (
    BootstrapError,
    QueryError,
    ResourceDoesNotExist,
    UnsupportedError,
)

from kalkan.domain.emails import clean_email
from kalkan.logging import get_logger

BOOTSTRAP_KEY = "rdap:bootstrap"
BOOTSTRAP_MAX_AGE = 24 * 3600
# The shared copy outlives its freshness, so an IANA outage does not leave workers without one.
BOOTSTRAP_CACHE_SECONDS = 7 * 24 * 3600
IP_CACHE_PREFIX = "rdap:ip:"
ASN_CACHE_PREFIX = "rdap:asn:"
RESULT_CACHE_SECONDS = 7 * 24 * 3600
RATE_PREFIX = "rdap:rate:"
RATE_PER_SECOND = 2.0
BURST = 5
MAX_WAIT_SECONDS = 10.0
HTTP_TIMEOUT_SECONDS = 6
# The RIRs refer IP and ASN queries to each other; nothing else may redirect a query.
RIR_HOSTS = frozenset(
    {"rdap.arin.net", "rdap.db.ripe.net", "rdap.apnic.net", "rdap.lacnic.net", "rdap.afrinic.net"}
)

# whoisit passes this timeout to every request it makes.
whoisit.utils.http_timeout = HTTP_TIMEOUT_SECONDS

log = get_logger(__name__)

# whoisit keeps its bootstrap in one module-level object shared by the worker threads.
_bootstrap_lock = threading.Lock()


class RdapUnavailableError(Exception):
    """The TLD has no RDAP service in IANA's bootstrap."""


class RdapThrottledError(Exception):
    """The server's rate limit did not leave a slot in time; try again later."""


class Cache(Protocol):
    def get(self, name: str) -> Any: ...

    def set(self, name: str, value: str, *, ex: int) -> Any: ...


class TokenBucket(Protocol):
    def acquire(self, key: str, *, rate: float, burst: int) -> float:
        """Take a token; returns 0, or the seconds to wait before asking again."""
        ...


# Atomic token bucket: KEYS[1] bucket; ARGV rate per second, burst, now in ms.
_BUCKET_SCRIPT = """
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'at')
local tokens = tonumber(state[1]) or burst
local at = tonumber(state[2]) or now
tokens = math.min(burst, tokens + math.max(0, now - at) * rate / 1000)
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  wait = math.ceil((1 - tokens) * 1000 / rate)
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'at', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(burst * 1000 / rate) + 1000)
return wait
"""


class RedisTokenBucket:
    def __init__(self, redis: Any, clock: Callable[[], float] = time.time) -> None:
        self._script = redis.register_script(_BUCKET_SCRIPT)
        self._clock = clock

    def acquire(self, key: str, *, rate: float, burst: int) -> float:
        wait_ms = self._script(keys=[key], args=[rate, burst, int(self._clock() * 1000)])
        return int(wait_ms) / 1000


def _rate_limited(exc: QueryError) -> bool:
    """whoisit retries HTTP 429 itself, then reports it as a generic query error."""
    return getattr(exc, "status_code", 0) == 429 or "429" in str(exc)


class RdapSession(requests.Session):
    """No retries inside a request, and redirects only to the RIRs' https RDAP servers."""

    def __init__(self) -> None:
        super().__init__()
        self.mount("https://", HTTPAdapter(max_retries=0))
        self.max_redirects = 3

    def get_redirect_target(self, resp: requests.Response) -> str | None:
        target = super().get_redirect_target(resp)
        if target is None:
            return None
        parts = urlsplit(urljoin(resp.url, target))
        if parts.scheme == "https" and (parts.hostname or "") in RIR_HOSTS:
            return target
        log.warning("rdap.redirect_refused", origin=urlsplit(resp.url).hostname)
        return None


def _age_seconds(data: str) -> float:
    return time.time() - float(json.loads(data).get("timestamp", 0))


class RdapClient:
    def __init__(
        self,
        cache: Cache,
        bucket: TokenBucket,
        *,
        sleep: Callable[[float], None] = time.sleep,
        session: requests.Session | None = None,
    ) -> None:
        self._cache = cache
        self._bucket = bucket
        self._sleep = sleep
        self._session = session or RdapSession()

    def ensure_bootstrap(self) -> None:
        with _bootstrap_lock:
            if whoisit.is_bootstrapped() and not whoisit.bootstrap_is_older_than(1):
                return
            data = self._bootstrap_data()
            if data is None:
                if whoisit.is_bootstrapped():
                    log.warning("rdap.bootstrap_refresh_failed", keeping="stale copy")
                    return
                raise BootstrapError("no RDAP bootstrap data could be loaded")
            # The swap takes milliseconds; a query in another thread that hits it retries.
            whoisit.clear_bootstrapping()
            whoisit.load_bootstrap_data(data)

    def _bootstrap_data(self) -> str | None:
        """A fresh shared copy, else a new download, else the stale shared copy, else None."""
        cached = self._cache.get(BOOTSTRAP_KEY)
        if isinstance(cached, bytes):
            cached = cached.decode()
        if cached is not None and _age_seconds(cached) < BOOTSTRAP_MAX_AGE:
            return str(cached)
        try:
            fresh = Bootstrap(session=self._session)
            fresh.bootstrap()
            data = str(fresh.save_bootstrap_data())
        except Exception:  # fall back to what we have
            log.warning("rdap.bootstrap_download_failed", exc_info=True)
            return str(cached) if cached is not None else None
        self._cache.set(BOOTSTRAP_KEY, data, ex=BOOTSTRAP_CACHE_SECONDS)
        return data

    def _endpoint(self, query_type: str, value: str) -> str:
        try:
            _, url, _ = whoisit.build_query(query_type=query_type, query_value=value)
        except UnsupportedError as exc:
            raise RdapUnavailableError(str(exc)) from exc
        return str(url)

    def _throttle(self, url: str) -> None:
        key = RATE_PREFIX + urlsplit(url).netloc.lower()
        waited = 0.0
        while (wait := self._bucket.acquire(key, rate=RATE_PER_SECOND, burst=BURST)) > 0:
            if waited + wait > MAX_WAIT_SECONDS:
                raise RdapThrottledError(f"no RDAP slot for {urlsplit(url).netloc}")
            self._sleep(wait)
            waited += wait

    def domain(self, name: str) -> dict[str, Any] | None:
        """The registry's raw RDAP JSON for ``name``; None when the registry does not know it."""
        self.ensure_bootstrap()
        self._throttle(self._endpoint("domain", name))
        try:
            response: dict[str, Any] = whoisit.domain(
                name, raw=True, follow_related=False, session=self._session
            )
        except ResourceDoesNotExist:
            return None
        except UnsupportedError as exc:
            raise RdapUnavailableError(str(exc)) from exc
        except QueryError as exc:
            if _rate_limited(exc):
                raise RdapThrottledError(str(exc)) from exc
            raise
        return response

    def ip(self, address: str) -> dict[str, Any] | None:
        """The RIR's raw RDAP JSON for the network of ``address``; None for non-public IPs."""
        if not ipaddress.ip_address(address).is_global:
            return None
        return self._cached(
            IP_CACHE_PREFIX + address,
            "ip",
            address,
            lambda: whoisit.ip(address, raw=True, session=self._session),
        )

    def asn(self, number: int) -> dict[str, Any] | None:
        """The RIR's raw RDAP JSON for an autonomous system."""
        return self._cached(
            ASN_CACHE_PREFIX + str(number),
            "asn",
            str(number),
            lambda: whoisit.asn(number, raw=True, session=self._session),
        )

    def _cached(
        self, key: str, query_type: str, value: str, query: Callable[[], Any]
    ) -> dict[str, Any] | None:
        cached = self._cache.get(key)
        if cached is not None:
            stored: dict[str, Any] | None = json.loads(cached)
            return stored
        self.ensure_bootstrap()
        self._throttle(self._endpoint(query_type, value))
        try:
            response: dict[str, Any] | None = query()
        except ResourceDoesNotExist:
            response = None
        except QueryError as exc:
            if _rate_limited(exc):
                raise RdapThrottledError(str(exc)) from exc
            raise
        self._cache.set(key, json.dumps(response), ex=RESULT_CACHE_SECONDS)
        return response


# --- parsing the raw JSON ---------------------------------------------------------------------


def _vcard(entity: dict[str, Any]) -> dict[str, str]:
    """``fn``, ``email`` and ``kind`` from an entity's jCard."""
    fields: dict[str, str] = {}
    card = entity.get("vcardArray")
    if not isinstance(card, list) or len(card) < 2 or not isinstance(card[1], list):
        return fields
    for item in card[1]:
        if isinstance(item, list) and len(item) >= 4 and isinstance(item[3], str):
            name = str(item[0]).lower()
            if name in ("fn", "email", "kind") and item[3].strip() and name not in fields:
                fields[name] = item[3].strip()
    return fields


def _entities(data: dict[str, Any]) -> list[dict[str, Any]]:
    """All entities, nested ones included, depth first."""
    found: list[dict[str, Any]] = []
    for entity in data.get("entities") or []:
        if isinstance(entity, dict):
            found.append(entity)
            found += _entities(entity)
    return found


def _abuse_email(entities: list[dict[str, Any]]) -> str | None:
    for entity in entities:
        if "abuse" in (entity.get("roles") or []):
            email = clean_email(_vcard(entity).get("email"))
            if email:
                return email
    return None


def parse_domain(data: dict[str, Any]) -> dict[str, Any]:
    """Registrar, its IANA id and abuse e-mail, registration date and name servers."""
    registrar = next(
        (e for e in data.get("entities") or [] if "registrar" in (e.get("roles") or [])), None
    )
    iana_id = None
    name = None
    abuse = None
    if registrar is not None:
        name = _vcard(registrar).get("fn")
        for public_id in registrar.get("publicIds") or []:
            if "iana" in str(public_id.get("type", "")).lower():
                identifier = str(public_id.get("identifier", "")).strip()
                iana_id = int(identifier) if identifier.isdigit() else None
        abuse = _abuse_email(_entities(registrar))
    # Some registries list the registrar's abuse contact next to it rather than inside it.
    abuse = abuse or _abuse_email(
        [e for e in data.get("entities") or [] if "registrant" not in (e.get("roles") or [])]
    )
    registered_at = next(
        (
            event.get("eventDate")
            for event in data.get("events") or []
            if event.get("eventAction") == "registration"
        ),
        None,
    )
    nameservers = sorted(
        {
            str(ns.get("ldhName", "")).rstrip(".").lower()
            for ns in data.get("nameservers") or []
            if ns.get("ldhName")
        }
    )
    return {
        "registrar": name,
        "registrar_iana_id": iana_id,
        "registrar_abuse_email": abuse,
        "registered_at": registered_at,
        "nameservers": nameservers,
        "status": list(data.get("status") or []),
    }


def parse_ip(data: dict[str, Any]) -> dict[str, Any]:
    """Network owner, country and abuse e-mail of an IP network."""
    entities = _entities(data)
    registrants = [e for e in entities if "registrant" in (e.get("roles") or [])]
    # Prefer an organisation's name over a person's or a bare handle.
    registrants.sort(key=lambda e: (_vcard(e).get("kind") != "org", not _vcard(e).get("fn")))
    org = next((_vcard(e).get("fn") for e in registrants if _vcard(e).get("fn")), None)
    return {
        "org": org or data.get("name"),
        "country": data.get("country"),
        "abuse_email": _abuse_email(entities),
        "network": data.get("handle"),
    }


def parse_asn(data: dict[str, Any]) -> dict[str, Any]:
    """Holder and abuse e-mail of an autonomous system."""
    parsed = parse_ip(data)
    return {"org": parsed["org"], "abuse_email": parsed["abuse_email"]}
