"""The browser's egress proxy in self-hosted deployments (safe_http's rules for Chromium).

Everything runs on 127.0.0.1: the "sites" are local servers, reachable only because the test
proxy admits 127.0.0.0/8 through the ``allow_cidrs`` hook; the names are served by a fake
resolver, so nothing leaves the machine.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from typing import Any

import pytest

from kalkan.net import egress_proxy
from kalkan.net.egress_proxy import (
    EgressProxy,
    ProxyConfig,
    ProxyError,
    check_target,
    parse_args,
    parse_head,
    upstream_head,
)
from kalkan.net.safe_http import NoSuchHostError

LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"),)
PUBLIC = "93.184.215.14"


class Recorder:
    """Stands in for the module logger: keeps ``(event, fields)``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def _add(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    info = warning = _add

    def named(self, event: str) -> list[dict[str, Any]]:
        return [fields for name, fields in self.events if name == event]


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(egress_proxy, "log", recorder)
    return recorder


class Site:
    """A local TCP server: echoes the bytes it gets, or answers one HTTP request."""

    def __init__(self, mode: str) -> None:
        self.socket = socket.create_server(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.mode = mode
        self.heads: list[bytes] = []
        self.accepted = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.socket.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            try:
                if self.mode == "echo":
                    while chunk := conn.recv(4096):
                        conn.sendall(chunk)
                    return
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                self.heads.append(data.partition(b"\r\n\r\n")[0])
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"
                )
            except OSError:
                return

    def close(self) -> None:
        with_suppressed(lambda: self.socket.shutdown(socket.SHUT_RDWR))
        self.socket.close()


def with_suppressed(action: Callable[[], object]) -> None:
    with suppress(OSError):
        action()


@pytest.fixture
def http_site() -> Iterator[Site]:
    site = Site("http")
    yield site
    site.close()


@pytest.fixture
def echo_site() -> Iterator[Site]:
    site = Site("echo")
    yield site
    site.close()


class Resolver:
    def __init__(self, names: dict[str, list[str]]) -> None:
        self.names = names
        self.calls: list[str] = []

    def __call__(self, host: str) -> list[str]:
        self.calls.append(host)
        if host not in self.names:
            raise NoSuchHostError(host)
        return self.names[host]


def _proxy(
    resolver: Resolver, *, ports: set[int] | None = None, allow: bool = True, **options: Any
) -> EgressProxy:
    config = ProxyConfig(
        host="127.0.0.1",
        port=0,
        allowed_ports=frozenset(ports or {80, 443}),
        allow_cidrs=LOOPBACK if allow else (),
        **options,
    )
    return EgressProxy(config, resolver=resolver)


def _send(proxy: EgressProxy, data: bytes, *, read: bool = True) -> bytes:
    with socket.create_connection(proxy.address, timeout=5) as conn:
        conn.sendall(data)
        if not read:
            return b""
        return _read_all(conn)


def _read_all(conn: socket.socket) -> bytes:
    received = b""
    while chunk := conn.recv(4096):
        received += chunk
    return received


def _status(response: bytes) -> int:
    return int(response.split(b" ", 2)[1])


# --- target checks (the same rules as safe_http) ---


def test_a_public_name_resolves_once_to_the_address_to_connect() -> None:
    resolver = Resolver({"site.example.com": ["2606:2800::1", PUBLIC]})
    target = check_target("Site.Example.COM.", 443, resolver=resolver)
    assert (target.host, target.port, target.address) == ("site.example.com", 443, PUBLIC)
    assert resolver.calls == ["site.example.com"]


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["192.168.0.10"],
        ["172.31.250.2"],
        ["169.254.169.254"],  # the metadata server
        ["100.64.0.1"],  # CGNAT
        ["::1"],
        ["fd00::1"],
        ["fe80::1"],
        ["::ffff:10.0.0.1"],
        ["0.0.0.0"],  # noqa: S104 - an address under test, not a bind
        [PUBLIC, "10.0.0.1"],  # one private answer is enough to refuse
    ],
)
def test_a_name_with_a_non_public_address_is_refused(addresses: list[str]) -> None:
    with pytest.raises(ProxyError) as caught:
        check_target("site.example.com", 443, resolver=Resolver({"site.example.com": addresses}))
    assert (caught.value.status, caught.value.reason) == (403, "not_public")


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "::ffff:127.0.0.1", "0.0.0.0"],  # noqa: S104
)
def test_a_non_public_ip_literal_is_refused_without_resolving(host: str) -> None:
    resolver = Resolver({})
    with pytest.raises(ProxyError) as caught:
        check_target(host, 80, resolver=resolver)
    assert caught.value.status == 403
    assert resolver.calls == []


