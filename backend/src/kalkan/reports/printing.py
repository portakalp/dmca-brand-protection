"""Printing a monthly report to PDF in worker-browser (architecture.md §21, §10; K6).

The page is the web app's ``/print/report/<id>``; the print token goes in ``X-Report-Token`` on
that one request, never in the URL. The print is the only browser work without a proxy, so it
gets its own Chromium: launched without the capture runtime's sentinel proxy, it may reach
the print base URL (``REPORT_PRINT_BASE_URL``, else ``APP_BASE_URL``; see
:mod:`kalkan.workers.report_print`) and nothing else, enforced twice:

- Chromium resolves no host name but the app's (``--host-resolver-rules``), which also covers
  connections Playwright's routing never sees (pre-connects, DNS prefetch);
- every request of the context outside the app's origin and path is aborted, and no
  WebSocket is opened at all.

Service workers, downloads and permissions are off. The capture runtime of the thread is
closed first (one Playwright per thread), and the same watchdog kills a print that hangs.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from kalkan.logging import get_logger
from kalkan.scanning import capture

if TYPE_CHECKING:
    from playwright.sync_api import Route, ViewportSize, WebSocketRoute

TOKEN_HEADER = "x-report-token"  # noqa: S105 - a header name
READY_SELECTOR = "html[data-report-ready]"
BUDGET_SECONDS = 60.0
NAVIGATION_TIMEOUT_MS = 30_000
READY_TIMEOUT_MS = 20_000
# A4 at 96 dpi, so the layout is the paper's.
VIEWPORT: ViewportSize = {"width": 794, "height": 1123}
_DEFAULT_PORTS = {"http": 80, "https": 443}

log = get_logger(__name__)


class PrintError(RuntimeError):
    """The page did not print; ``build_report`` tries again with a new token."""


@dataclass(frozen=True)
class AppOrigin:
    """Where the printer may go: the print base URL's scheme, host, port and path."""

    scheme: str
    host: str
    port: int
    path: str

    @classmethod
    def parse(cls, base_url: str) -> AppOrigin:
        parts = urlsplit(base_url)
        if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
            raise PrintError("the print base URL must be an http(s) URL")
        if parts.username or parts.password:
            raise PrintError("the print base URL must not carry credentials")
        return cls(
            parts.scheme,
            parts.hostname.lower(),
            parts.port or _DEFAULT_PORTS[parts.scheme],
            parts.path.rstrip("/"),
        )

    def allows(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            port = parts.port or _DEFAULT_PORTS.get(parts.scheme.replace("ws", "http", 1))
        except ValueError:
            return False
        scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
        path = parts.path or "/"
        return (
            scheme == self.scheme
            and (parts.hostname or "").lower() == self.host
            and port == self.port
            and (path == self.path or path.startswith(self.path + "/"))
        )

    def url(self, path: str) -> str:
        default = _DEFAULT_PORTS[self.scheme] == self.port
        netloc = self.host if default else f"{self.host}:{self.port}"
        return f"{self.scheme}://{netloc}{self.path}{path}"

    def launch_args(self) -> list[str]:
        # An IP literal is never resolved, so the rule only matters for names.
        return [
            *capture.LAUNCH_ARGS,
            f"--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE {self.host}",
            "--dns-prefetch-disable",
        ]


@dataclass
class Blocked:
    """Requests the context refused (counted for the log and the tests; URLs are not kept)."""

    count: int = 0
    hosts: set[str] = field(default_factory=set)

    def add(self, url: str) -> None:
        self.count += 1
        self.hosts.add(urlsplit(url).hostname or "")


def _router(
    origin: AppOrigin, page_url: str, token: str, blocked: Blocked
) -> Callable[[Route], None]:
    def handle(route: Route) -> None:
        request = route.request
        if not origin.allows(request.url):
            blocked.add(request.url)
            route.abort("blockedbyclient")
            return
        if request.is_navigation_request() and request.url == page_url:
            route.continue_(headers={**request.headers, TOKEN_HEADER: token})
            return
        route.continue_()

    return handle


def _socket_router(blocked: Blocked) -> Callable[[WebSocketRoute], None]:
    # A routed WebSocket that never calls connect_to_server() reaches no server (Playwright's
    # mock mode); the print page needs none. Closing it here would block the sync API.
    def handle(socket: WebSocketRoute) -> None:
        blocked.add(socket.url)

    return handle


def print_pdf(report_id: str, token: str, base_url: str) -> tuple[bytes, Blocked]:
    """Open the print page and return the A4 PDF and what was blocked."""
    origin = AppOrigin.parse(base_url)
    page_url = origin.url(f"/print/report/{report_id}")
    capture.shutdown()  # the capture runtime's Playwright; the next capture starts it again
    runtime = capture.BrowserRuntime(proxy=None, args=origin.launch_args())
    blocked = Blocked()
    try:
        with capture.watchdog(runtime, BUDGET_SECONDS):
            browser = runtime.browser()
            context = browser.new_context(
                viewport=VIEWPORT,
                color_scheme="light",
                service_workers="block",
                accept_downloads=False,
                java_script_enabled=True,
            )
            try:
                context.route(re.compile(r".*"), _router(origin, page_url, token, blocked))
                context.route_web_socket(re.compile(r".*"), _socket_router(blocked))
                page = context.new_page()
                response = page.goto(page_url, wait_until="load", timeout=NAVIGATION_TIMEOUT_MS)
                if response is None or response.status != 200:
                    status = response.status if response else None
                    raise PrintError(f"the print page answered {status}")
                page.wait_for_selector(READY_SELECTOR, state="attached", timeout=READY_TIMEOUT_MS)
                pdf = page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
            finally:
                if runtime.usable():
                    context.close()
    finally:
        runtime.close()
    if blocked.count:
        log.info("report.print_blocked", report_id=report_id, requests=blocked.count)
    return pdf, blocked
