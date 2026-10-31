"""A local hop that holds the proxy credentials instead of Chromium (architecture.md §10; K93).

Given ``username`` and ``password``, Playwright 1.63 answers every authentication challenge
with them, a site's own ``401`` included, and Chromium records its ``Proxy-Authorization``
header in the HAR. So the browser never gets the credentials: its context proxy is this
forwarder on 127.0.0.1, which adds ``Proxy-Authorization`` to each request and passes the bytes
to the upstream proxy. A site asking for Basic authentication then gets nothing.

Every request is forwarded on its own upstream connection (``Connection: close``), so the
header goes into each request head; CONNECT tunnels (https, WebSocket) carry it once. The
forwarder only relays: it never resolves or connects to a candidate site itself.
"""

from __future__ import annotations

import base64
import socket
import threading
from contextlib import suppress
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from kalkan.logging import get_logger
from kalkan.scanning.proxy import ProxyChoice

if TYPE_CHECKING:
    from playwright.sync_api import ProxySettings

MAX_HEAD_BYTES = 64 * 1024
IDLE_SECONDS = 60.0
CONNECT_SECONDS = 15.0
_CHUNK = 64 * 1024
# Hop-by-hop headers the browser sends to its proxy; the forwarder sets its own.
_DROPPED = frozenset({b"proxy-authorization", b"proxy-connection", b"connection", b"keep-alive"})

log = get_logger(__name__)


def rewrite_head(head: bytes, authorization: bytes) -> bytes:
    """The request head for the upstream proxy: the browser's proxy headers are replaced by
    ``Proxy-Authorization`` and, outside CONNECT, ``Connection: close``."""
    lines = head.split(b"\r\n")
    request_line = lines[0]
    kept = [
        line
        for line in lines[1:]
        if line and line.split(b":", 1)[0].strip().lower() not in _DROPPED
    ]
    extra = [b"Proxy-Authorization: " + authorization]
    if not request_line.upper().startswith(b"CONNECT "):
        extra.append(b"Connection: close")
    return b"\r\n".join([request_line, *kept, *extra]) + b"\r\n\r\n"


def _read_head(sock: socket.socket) -> tuple[bytes, bytes] | None:
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > MAX_HEAD_BYTES:
            return None
        chunk = sock.recv(_CHUNK)
        if not chunk:
            return None
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _copy(source: socket.socket, target: socket.socket) -> None:
    try:
        while chunk := source.recv(_CHUNK):
            target.sendall(chunk)
    except OSError:
        pass
    finally:
        with suppress(OSError):
            target.shutdown(socket.SHUT_WR)


class CredentialForwarder:
    """A proxy on 127.0.0.1 for one job attempt; use it as a context manager."""

    def __init__(self, proxy: ProxyChoice) -> None:
        upstream = urlsplit(proxy.server)
        self._upstream = (upstream.hostname or "", upstream.port or 80)
        pair = f"{proxy.username or ''}:{proxy.password or ''}"
        self._authorization = b"Basic " + base64.b64encode(pair.encode())
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._sockets: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._serve, name="proxy-forwarder", daemon=True)

    @property
    def server(self) -> str:
        return f"http://127.0.0.1:{self._listener.getsockname()[1]}"

    def playwright(self) -> ProxySettings:
        return {"server": self.server}

    def __enter__(self) -> CredentialForwarder:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True
        # On Linux, close() alone neither wakes a thread blocked in accept() nor stops the
        # kernel socket listening; shutdown() does both. macOS refuses it (ENOTCONN) and
        # wakes accept() on close() instead.
        with suppress(OSError):
            self._listener.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            self._listener.close()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        with self._lock:
            open_sockets = list(self._sockets)
        for sock in open_sockets:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                sock.close()

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
            with suppress(OSError):
                sock.close()

    def _serve(self) -> None:
        while not self._closed:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            if not self._track(client):
                self._release(client)
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            client.settimeout(IDLE_SECONDS)
            read = _read_head(client)
            if read is None:
                return
            head, rest = read
            # A failed upstream connection closes the client without an answer, so the
            # browser sees a proxy failure (an error), not a page.
            upstream = socket.create_connection(self._upstream, timeout=CONNECT_SECONDS)
            if not self._track(upstream):
                return
            upstream.settimeout(IDLE_SECONDS)
            upstream.sendall(rewrite_head(head, self._authorization) + rest)
            sender = threading.Thread(target=_copy, args=(client, upstream), daemon=True)
            sender.start()
            _copy(upstream, client)
            sender.join(IDLE_SECONDS)
        except OSError as exc:
            log.info("capture.forwarder_connection_failed", error=type(exc).__name__)
        finally:
            self._release(client, upstream)