@pytest.mark.parametrize("host", ["localhost", "web", "metadata"])
def test_single_label_names_are_refused(host: str) -> None:
    with pytest.raises(ProxyError) as caught:
        check_target(host, 80, resolver=Resolver({host: [PUBLIC]}))
    assert (caught.value.status, caught.value.reason) == (403, "invalid_host")


def test_only_the_allowed_ports_may_be_reached() -> None:
    resolver = Resolver({"site.example.com": [PUBLIC]})
    for port in (22, 25, 6379, 8080):
        with pytest.raises(ProxyError) as caught:
            check_target("site.example.com", port, resolver=resolver)
        assert (caught.value.status, caught.value.reason) == (403, "port_not_allowed")
    assert resolver.calls == []  # refused before any DNS query
    assert check_target("site.example.com", 8080, allowed_ports={8080}, resolver=resolver)


def test_an_unknown_name_is_a_bad_gateway() -> None:
    with pytest.raises(ProxyError) as caught:
        check_target("nowhere.example.com", 443, resolver=Resolver({}))
    assert (caught.value.status, caught.value.reason) == (502, "no_such_host")


def test_the_allow_cidrs_hook_admits_only_its_networks() -> None:
    resolver = Resolver({"site.test.example": ["127.0.0.1"], "other.example.com": ["10.0.0.1"]})
    assert check_target("site.test.example", 80, allow_cidrs=LOOPBACK, resolver=resolver)
    with pytest.raises(ProxyError):
        check_target("other.example.com", 80, allow_cidrs=LOOPBACK, resolver=resolver)


# --- request parsing ---


@pytest.mark.parametrize(
    "head",
    [
        b"GARBAGE",
        b"GET http://a.example.com/ HTTP/2.0",
        b"get http://a.example.com/ HTTP/1.1",
        b"GET  http://a.example.com/ HTTP/1.1",
        b"GET http://a.example.com/ HTTP/1.1\r\nNo colon here",
        b"GET http://a.example.com/ HTTP/1.1\r\n folded: header",
        b"GET http://a.example.com/ HTTP/1.1\r\nBad Name: x",
        # Bare LF, bare CR or NUL inside a header (would be injection toward the site).
        b"GET http://a.example.com/ HTTP/1.1\r\nX-A: a\nInjected: yes",
        b"GET http://a.example.com/ HTTP/1.1\r\nX-A: a\rInjected: yes",
        b"GET http://a.example.com/ HTTP/1.1\r\nX-A: a\x00",
        b"GET http://a.example.com/ HTTP/1.1\nX-A: a",
    ],
)
def test_malformed_heads_are_refused(head: bytes) -> None:
    with pytest.raises(ProxyError) as caught:
        parse_head(head)
    assert caught.value.status == 400


def test_the_upstream_request_is_origin_form_with_its_own_host() -> None:
    request = parse_head(
        b"POST http://a.example.com:8080/p?q=1 HTTP/1.1\r\nHost: evil.test\r\n"
        b"Proxy-Authorization: Basic x\r\nProxy-Connection: keep-alive\r\n"
        b"Connection: keep-alive\r\nAccept: */*\r\nContent-Length: 2"
    )
    assert upstream_head(request, "a.example.com", 8080, "/p?q=1") == (
        b"POST /p?q=1 HTTP/1.1\r\nHost: a.example.com:8080\r\nAccept: */*\r\n"
        b"Content-Length: 2\r\nConnection: close\r\n\r\n"
    )


# --- the running proxy ---


def test_an_absolute_form_get_round_trips_to_the_checked_address(
    http_site: Site, logs: Recorder
) -> None:
    resolver = Resolver({"site.example.com": ["127.0.0.1"]})
    with _proxy(resolver, ports={http_site.port}) as proxy:
        response = _send(
            proxy,
            f"GET http://site.example.com:{http_site.port}/a?b=c HTTP/1.1\r\n"
            "Host: site.example.com\r\nProxy-Connection: keep-alive\r\n\r\n".encode(),
        )
    assert response.startswith(b"HTTP/1.1 200 OK")
    assert response.endswith(b"hello")
    (head,) = http_site.heads
    assert head.startswith(b"GET /a?b=c HTTP/1.1\r\n")
    assert f"Host: site.example.com:{http_site.port}".encode() in head
    assert b"Proxy-Connection" not in head
    assert b"Connection: close" in head
    assert resolver.calls == ["site.example.com"]
    (connected,) = logs.named("egress.connected")
    assert connected["host"] == "site.example.com"
    assert "b=c" not in str(logs.events)  # paths and queries are never logged


