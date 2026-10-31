"""The browser's forward proxy in a self-hosted deployment: ``python -m kalkan.net.egress_proxy``.

In the cloud deployment Chromium reaches candidate sites through a residential proxy, and the
host firewall keeps the browser network away from private addresses. A self-hosted server has
neither, so worker-browser sits on an internal network whose only way out is this proxy
(``PROXY_MAP='{"XX":"http://egress-proxy:8888"}'``). It applies the rules of
:mod:`kalkan.net.safe_http` to every connection the browser makes:

- The target host is resolved once, with the same resolver and the same :func:`is_public`
  test as the SSRF-safe client. When any of its addresses is not public (loopback, RFC 1918,
  link-local and the metadata server, CGNAT, ULA, IPv4-mapped forms of those) the request is
  answered ``403`` and the refusal is logged with the host and the reason only.
- The proxy connects to the address it checked, never to the name, so a second DNS answer
  cannot rebind the connection to an internal service.
- Only the configured ports (80 and 443 by default) may be reached, by CONNECT tunnels (https,
  WebSocket) and by plain-http absolute-form requests alike.
- A request head may be at most ``max_head_bytes`` and must arrive within ``head_seconds``;
  at most ``max_connections`` clients are served at once (the rest get ``503``); a connection
  quiet for ``idle_seconds`` in either direction is closed.

One request per client connection: a plain-http request goes upstream with ``Connection:
close``, and whatever else the client sends on that connection can only reach the same
checked address. Paths and query strings are never logged (they may carry tokens).

``SSRF_ALLOW_CIDRS`` admits extra networks, as it does for the SSRF-safe client: for the local
fixture sites and the tests only, since the settings refuse it when ``ENV=prod``.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import signal
import socket
import threading
import time
from collections.abc import Collection, Sequence
from contextlib import suppress
from dataclasses import dataclass
from types import FrameType
from urllib.parse import urlsplit

from kalkan.config import get_settings
from kalkan.domain.hostnames import InvalidHostnameError, normalize_hostname
from kalkan.logging import configure_logging, get_logger
from kalkan.net.safe_http import (
    FetchError,
    IPAddress,
    IPNetwork,
    NoSuchHostError,
    Resolver,
    is_public,
    system_resolver,
)

SERVICE = "egress-proxy"
# All interfaces of the container: the browser network is its only client network.
DEFAULT_LISTEN = "0.0.0.0:8888"
DEFAULT_PORTS = frozenset({80, 443})
MAX_HEAD_BYTES = 16 * 1024
MAX_CONNECTIONS = 256
HEAD_SECONDS = 15.0
CONNECT_SECONDS = 10.0
IDLE_SECONDS = 60.0
DRAIN_SECONDS = 10.0
_CHUNK = 64 * 1024
_LINGER_SECONDS = 1.0
_LINGER_BYTES = 256 * 1024
# Clients turned away with 503 at once; beyond that a busy proxy just closes.
MAX_REFUSING = 16
_REQUEST_LINE = re.compile(rb"^([A-Z]{3,10}) (\S{1,8192}) HTTP/1\.[01]$")
_HEADER_NAME = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
# Headers meant for this hop; the upstream request gets its own Host and Connection.
_HOP_HEADERS = frozenset(
    {b"connection", b"keep-alive", b"proxy-connection", b"proxy-authorization", b"host", b"te"}
)
_REASONS = {
    400: "Bad Request",
    403: "Forbidden",
    408: "Request Timeout",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}

# A fixed name: under ``python -m`` the module is ``__main__``.
log = get_logger("kalkan.net.egress_proxy")


class ProxyError(Exception):
    """A request the proxy answers itself; ``reason`` is a short code for the log."""

    def __init__(self, status: int, reason: str, host: str | None = None) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.host = host


@dataclass(frozen=True)
class Target:
    """Where one client request goes: the name it asked for and the address that was checked."""

    host: str
    port: int
    address: str


@dataclass(frozen=True)
class ProxyConfig:
    host: str = "0.0.0.0"  # noqa: S104 - see DEFAULT_LISTEN
    port: int = 8888
    allowed_ports: frozenset[int] = DEFAULT_PORTS
    # Test and local-fixture hook, like SafeHttpClient's; prod settings keep it empty.
    allow_cidrs: tuple[IPNetwork, ...] = ()
    max_head_bytes: int = MAX_HEAD_BYTES
    max_connections: int = MAX_CONNECTIONS
    head_seconds: float = HEAD_SECONDS
    connect_seconds: float = CONNECT_SECONDS
    idle_seconds: float = IDLE_SECONDS


def check_target(
    host: str,
    port: int,
    *,
    allowed_ports: Collection[int] = DEFAULT_PORTS,
    allow_cidrs: Sequence[IPNetwork] = (),
    resolver: Resolver = system_resolver,
) -> Target:
    """Resolve ``host`` once and return the address to connect to, or raise ``ProxyError``.

    The same steps as ``SafeHttpClient.check_url``: an IP literal is taken as is, a name must
    be a valid domain name, and every address it resolves to must be public (or in
    ``allow_cidrs``); IPv4 is preferred.
    """
    host = host.rstrip(".").lower()
    if port not in allowed_ports:
        raise ProxyError(403, "port_not_allowed", host)
    try:
        literal: IPAddress | None = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [literal]
    else:
        try:
            host = normalize_hostname(host)
        except InvalidHostnameError:
            # Single-label names (``web``, ``localhost``) are internal by definition.
            raise ProxyError(403, "invalid_host", host) from None
        try:
            resolved = resolver(host)
        except NoSuchHostError:
            raise ProxyError(502, "no_such_host", host) from None
        except FetchError:
            raise ProxyError(502, "resolve_failed", host) from None
        if not resolved:
            raise ProxyError(502, "no_such_host", host)
        try:
            addresses = [ipaddress.ip_address(address) for address in resolved]
        except ValueError:
            raise ProxyError(502, "resolve_failed", host) from None

    def allowed(address: IPAddress) -> bool:
        return is_public(address) or any(address in network for network in allow_cidrs)

    if not all(allowed(address) for address in addresses):
        raise ProxyError(403, "not_public", host)
    chosen = sorted(addresses, key=lambda address: address.version)[0]
    return Target(host=host, port=port, address=str(chosen))


@dataclass(frozen=True)
class Request:
    """A parsed client request head."""

    method: bytes
    target: bytes
    headers: list[tuple[bytes, bytes]]


def parse_head(head: bytes) -> Request:
    """The request line and headers, or ``ProxyError(400)`` for anything malformed."""
    # A bare CR or LF, or a NUL, would reach the site unchanged, and many servers end a line
    # on a bare LF: header injection toward the upstream. RFC 9112 §2.2 lets us reject them.
    unfolded = head.replace(b"\r\n", b"")
    if b"\n" in unfolded or b"\r" in unfolded or b"\x00" in head:
        raise ProxyError(400, "bad_header")
    lines = head.split(b"\r\n")
    match = _REQUEST_LINE.match(lines[0])
    if match is None:
        raise ProxyError(400, "bad_request_line")
    headers: list[tuple[bytes, bytes]] = []
    for line in lines[1:]:
        name, colon, value = line.partition(b":")
        # No obsolete line folding and no space before the colon (RFC 9112 §5).
        if not colon or not _HEADER_NAME.match(name):
            raise ProxyError(400, "bad_header")
        headers.append((name, value.strip()))
    return Request(method=match.group(1), target=match.group(2), headers=headers)


def connect_authority(target: bytes) -> tuple[str, int]:
    """``host`` and ``port`` of a CONNECT target (``host:port`` or ``[v6]:port``)."""
    try:
        parts = urlsplit("//" + target.decode("ascii"))
        port = parts.port
    except (UnicodeDecodeError, ValueError):
        raise ProxyError(400, "bad_connect_target") from None
    if (
        not parts.hostname
        or port is None
        or parts.username is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise ProxyError(400, "bad_connect_target")
    return parts.hostname, port


def absolute_target(target: bytes) -> tuple[str, int, str]:
    """``host``, ``port`` and origin-form path of an absolute-form ``http://`` target."""
    try:
        text = target.decode("ascii")
        parts = urlsplit(text)
        port = parts.port or 80
    except (UnicodeDecodeError, ValueError):
        raise ProxyError(400, "bad_target") from None
    if parts.scheme.lower() != "http" or not parts.hostname:
        # https goes through CONNECT; an origin-form path is not a proxy request.
        raise ProxyError(400, "not_absolute_http")
    if parts.username is not None or parts.password is not None:
        raise ProxyError(400, "userinfo_in_target")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return parts.hostname, port, path


