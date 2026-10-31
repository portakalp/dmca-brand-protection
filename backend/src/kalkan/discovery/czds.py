"""ICANN CZDS zone files (architecture.md §8).

Daily: authenticate, list the download links of approved TLDs, stream each zone, gunzip it
on the fly without writing to disk, take the owner names of NS records and match them.
Names that match and are not yet in the database become candidates. With an empty
``CZDS_TLDS`` every approved TLD is scanned except .com and .net, which only run when
listed (K59); ``*`` scans every approved TLD, .com and .net included (K106). Without
credentials the job is skipped.

CZDS is a known provider API, so it does not go through ``net/safe_http.py`` (§10).
"""

from __future__ import annotations

import time
import uuid
import zlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx2

from kalkan.config import Settings
from kalkan.db.base import utcnow
from kalkan.db.session import Database
from kalkan.discovery.catalog import MatcherCache
from kalkan.discovery.matcher import Matcher, MatchResult
from kalkan.discovery.metrics import metrics
from kalkan.discovery.normalize import HostView
from kalkan.discovery.record import Sighting, record_discovery
from kalkan.domain.enums import SightingSource
from kalkan.logging import get_logger

AUTH_URL = "https://account-api.icann.org/api/authenticate"
LINKS_URL = "https://czds-api.icann.org/czds/downloads/links"
OPT_IN_TLDS = frozenset({"com", "net"})
ALL_TLDS = "*"
CHUNK_BYTES = 1 << 16
RECORD_BATCH = 200
LEDGER_PREFIX = "czds:downloaded:"
LEDGER_SECONDS = 36 * 3600

log = get_logger(__name__)


class CzdsError(RuntimeError):
    pass


class CzdsClient:
    def __init__(
        self, username: str, password: str, *, transport: httpx2.BaseTransport | None = None
    ) -> None:
        self._credentials = {"username": username, "password": password}
        self._http = httpx2.Client(
            transport=transport,
            timeout=httpx2.Timeout(30.0, read=120.0),
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )
        self._token: str | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CzdsClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def authenticate(self) -> None:
        response = self._http.post(AUTH_URL, json=self._credentials)
        if response.status_code != 200:
            raise CzdsError(f"CZDS authentication failed with HTTP {response.status_code}")
        token = response.json().get("accessToken")
        if not token:
            raise CzdsError("CZDS authentication returned no access token")
        self._token = token

    def _auth_headers(self) -> dict[str, str]:
        if self._token is None:
            self.authenticate()
        return {"Authorization": f"Bearer {self._token}"}

    def links(self) -> list[str]:
        response = self._http.get(LINKS_URL, headers=self._auth_headers())
        if response.status_code != 200:
            raise CzdsError(f"CZDS download links failed with HTTP {response.status_code}")
        return [str(link) for link in response.json()]

    @contextmanager
    def zone(self, url: str) -> Iterator[Iterator[bytes]]:
        """The gzip bytes of one zone file, as a stream of chunks."""
        with self._http.stream("GET", url, headers=self._auth_headers()) as response:
            if response.status_code != 200:
                raise CzdsError(f"CZDS zone download failed with HTTP {response.status_code}")
            yield response.iter_bytes(CHUNK_BYTES)


def tld_of(link: str) -> str:
    """``https://czds-api.icann.org/czds/downloads/xyz.zone`` -> ``xyz``."""
    name = urlsplit(link).path.rsplit("/", 1)[-1]
    return name.removesuffix(".zone").lower()


def select_links(links: Iterable[str], wanted: Sequence[str]) -> list[tuple[str, str]]:
    """``(tld, link)`` pairs to scan, sorted by TLD; ``*`` in ``wanted`` means all of them."""
    chosen = {tld.strip().lower().lstrip(".") for tld in wanted if tld.strip()}
    pairs = []
    for link in links:
        tld = tld_of(link)
        if ALL_TLDS in chosen:
            wanted_tld = True
        elif chosen:
            wanted_tld = tld in chosen
        else:
            wanted_tld = tld not in OPT_IN_TLDS
        if wanted_tld:
            pairs.append((tld, link))
    return sorted(pairs)


def gunzip_lines(chunks: Iterable[bytes]) -> Iterator[str]:
    """Decompress a gzip stream chunk by chunk and yield its lines; never holds the file."""
    decompressor = zlib.decompressobj(wbits=31)
    pending = b""
    for chunk in chunks:
        data = decompressor.decompress(chunk)
        # A file may hold several gzip members one after another.
        while decompressor.eof and decompressor.unused_data:
            rest = decompressor.unused_data
            decompressor = zlib.decompressobj(wbits=31)
            data += decompressor.decompress(rest)
        pending += data
        *lines, pending = pending.split(b"\n")
        for line in lines:
            yield line.decode("ascii", "replace")
    pending += decompressor.flush()
    if pending:
        yield pending.decode("ascii", "replace")