def test_a_connect_tunnel_carries_data_both_ways(echo_site: Site) -> None:
    resolver = Resolver({"tunnel.example.com": ["127.0.0.1"]})
    with (
        _proxy(resolver, ports={echo_site.port}) as proxy,
        socket.create_connection(proxy.address, timeout=5) as conn,
    ):
        conn.sendall(
            f"CONNECT tunnel.example.com:{echo_site.port} HTTP/1.1\r\n"
            f"Host: tunnel.example.com:{echo_site.port}\r\n\r\nearly".encode()
        )
        received = b""
        while not received.endswith(b"early"):
            received += conn.recv(4096)
        assert received == b"HTTP/1.1 200 Connection established\r\n\r\nearly"
        conn.sendall(b"\x16\x03\x01 binary \x00\xff")
        assert conn.recv(4096) == b"\x16\x03\x01 binary \x00\xff"
        conn.shutdown(socket.SHUT_WR)
        assert _read_all(conn) == b""


def test_a_private_target_is_refused_and_logged_without_connecting(
    http_site: Site, logs: Recorder
) -> None:
    resolver = Resolver({"rebind.example.com": ["127.0.0.1"]})
    # No allow_cidrs: 127.0.0.1 is just as forbidden as the metadata server.
    with _proxy(resolver, ports={http_site.port}, allow=False) as proxy:
        for request in (
            f"CONNECT rebind.example.com:{http_site.port} HTTP/1.1\r\n\r\n",
            f"GET http://127.0.0.1:{http_site.port}/secret?token=abc HTTP/1.1\r\n\r\n",
            f"CONNECT [::1]:{http_site.port} HTTP/1.1\r\n\r\n",
        ):
            assert _status(_send(proxy, request.encode())) == 403
    assert http_site.accepted == 0
    refused = logs.named("egress.refused")
    assert [entry["reason"] for entry in refused] == ["not_public"] * 3
    assert [entry["host"] for entry in refused] == ["rebind.example.com", "127.0.0.1", "::1"]
    assert "token=abc" not in str(logs.events)


@pytest.mark.parametrize(
    "request_text",
    [
        "CONNECT 169.254.169.254:80 HTTP/1.1\r\n\r\n",
        "GET http://169.254.169.254/computeMetadata/v1/ HTTP/1.1\r\n\r\n",
        "GET http://metadata.google.internal/ HTTP/1.1\r\n\r\n",
        "CONNECT 10.0.0.5:443 HTTP/1.1\r\n\r\n",
        "CONNECT [fe80::1]:443 HTTP/1.1\r\n\r\n",
    ],
)
def test_metadata_and_private_ranges_are_refused(request_text: str) -> None:
    resolver = Resolver({"metadata.google.internal": ["169.254.169.254"]})
    with _proxy(resolver) as proxy:
        assert _status(_send(proxy, request_text.encode())) == 403


def test_a_port_outside_the_list_is_refused(echo_site: Site, logs: Recorder) -> None:
    resolver = Resolver({"tunnel.example.com": ["127.0.0.1"]})
    with _proxy(resolver) as proxy:  # 80 and 443 only
        response = _send(
            proxy, f"CONNECT tunnel.example.com:{echo_site.port} HTTP/1.1\r\n\r\n".encode()
        )
    assert _status(response) == 403
    assert echo_site.accepted == 0
    assert logs.named("egress.refused")[0]["reason"] == "port_not_allowed"


@pytest.mark.parametrize(
    "request_bytes",
    [
        b"HELLO\r\n\r\n",
        b"GET /relative HTTP/1.1\r\nHost: a.example.com\r\n\r\n",  # not a proxy request
        b"GET https://a.example.com/ HTTP/1.1\r\n\r\n",  # https goes through CONNECT
        b"GET http://user:pw@a.example.com/ HTTP/1.1\r\n\r\n",
        b"CONNECT a.example.com HTTP/1.1\r\n\r\n",  # no port
        b"CONNECT a.example.com:443/x HTTP/1.1\r\n\r\n",
        b"GET http://a.example.com/ HTTP/1.1\r\nbroken header\r\n\r\n",
    ],
)
def test_a_malformed_request_gets_400(request_bytes: bytes) -> None:
    resolver = Resolver({"a.example.com": [PUBLIC]})
    with _proxy(resolver) as proxy:
        assert _status(_send(proxy, request_bytes)) == 400
    assert resolver.calls == []


