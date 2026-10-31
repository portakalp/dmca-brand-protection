"""The local hop that keeps the proxy credentials out of the browser (K93)."""

from __future__ import annotations

import base64
import socket
import threading
from collections.abc import Iterator

import pytest

from kalkan.scanning.proxy import parse_template
from kalkan.scanning.proxy_forwarder import CredentialForwarder, rewrite_head

TOKEN = b"Basic " + base64.b64encode(b"user-abcd1234:s3cret")


def test_rewrite_replaces_the_browser_proxy_headers() -> None:
    head = (
        b"GET http://site.test/a HTTP/1.1\r\nHost: site.test\r\nProxy-Connection: keep-alive\r\n"
        b"Proxy-Authorization: Basic stale\r\nConnection: keep-alive\r\nAccept: */*"
    )
    rewritten = rewrite_head(head, TOKEN)
    assert rewritten == (
        b"GET http://site.test/a HTTP/1.1\r\nHost: site.test\r\nAccept: */*\r\n"
        b"Proxy-Authorization: " + TOKEN + b"\r\nConnection: close\r\n\r\n"
    )


def test_rewrite_keeps_a_tunnel_open() -> None:
    rewritten = rewrite_head(b"CONNECT site.test:443 HTTP/1.1\r\nHost: site.test:443", TOKEN)
    assert rewritten.endswith(b"Proxy-Authorization: " + TOKEN + b"\r\n\r\n")
    assert b"Connection: close" not in rewritten


class Upstream:
    """A stand-in for the residential proxy: records each request head, answers once."""

    def __init__(self) -> None:
        self.socket = socket.create_server(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.heads: list[bytes] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.socket.accept()
            except OSError:
                return
            with connection:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.heads.append(data.split(b"\r\n\r\n")[0])
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                )


@pytest.fixture
def upstream() -> Iterator[Upstream]:
    server = Upstream()
    yield server
    server.socket.close()


def _ask(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(request)
        answer = b""
        while chunk := client.recv(4096):
            answer += chunk
    return answer


def test_requests_reach_the_upstream_with_the_credentials(upstream: Upstream) -> None:
    proxy = parse_template(
        "PL", f"http://user-{{session}}:s3cret@127.0.0.1:{upstream.port}", "abcd1234"
    )
    with CredentialForwarder(proxy) as forwarder:
        assert set(forwarder.playwright()) == {"server"}
        port = int(forwarder.server.rsplit(":", 1)[1])
        answer = _ask(port, b"GET http://site.test/ HTTP/1.1\r\nHost: site.test\r\n\r\n")
    assert answer.endswith(b"\r\n\r\nok")
    [head] = upstream.heads
    assert b"Proxy-Authorization: " + TOKEN in head
    assert head.startswith(b"GET http://site.test/ HTTP/1.1")


def test_a_dead_upstream_closes_the_connection_without_an_answer() -> None:
    unused = socket.create_server(("127.0.0.1", 0))
    port = unused.getsockname()[1]
    unused.close()
    proxy = parse_template("PL", f"http://user:pass@127.0.0.1:{port}", "x")
    with CredentialForwarder(proxy) as forwarder:
        local = int(forwarder.server.rsplit(":", 1)[1])
        assert _ask(local, b"GET http://site.test/ HTTP/1.1\r\nHost: site.test\r\n\r\n") == b""


def test_closing_stops_listening(upstream: Upstream) -> None:
    proxy = parse_template("PL", f"http://user:pass@127.0.0.1:{upstream.port}", "x")
    forwarder = CredentialForwarder(proxy)
    with forwarder:
        port = int(forwarder.server.rsplit(":", 1)[1])
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


def test_a_connect_tunnel_carries_bytes_both_ways() -> None:
    heads: list[bytes] = []
    server = socket.create_server(("127.0.0.1", 0))

    def tunnel() -> None:
        connection, _ = server.accept()
        with connection:
            data = b""
            while b"\r\n\r\n" not in data:
                data += connection.recv(4096)
            heads.append(data.split(b"\r\n\r\n")[0])
            connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            connection.sendall(connection.recv(4096).upper())  # the "TLS" bytes, echoed

    threading.Thread(target=tunnel, daemon=True).start()
    proxy = parse_template("PL", f"http://user:pass@127.0.0.1:{server.getsockname()[1]}", "x")
    try:
        with CredentialForwarder(proxy) as forwarder:
            port = int(forwarder.server.rsplit(":", 1)[1])
            with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                client.sendall(b"CONNECT site.test:443 HTTP/1.1\r\nHost: site.test:443\r\n\r\n")
                assert client.recv(4096).startswith(b"HTTP/1.1 200")
                client.sendall(b"hello")
                assert client.recv(4096) == b"HELLO"
    finally:
        server.close()
    [head] = heads
    assert head.startswith(b"CONNECT site.test:443 HTTP/1.1")
    assert b"Proxy-Authorization: Basic " + base64.b64encode(b"user:pass") in head
