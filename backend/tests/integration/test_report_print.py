"""Printing a report with a real Chromium (architecture.md §21).

Two local HTTP servers stand in for the web app (``APP_BASE_URL``) and for any other address.
The app's print page tries to reach the other one every way a page can (image, script, fetch,
iframe, WebSocket, a link it follows) and a path of its own origin outside the base path. The
printer must reach none of them, send the token on the page request only, and return an A4 PDF.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kalkan.reports.printing import PrintError, print_pdf
from kalkan.scanning import capture

PAGE = re.compile(rb"/Type\s*/Page(?!s)")
REPORT_ID = "4f9d6f0e-5d41-4c55-9f1d-8a6f1f9b2c11"
TOKEN = "print-token-for-the-test"


@dataclass
class Server:
    port: int = 0
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    page: bytes = b""


def _serve(server: Server) -> tuple[ThreadingHTTPServer, threading.Thread]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            server.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            body = server.page if self.path.startswith("/app/print/") else b"ok"
            status = 200 if body else 404
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


@pytest.fixture
def servers() -> Iterator[tuple[Server, Server]]:
    app, other = Server(), Server()
    running = [_serve(app), _serve(other)]
    try:
        yield app, other
    finally:
        for httpd, thread in running:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        capture.shutdown()


def page(other: int) -> bytes:
    away = f"http://127.0.0.1:{other}"
    return f"""<!doctype html>
<html><head><title>Report</title>
<link rel="preconnect" href="http://localhost:{other}">
<link rel="stylesheet" href="{away}/style.css">
<script src="{away}/script.js"></script>
</head><body>
<h1>Monthly report</h1>
<img src="{away}/image.png" alt="">
<img src="http://localhost:{other}/by-name.png" alt="">
<iframe src="{away}/frame"></iframe>
<p style="page-break-after: always">Page one</p><p>Page two</p>
<script>
  fetch("{away}/fetch").catch(() => {{}});
  fetch("/outside").catch(() => {{}});
  try {{ new WebSocket("ws://127.0.0.1:{other}/socket"); }} catch (e) {{}}
  navigator.sendBeacon && navigator.sendBeacon("{away}/beacon", "x");
  setTimeout(() => document.documentElement.dataset.reportReady = "1", 300);
</script>
</body></html>""".encode()


def test_the_printer_reaches_only_the_app_and_sends_the_token_once(
    servers: tuple[Server, Server],
) -> None:
    app, other = servers
    app.page = page(other.port)
    pdf, blocked = print_pdf(REPORT_ID, TOKEN, f"http://127.0.0.1:{app.port}/app")

    assert pdf.startswith(b"%PDF-")
    assert len(PAGE.findall(pdf)) == 2
    assert other.requests == []
    assert blocked.count >= 5
    paths = [path for path, _ in app.requests]
    assert paths[0] == f"/app/print/report/{REPORT_ID}"
    assert "/outside" not in paths
    # The token travels in the header of the page request only, never in a URL.
    with_token = [path for path, headers in app.requests if headers.get("x-report-token")]
    assert with_token == [f"/app/print/report/{REPORT_ID}"]
    assert app.requests[0][1]["x-report-token"] == TOKEN
    assert all(TOKEN not in path for path in paths)


def test_a_page_that_does_not_answer_200_is_not_printed(servers: tuple[Server, Server]) -> None:
    app, _ = servers
    app.page = b""  # 404
    with pytest.raises(PrintError, match="404"):
        print_pdf(REPORT_ID, TOKEN, f"http://127.0.0.1:{app.port}/app")


def test_the_capture_runtime_starts_again_after_a_print(servers: tuple[Server, Server]) -> None:
    app, other = servers
    app.page = page(other.port)
    runtime = capture.runtime()
    runtime.browser()
    print_pdf(REPORT_ID, TOKEN, f"http://127.0.0.1:{app.port}/app")
    # The print closed the thread's capture browser (one Playwright per thread) ...
    assert not runtime.running
    # ... and the next capture launches it again, with the sentinel proxy.
    assert runtime.browser().is_connected()