def test_an_oversized_head_gets_431() -> None:
    with _proxy(Resolver({}), max_head_bytes=1024) as proxy:
        response = _send(
            proxy,
            b"GET http://a.example.com/ HTTP/1.1\r\nX-Big: " + b"a" * 4096 + b"\r\n\r\n",
        )
    assert _status(response) == 431


def test_a_client_that_sends_no_head_is_dropped() -> None:
    with (
        _proxy(Resolver({}), head_seconds=0.3) as proxy,
        socket.create_connection(proxy.address, timeout=5) as conn,
    ):
        conn.sendall(b"GET http://a.example.com/")
        started = time.monotonic()
        response = _read_all(conn)
    assert _status(response) == 408
    assert time.monotonic() - started < 3


def test_an_idle_tunnel_is_closed(echo_site: Site) -> None:
    resolver = Resolver({"tunnel.example.com": ["127.0.0.1"]})
    with (
        _proxy(resolver, ports={echo_site.port}, idle_seconds=0.3) as proxy,
        socket.create_connection(proxy.address, timeout=5) as conn,
    ):
        conn.sendall(f"CONNECT tunnel.example.com:{echo_site.port} HTTP/1.1\r\n\r\n".encode())
        started = time.monotonic()
        assert _read_all(conn).startswith(b"HTTP/1.1 200")
    assert time.monotonic() - started < 3


def test_connections_beyond_the_limit_get_503(echo_site: Site, logs: Recorder) -> None:
    resolver = Resolver({"tunnel.example.com": ["127.0.0.1"]})
    with (
        _proxy(resolver, ports={echo_site.port}, max_connections=1) as proxy,
        socket.create_connection(proxy.address, timeout=5) as first,
    ):
        first.sendall(f"CONNECT tunnel.example.com:{echo_site.port} HTTP/1.1\r\n\r\n".encode())
        assert first.recv(4096).startswith(b"HTTP/1.1 200")
        assert _status(_send(proxy, b"CONNECT tunnel.example.com:1 HTTP/1.1\r\n\r\n")) == 503
        assert logs.named("egress.busy")
        first.sendall(b"still here")
        assert first.recv(4096) == b"still here"


def test_an_unreachable_site_is_a_bad_gateway() -> None:
    closed = socket.create_server(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    resolver = Resolver({"down.example.com": ["127.0.0.1"]})
    with _proxy(resolver, ports={port}) as proxy:
        response = _send(proxy, f"CONNECT down.example.com:{port} HTTP/1.1\r\n\r\n".encode())
    assert _status(response) == 502


def test_close_stops_accepting_and_cuts_open_tunnels_after_the_drain(echo_site: Site) -> None:
    resolver = Resolver({"tunnel.example.com": ["127.0.0.1"]})
    proxy = _proxy(resolver, ports={echo_site.port}).start()
    address = proxy.address
    with socket.create_connection(address, timeout=5) as conn:
        conn.sendall(f"CONNECT tunnel.example.com:{echo_site.port} HTTP/1.1\r\n\r\n".encode())
        assert conn.recv(4096).startswith(b"HTTP/1.1 200")
        started = time.monotonic()
        proxy.close(drain_seconds=0.3)
        assert 0.3 <= time.monotonic() - started < 3  # waited for the tunnel, then cut it
        assert _read_all(conn) == b""
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(address, timeout=1).close()


def test_command_line_options() -> None:
    args = parse_args(["--listen", "127.0.0.1:9999", "--ports", "443,80,8443"])
    assert args.listen == ("127.0.0.1", 9999)
    assert args.ports == frozenset({80, 443, 8443})
    defaults = parse_args([])
    assert defaults.listen == ("0.0.0.0", 8888)  # noqa: S104
    assert defaults.ports == frozenset({80, 443})
    with pytest.raises(SystemExit):
        parse_args(["--ports", "0"])


def test_the_proxy_url_is_a_valid_proxy_map_value() -> None:
    from kalkan.scanning.proxy import parse_template, validate_template

    validate_template("http://egress-proxy:8888")
    choice = parse_template("XX", "http://egress-proxy:8888", "abcd1234")
    assert choice.server == "http://egress-proxy:8888"
    assert choice.username is None