def iter_ns_owners(lines: Iterable[str], tld: str) -> Iterator[str]:
    """Owner names of NS records below ``tld``, consecutive duplicates removed.

    Zone lines look like ``mintera.xyz.  3600  in  ns  ns1.example.net.``.
    """
    apex = tld.lower().rstrip(".")
    previous = None
    for line in lines:
        fields = line.split()
        if len(fields) < 5 or fields[0].startswith((";", "$")):
            continue
        if fields[2].lower() != "in" or fields[3].lower() != "ns":
            continue
        owner = fields[0].lower().rstrip(".")
        if owner in (apex, previous):
            continue
        previous = owner
        yield owner


@dataclass
class ZoneResult:
    tld: str
    names: int = 0
    matched: int = 0
    new_match_ids: list[uuid.UUID] = field(default_factory=list)
    seconds: float = 0.0
    error: str | None = None


def scan_zone(
    tld: str,
    chunks: Iterable[bytes],
    matcher: Matcher,
    database: Database,
    queue_admit: Callable[[Sequence[uuid.UUID]], None],
    *,
    now: Callable[[], datetime] = utcnow,
) -> ZoneResult:
    result = ZoneResult(tld)
    started = time.monotonic()
    batch: list[tuple[HostView, list[MatchResult]]] = []

    def flush() -> None:
        if not batch:
            return
        seen_at = now()
        new_ids: list[uuid.UUID] = []
        with database.worker_session() as session:
            for view, matches in batch:
                recorded = record_discovery(
                    session,
                    view,
                    matches,
                    Sighting(SightingSource.CZDS, seen_at, {"tld": tld}),
                    sight_known=False,
                )
                new_ids += recorded.new_match_ids
        queue_admit(new_ids)
        result.new_match_ids += new_ids
        batch.clear()

    for owner in iter_ns_owners(gunzip_lines(chunks), tld):
        result.names += 1
        found = matcher.match_host(owner)
        if found is None or not found[1]:
            continue
        result.matched += 1
        batch.append(found)
        if len(batch) >= RECORD_BATCH:
            flush()
    flush()
    result.seconds = time.monotonic() - started
    observed = metrics()
    observed.czds_zone_seconds.labels(tld=tld).set(result.seconds)
    observed.czds_zone_names.labels(tld=tld).set(result.names)
    return result


class DownloadLedger(Protocol):
    """Where a zone's download is claimed for the day (Redis ``SET NX EX``)."""

    def set(self, name: str, value: str, *, nx: bool, ex: int) -> Any: ...


def ledger_key(tld: str, day: date) -> str:
    return f"{LEDGER_PREFIX}{tld}:{day.isoformat()}"


def run_czds_scan(
    settings: Settings,
    database: Database,
    queue_admit: Callable[[Sequence[uuid.UUID]], None],
    ledger: DownloadLedger,
    *,
    transport: httpx2.BaseTransport | None = None,
    today: Callable[[], date] = lambda: utcnow().date(),
) -> list[ZoneResult]:
    """Scan every selected zone; a failing zone is logged and the others still run.

    A zone is claimed in ``ledger`` before its download, so a redelivered or repeated run
    on the same UTC day skips it: CZDS allows one download per zone and day.
    """
    if settings.czds_username is None or settings.czds_password is None:
        log.info("czds.skipped", reason="CZDS_USERNAME or CZDS_PASSWORD is not set")
        return []
    matchers = MatcherCache(database)
    results = []
    with CzdsClient(
        settings.czds_username.get_secret_value(),
        settings.czds_password.get_secret_value(),
        transport=transport,
    ) as client:
        pairs = select_links(client.links(), settings.czds_tlds)
        log.info("czds.start", tlds=[tld for tld, _ in pairs])
        for tld, link in pairs:
            if not ledger.set(ledger_key(tld, today()), "1", nx=True, ex=LEDGER_SECONDS):
                log.info("czds.zone_skipped", tld=tld, reason="already downloaded today")
                continue
            try:
                with client.zone(link) as chunks:
                    zone = scan_zone(tld, chunks, matchers.get(), database, queue_admit)
            except Exception as exc:  # one zone must not stop the others
                zone = ZoneResult(tld, error=f"{type(exc).__name__}: {exc}")
                log.warning("czds.zone_failed", tld=tld, error=zone.error, exc_info=True)
            else:
                log.info(
                    "czds.zone_done",
                    tld=tld,
                    names=zone.names,
                    matched=zone.matched,
                    new_matches=len(zone.new_match_ids),
                    seconds=round(zone.seconds, 1),
                )
            results.append(zone)
    return results
