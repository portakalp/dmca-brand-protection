"""SSRF-safe HTTP client for candidate sites and the URLs they lead to (architecture.md §10).

Every request to a candidate site, its redirects, its favicon or its form targets goes
through :class:`SafeHttpClient`:

- The client resolves the host itself and refuses the request when any address is not
  public: loopback, private (RFC 1918), link-local (169.254.0.0/16, the metadata server
  included), CGNAT (100.64.0.0/10), multicast, reserved and documentation ranges, and the
  IPv6 equivalents (loopback, ULA, link-local, IPv4-mapped).
- The connection goes to the checked address (the URL carries the IP; the ``Host`` header
  and TLS SNI carry the name), so a second DNS answer cannot rebind it.
- Redirects are followed by hand, at most 10, and every hop is checked again.
- Nothing is shared between requests: no kept-alive connection (a pooled TLS session would
  skip the next host's certificate check) and no cookie jar. Cookies a site sets are sent back
  to the same host within one fetch only, so cookie-checking redirects still work.
- The body is cut at 2 MB of transferred bytes. gzip and deflate are decoded here, with the
  output capped at 2 MB too; other encodings are refused. Connecting may take 5 seconds, each
  read 5 and the whole fetch 15; a server that trickles header bytes is stopped by the job's
  own time limit (K69).
- Anything that goes wrong becomes a :class:`FetchError`; the page is untrusted and must not
  crash the caller.
- ``SSRF_ALLOW_CIDRS`` admits extra networks for tests and the local fixture sites only; the
  settings refuse it when ``ENV=prod``.
"""

from __future__ import annotations

import http.cookiejar
import ipaddress
import socket
import time
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx2

from kalkan.domain.hostnames import InvalidHostnameError, normalize_hostname

MAX_REDIRECTS = 10
MAX_BYTES = 2 * 1024 * 1024
CONNECT_SECONDS = 5.0
READ_SECONDS = 5.0
TOTAL_SECONDS = 15.0
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
ACCEPT_ENCODING = "gzip, deflate"
MAX_COOKIES = 50

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str], list[str]]


class FetchError(Exception):
    """The fetch failed: no such host, no connection, time-out, too many redirects."""


class NoSuchHostError(FetchError):
    pass


class ConnectionFailedError(FetchError):
    pass


class SsrfBlockedError(FetchError):
    """The URL leads to an address the client must not reach."""


def system_resolver(host: str) -> list[str]:
    """The host's addresses from the operating system's resolver."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise NoSuchHostError(f"{host}: {exc.strerror}") from exc
    except (UnicodeError, ValueError, OSError) as exc:
        raise FetchError(f"{host}: {type(exc).__name__}") from exc
    return sorted({str(info[4][0]) for info in infos})


def is_public(address: IPAddress) -> bool:
    """A globally routable unicast address."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return is_public(address.ipv4_mapped)
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


@dataclass(frozen=True)
class Response:
    url: str  # the final URL
    status: int
    headers: dict[str, str]
    body: bytes
    truncated: bool
    redirect_chain: list[str] = field(default_factory=list)  # every URL before the final one
    address: str = ""  # the IP the final response came from