def upstream_head(request: Request, host: str, port: int, path: str) -> bytes:
    """The origin-form request for the site: its own Host and ``Connection: close``."""
    authority = f"[{host}]" if ":" in host else host
    if port != 80:
        authority = f"{authority}:{port}"
    lines = [request.method + b" " + path.encode("ascii") + b" HTTP/1.1"]
    lines.append(b"Host: " + authority.encode("ascii"))
    lines.extend(
        name + b": " + value for name, value in request.headers if name.lower() not in _HOP_HEADERS
    )
    lines.append(b"Connection: close")
    return b"\r\n".join(lines) + b"\r\n\r\n"


def error_response(status: int) -> bytes:
    text = _REASONS.get(status, "Error")
    body = f"egress proxy: {status} {text}\n".encode()
    return (
        f"HTTP/1.1 {status} {text}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body


def answer_and_close(sock: socket.socket, status: int) -> None:
    """Send an error answer and close so that the client can read it.

    Closing a socket with unread request bytes makes Linux send a reset, which can destroy
    the answer before the client reads it; so the rest of the request is read and dropped
    first, for at most ``_LINGER_SECONDS`` and ``_LINGER_BYTES``.
    """
    try:
        sock.settimeout(_LINGER_SECONDS)
        sock.sendall(error_response(status))
        sock.shutdown(socket.SHUT_WR)
        deadline = time.monotonic() + _LINGER_SECONDS
        drained = 0
        while drained < _LINGER_BYTES and time.monotonic() < deadline:
            chunk = sock.recv(_CHUNK)
            if not chunk:
                break
            drained += len(chunk)
    except OSError:
        pass
    finally:
        with suppress(OSError):
            sock.close()


def _copy(source: socket.socket, target: socket.socket) -> None:
    try:
        while chunk := source.recv(_CHUNK):
            target.sendall(chunk)
    except OSError:  # includes the idle time-out
        pass
    finally:
        with suppress(OSError):
            target.shutdown(socket.SHUT_WR)


class EgressProxy:
    """A threaded forward proxy; ``start()`` serves in the background, ``close()`` drains."""

    def __init__(self, config: ProxyConfig, *, resolver: Resolver = system_resolver) -> None:
        self._config = config
        self._resolver = resolver
        self._listener = socket.create_server((config.host, config.port), backlog=128)
        self._slots = threading.BoundedSemaphore(config.max_connections)
        self._refusing = threading.BoundedSemaphore(MAX_REFUSING)
        self._sockets: set[socket.socket] = set()
        self._active: set[threading.Thread] = set()
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._serve, name="egress-proxy", daemon=True)

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._listener.getsockname()[:2]
        return str(host), int(port)

    def start(self) -> EgressProxy:
        self._thread.start()
        log.info(
            "egress.started",
            listen=f"{self.address[0]}:{self.address[1]}",
            ports=sorted(self._config.allowed_ports),
            allow_cidrs=[str(network) for network in self._config.allow_cidrs],
            max_connections=self._config.max_connections,
        )
        return self

    def __enter__(self) -> EgressProxy:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close(drain_seconds=0)

    def close(self, drain_seconds: float = DRAIN_SECONDS) -> None:
        """Stop accepting, let open connections finish for ``drain_seconds``, close the rest."""
        with self._lock:
            self._closed = True
        # shutdown() wakes the thread blocked in accept() on Linux; close() alone does not.
        with suppress(OSError):
            self._listener.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            self._listener.close()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        deadline = time.monotonic() + drain_seconds
        while time.monotonic() < deadline:
            with self._lock:
                if not self._active:
                    break
            time.sleep(0.05)
        with self._lock:
            open_sockets = list(self._sockets)
            remaining = len(self._active)
        for sock in open_sockets:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                sock.close()
        log.info("egress.stopped", cut_connections=remaining)

    def _track(self, sock: socket.socket) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._sockets.add(sock)
            return True

    def _release(self, *sockets: socket.socket | None) -> None:
        for sock in sockets:
            if sock is None:
                continue
            with self._lock:
                self._sockets.discard(sock)
            # shutdown() first wakes a thread still blocked in recv() on this socket, so it
            # cannot read from a later connection that reuses the file descriptor.
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                sock.close()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                log.warning("egress.busy", max_connections=self._config.max_connections)
                self._turn_away(client)
                continue
            if not self._track(client):
                self._slots.release()
                self._release(client)
                return
            thread = threading.Thread(target=self._run, args=(client,), daemon=True)
            with self._lock:
                self._active.add(thread)
            thread.start()

    def _turn_away(self, client: socket.socket) -> None:
        """Answer 503 off the accept loop, so a flood of clients cannot stall it."""
        if not self._refusing.acquire(blocking=False):
            with suppress(OSError):
                client.close()
            return

        def refuse() -> None:
            try:
                answer_and_close(client, 503)
            finally:
                self._refusing.release()

        threading.Thread(target=refuse, daemon=True).start()

    def _run(self, client: socket.socket) -> None:
        try:
            self._handle(client)
        finally:
            self._release(client)
            self._slots.release()
            with self._lock:
                self._active.discard(threading.current_thread())

    def _read_head(self, client: socket.socket) -> tuple[bytes, bytes]:
        deadline = time.monotonic() + self._config.head_seconds
        limit = self._config.max_head_bytes
        data = b""
        while b"\r\n\r\n" not in data:
            if len(data) > limit:
                raise ProxyError(431, "head_too_large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProxyError(408, "head_timeout")
            client.settimeout(min(remaining, self._config.idle_seconds))
            try:
                chunk = client.recv(_CHUNK)
            except TimeoutError:
                raise ProxyError(408, "head_timeout") from None
            if not chunk:
                raise ProxyError(400, "head_incomplete")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        if len(head) > limit:
            raise ProxyError(431, "head_too_large")
        return head, rest

    def _handle(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        host: str | None = None
        try:
            head, rest = self._read_head(client)
            request = parse_head(head)
            if request.method == b"CONNECT":
                host, port = connect_authority(request.target)
                path = ""
            else:
                host, port, path = absolute_target(request.target)
            target = self._check(host, port)
            upstream = self._connect(target)
            if request.method == b"CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                upstream.sendall(rest)
            else:
                upstream.sendall(upstream_head(request, target.host, port, path) + rest)
            log.info(
                "egress.connected",
                method=request.method.decode(),
                host=target.host,
                port=port,
                address=target.address,
            )
            self._relay(client, upstream)
        except ProxyError as exc:
            if exc.status == 403:
                log.warning("egress.refused", host=exc.host or host, reason=exc.reason)
            else:
                log.info(
                    "egress.error", host=exc.host or host, status=exc.status, reason=exc.reason
                )
            answer_and_close(client, exc.status)
        except OSError as exc:
            log.info("egress.connection_failed", host=host, error=type(exc).__name__)
        finally:
            self._release(upstream)

    def _check(self, host: str, port: int) -> Target:
        return check_target(
            host,
            port,
            allowed_ports=self._config.allowed_ports,
            allow_cidrs=self._config.allow_cidrs,
            resolver=self._resolver,
        )

    def _connect(self, target: Target) -> socket.socket:
        try:
            upstream = socket.create_connection(
                (target.address, target.port), timeout=self._config.connect_seconds
            )
        except TimeoutError:
            raise ProxyError(504, "connect_timeout", target.host) from None
        except OSError:
            raise ProxyError(502, "connect_failed", target.host) from None
        if not self._track(upstream):
            with suppress(OSError):
                upstream.close()
            raise ProxyError(503, "shutting_down", target.host)
        upstream.settimeout(self._config.idle_seconds)
        return upstream

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        client.settimeout(self._config.idle_seconds)
        sender = threading.Thread(target=_copy, args=(client, upstream), daemon=True)
        sender.start()
        _copy(upstream, client)
        # The site is done: stop reading from the client, which ends the sender's recv(), and
        # wait for it, so no thread uses either socket after this connection is released.
        # Both sockets have the idle time-out, so a blocked sendall() ends too.
        with suppress(OSError):
            client.shutdown(socket.SHUT_RD)
        sender.join()


def _ports(value: str) -> frozenset[int]:
    try:
        ports = frozenset(int(port) for port in value.split(",") if port.strip())
    except ValueError:
        raise argparse.ArgumentTypeError("ports are comma-separated numbers") from None
    if not ports or not all(0 < port < 65536 for port in ports):
        raise argparse.ArgumentTypeError("ports must be between 1 and 65535")
    return ports


def _listen(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError("listen address is host:port")
    return host.strip("[]"), int(port)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m kalkan.net.egress_proxy")
    parser.add_argument("--listen", type=_listen, default=_listen(DEFAULT_LISTEN))
    parser.add_argument("--ports", type=_ports, default=DEFAULT_PORTS)
    parser.add_argument("--max-connections", type=int, default=MAX_CONNECTIONS)
    parser.add_argument("--max-head-bytes", type=int, default=MAX_HEAD_BYTES)
    parser.add_argument("--idle-seconds", type=float, default=IDLE_SECONDS)
    parser.add_argument("--drain-seconds", type=float, default=DRAIN_SECONDS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(SERVICE, settings.log_level)
    host, port = args.listen
    config = ProxyConfig(
        host=host,
        port=port,
        allowed_ports=args.ports,
        allow_cidrs=tuple(settings.ssrf_allow_cidrs),
        max_head_bytes=args.max_head_bytes,
        max_connections=args.max_connections,
        idle_seconds=args.idle_seconds,
    )
    stop = threading.Event()

    def request_stop(signum: int, _: FrameType | None) -> None:
        log.info("egress.stopping", signal=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    proxy = EgressProxy(config).start()
    stop.wait()
    proxy.close(drain_seconds=args.drain_seconds)


if __name__ == "__main__":
    main()