class SafeHttpClient:
    """A small, synchronous GET client; one instance may serve many requests."""

    def __init__(
        self,
        *,
        allow_cidrs: Iterable[IPNetwork] = (),
        resolver: Resolver = system_resolver,
        transport: httpx2.BaseTransport | None = None,
        port_map: Mapping[int, int] | None = None,
        max_bytes: int = MAX_BYTES,
        total_seconds: float = TOTAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._allow = tuple(allow_cidrs)
        self._resolver = resolver
        # Test hook: lets tests on the host reach fixture sites published on other ports.
        # It only applies while extra networks are allowed, which prod never has.
        self._port_map = dict(port_map or {}) if self._allow else {}
        self._max_bytes = max_bytes
        self._total = total_seconds
        self._clock = clock
        self._client = httpx2.Client(
            transport=transport,
            follow_redirects=False,
            timeout=httpx2.Timeout(READ_SECONDS, connect=CONNECT_SECONDS),
            trust_env=False,
            limits=httpx2.Limits(max_keepalive_connections=0),
            cookies=http.cookiejar.CookieJar(
                policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
            ),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SafeHttpClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def deadline(self, seconds: float) -> float:
        """A deadline ``seconds`` from now on this client's clock, to share across fetches."""
        return self._clock() + seconds

    def allowed(self, address: IPAddress) -> bool:
        return is_public(address) or any(address in network for network in self._allow)

    def check_url(self, url: str) -> tuple[str, str, int, str]:
        """``(scheme, host, port, address)`` of a URL the client may fetch, or an error."""
        try:
            parts = urlsplit(url)
            hostname = parts.hostname
        except ValueError as exc:
            raise FetchError(f"bad URL {url[:200]!r}") from exc
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise SsrfBlockedError(f"only http and https may be fetched, not {scheme!r}")
        if parts.username is not None or parts.password is not None:
            raise SsrfBlockedError("URLs with user information are not fetched")
        if not hostname:
            raise FetchError(f"no host in {url[:200]!r}")
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise FetchError(f"bad port in {url[:200]!r}") from exc
        host = hostname.rstrip(".").lower()
        try:
            literal: IPAddress | None = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            addresses = [literal]
        else:
            try:
                host = normalize_hostname(host)
            except InvalidHostnameError as exc:
                raise FetchError(str(exc)) from exc
            resolved = self._resolver(host)
            if not resolved:
                raise NoSuchHostError(f"{host} has no address")
            addresses = [ipaddress.ip_address(address) for address in resolved]
        refused = [str(address) for address in addresses if not self.allowed(address)]
        if refused:
            raise SsrfBlockedError(f"{host} resolves to an address that is not public: {refused}")
        # IPv4 first: more hosts serve it correctly.
        chosen = sorted(addresses, key=lambda address: address.version)[0]
        return scheme, host, port, str(chosen)

    def get(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        deadline: float | None = None,
    ) -> Response:
        """Fetch ``url``; ``deadline`` (on this client's clock) can only shorten the budget."""
        own = self._clock() + self._total
        deadline = own if deadline is None else min(own, deadline)
        chain: list[str] = []
        cookies: dict[str, dict[str, str]] = {}
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            response, address = self._fetch_once(current, headers or {}, deadline, cookies)
            location = response.headers.get("location")
            if response.status in REDIRECT_STATUSES and location:
                chain.append(current)
                try:
                    current = urljoin(current, location.strip())
                except ValueError as exc:
                    raise FetchError(f"bad redirect from {current[:200]}") from exc
                continue
            return Response(
                url=current,
                status=response.status,
                headers=response.headers,
                body=response.body,
                truncated=response.truncated,
                redirect_chain=chain,
                address=address,
            )
        raise FetchError(f"more than {MAX_REDIRECTS} redirects from {url[:200]}")

    def _fetch_once(
        self,
        url: str,
        headers: Mapping[str, str],
        deadline: float,
        cookies: dict[str, dict[str, str]],
    ) -> tuple[Response, str]:
        scheme, host, port, address = self.check_url(url)
        parts = urlsplit(url)
        connect_port = self._port_map.get(port, port)
        netloc = f"[{address}]" if ":" in address else address
        target = urlunsplit(
            (scheme, f"{netloc}:{connect_port}", parts.path or "/", parts.query, "")
        )
        default_port = 443 if scheme == "https" else 80
        request_headers = {
            **{name: value for name, value in headers.items() if name.lower() != "cookie"},
            "Host": host if port == default_port else f"{host}:{port}",
            "Accept-Encoding": ACCEPT_ENCODING,
        }
        if cookies.get(host):
            request_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies[host].items())
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise FetchError(f"time-out fetching {url[:200]}")
        timeout = httpx2.Timeout(
            min(READ_SECONDS, remaining), connect=min(CONNECT_SECONDS, remaining)
        )
        extensions = {"sni_hostname": host} if scheme == "https" else {}
        try:
            with self._client.stream(
                "GET", target, headers=request_headers, timeout=timeout, extensions=extensions
            ) as stream:
                # Raw bytes, so the cap counts what crosses the network, not what a
                # compressed body expands to.
                raw = bytearray()
                truncated = False
                for chunk in stream.iter_raw():
                    raw += chunk
                    if len(raw) > self._max_bytes:
                        del raw[self._max_bytes :]
                        truncated = True
                        break
                    if self._clock() > deadline:
                        raise FetchError(f"time-out reading {url[:200]}")
                status = stream.status_code
                response_headers = {k.lower(): v for k, v in stream.headers.items()}
                _keep_cookies(cookies, host, stream.headers.get_list("set-cookie"))
        except httpx2.TimeoutException as exc:
            raise FetchError(f"time-out fetching {url[:200]}") from exc
        except httpx2.TransportError as exc:
            raise ConnectionFailedError(f"{url[:200]}: {type(exc).__name__}") from exc
        except (httpx2.HTTPError, httpx2.InvalidURL, httpx2.StreamError) as exc:
            raise FetchError(f"{url[:200]}: {type(exc).__name__}") from exc
        except (ValueError, UnicodeError) as exc:
            raise FetchError(f"{url[:200]}: {type(exc).__name__}") from exc
        encoding = response_headers.get("content-encoding", "identity")
        body, clipped = _decode_body(bytes(raw), encoding, self._max_bytes)
        response = Response(
            url=url,
            status=status,
            headers=response_headers,
            body=body,
            truncated=truncated or clipped,
        )
        return response, address


def _keep_cookies(cookies: dict[str, dict[str, str]], host: str, values: list[str]) -> None:
    """Remember ``name=value`` of each Set-Cookie for this host, for this fetch only."""
    jar = cookies.setdefault(host, {})
    for value in values:
        pair = value.split(";", 1)[0]
        name, sep, content = pair.partition("=")
        name, content = name.strip(), content.strip()
        if not sep or not name or len(name) + len(content) > 4096:
            continue
        if name in jar or len(jar) < MAX_COOKIES:
            jar[name] = content


def _decode_body(raw: bytes, encoding: str, max_bytes: int) -> tuple[bytes, bool]:
    """The body after Content-Encoding, at most ``max_bytes``; ``(body, clipped)``."""
    encoding = encoding.strip().lower()
    if encoding in ("", "identity"):
        return raw, False
    if encoding in ("gzip", "x-gzip"):
        decoders = [zlib.MAX_WBITS | 16]
    elif encoding == "deflate":
        decoders = [zlib.MAX_WBITS, -zlib.MAX_WBITS]  # zlib-wrapped, or raw as some servers do
    else:
        raise FetchError(f"unsupported content encoding {encoding[:40]!r}")
    for wbits in decoders:
        try:
            body = zlib.decompressobj(wbits).decompress(raw, max_bytes + 1)
        except zlib.error:
            continue
        if len(body) > max_bytes:
            return body[:max_bytes], True
        return body, False
    raise FetchError(f"the {encoding} body could not be decoded")
