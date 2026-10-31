"""Stage 2: evidence capture with Playwright (architecture.md §10, §24; K6).

worker-browser runs one Chromium per process (:class:`BrowserRuntime`, created lazily in the
worker thread because Playwright's sync API is bound to its thread). Every job attempt opens a
fresh context per profile, with the job's proxy and the proxy country's locale and time zone,
and closes it before the next one. A proxy with credentials is reached through
``proxy_forwarder``, so Chromium never holds them (K93). Nothing is solved, clicked or
submitted: a bot challenge is recorded as it is (product.md §11).

One profile: navigate (``load`` within 30 s, then at most 3 s of network idle), viewport
screenshot, a bounded scroll for lazy content, back to the top, full-page screenshot (at most
10,000 px), HTML, the form and link inventory of every frame (``inventory.js``), the visible
text and the redirect chain. Media requests are aborted; images and fonts load. Downloads,
service workers, popups and permissions are off; dialogs are dismissed.

The files go to ``ARTIFACTS_ROOT/<job_id>.partial/`` with the names of
:mod:`kalkan.scanning.capture_contract`; ``meta.json`` is written last and the folder is
renamed to ``<job_id>/``, which worker-default's ``persist_capture`` reads.

Time: an attempt has a cooperative budget (:data:`JOB_BUDGET_SECONDS`). Some Playwright calls
have no timeout (``evaluate``, ``content``) and Dramatiq's time limit cannot reliably interrupt
them, so a watchdog kills Chromium :data:`KILL_GRACE_SECONDS` later (the driver too if that is
not enough); the blocked call then fails and the browser starts again on the next job.
Dramatiq's 120 s limit stays as the last resort.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dramatiq.middleware import TimeLimitExceeded
from pydantic import ValidationError

from kalkan.domain.enums import ArtifactKind, Profile
from kalkan.logging import get_logger
from kalkan.scanning import patterns
from kalkan.scanning.capture_contract import (
    FILES,
    MAX_BUTTONS,
    MAX_ERROR,
    MAX_FORMS,
    MAX_INPUTS_PER_FORM,
    MAX_LABEL,
    MAX_LINKS,
    MAX_REDIRECTS,
    MAX_TEXT,
    MAX_TITLE,
    MAX_URL,
    META_FILE,
    META_MAX_BYTES,
    PARTIAL_SUFFIX,
    ButtonInfo,
    CaptureJob,
    CaptureMeta,
    FormInfo,
    InputInfo,
    LinkInfo,
    Outcome,
    ProfileResult,
    file_name,
    job_dir,
)
from kalkan.scanning.form_patterns import button_intent, input_kind
from kalkan.scanning.profiles import chromium_major, context_options, market_for
from kalkan.scanning.proxy import ProxyChoice
from kalkan.scanning.proxy_forwarder import CredentialForwarder

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Frame, Page, Playwright, ProxySettings, Request, Route
    from playwright.sync_api import Response as PageResponse

# One attempt (both profiles) normally ends within this budget; Dramatiq's limit is 120 s.
JOB_BUDGET_SECONDS = 100.0
# The watchdog kills Chromium when a call without a timeout hangs this long past the budget,
# and the Playwright driver if the attempt still runs DRIVER_KILL_SECONDS later.
KILL_GRACE_SECONDS = 8.0
DRIVER_KILL_SECONDS = 5.0
NAVIGATION_TIMEOUT_MS = 30_000
NETWORK_IDLE_MS = 3_000
# Chromium commits its error page shortly after a failed navigation (some failures have none).
ERROR_PAGE_MS = 1_000
SCREENSHOT_TIMEOUT_MS = 15_000
ACTION_TIMEOUT_MS = 10_000
# A navigation or a scroll step does not start with less time than this left.
MIN_STEP_SECONDS = 2.0
SCROLL_STEPS = 5
SCROLL_MAX_PX = 10_000
SCROLL_PAUSE_MS = 250
FULL_PAGE_MAX_PX = 10_000
# Main-frame navigations per entry URL; stops script and meta-refresh loops. Chromium itself
# stops HTTP redirect loops after 20 hops (net::ERR_TOO_MANY_REDIRECTS).
MAX_NAVIGATIONS = 10
MAX_FRAMES = 10
# Elements of one kind the inventory looks at in a frame.
SCAN_ELEMENTS = 1_000
RESTART_EVERY = 50
LAUNCH_TIMEOUT_MS = 30_000
# Launch-level proxy where nothing listens: a context created without its own proxy cannot
# reach any address (fail closed). Playwright proxies loopback too (<-loopback>), so do contexts.
SENTINEL_PROXY: ProxySettings = {"server": "http://127.0.0.1:9"}
# The only variables Chromium inherits. Without a list it would get the worker's whole
# environment, and every renderer a hostile page runs in would hold PROXY_MAP and REDIS_URL.
BROWSER_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "FONTCONFIG_PATH",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "PLAYWRIGHT_BROWSERS_PATH",
)

LAUNCH_ARGS = (
    "--disable-dev-shm-usage",
    # WebRTC would send UDP around the HTTP proxy and show the VM's address (K93).
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
)
BLOCKED_RESOURCES = frozenset({"media"})
_HTTP = ("http://", "https://")

log = get_logger(__name__)


class BrowserUnavailableError(RuntimeError):
    """Chromium could not start: the worker's fault, not the site's. The job waits (§7)."""


class CaptureFailedError(RuntimeError):
    """Every profile of an attempt failed; Dramatiq retries the job (§7)."""

    def __init__(self, message: str, *, results: Sequence[ProfileResult] = ()) -> None:
        super().__init__(message)
        self.results = list(results)


def _now() -> datetime:
    return datetime.now(UTC)


def _cut(value: object, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _url(value: object) -> str | None:
    if isinstance(value, str) and value.startswith(_HTTP) and len(value) <= MAX_URL:
        return value
    return None


def _error(exc: BaseException) -> str:
    message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    # Playwright appends a call log; the first line names the failure.
    return f"{type(exc).__name__}: {str(message).splitlines()[0]}"[:MAX_ERROR]


# --- the browser process -----------------------------------------------------------------------


def _process_table() -> list[tuple[int, int]]:
    """(pid, parent pid) of every process: /proc on Linux, ``ps`` elsewhere (development)."""
    table: list[tuple[int, int]] = []
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            with suppress(OSError, ValueError, IndexError):
                stat = (entry / "stat").read_bytes()
                table.append((int(entry.name), int(stat.rsplit(b")", 1)[1].split()[1])))
        return table
    with suppress(OSError, subprocess.SubprocessError):
        listed = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="],  # noqa: S607 - a system tool, fixed arguments
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        ).stdout
        for line in listed.splitlines():
            with suppress(ValueError):
                pid, ppid = line.split()
                table.append((int(pid), int(ppid)))
    return table


def descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for pid, ppid in _process_table():
        children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), ()):
            found.append(child)
            stack.append(child)
    return found


def _connection(playwright: Playwright | None) -> Any:
    """Playwright's internal connection (1.63, pinned with the image; tests cover its use)."""
    return playwright._impl_obj._connection if playwright is not None else None


def _driver_pid(playwright: Playwright | None) -> int | None:
    """The Playwright driver (Node) process; Chromium runs below it in its own process group."""
    try:
        return int(_connection(playwright)._transport._proc.pid)
    except (AttributeError, TypeError, ValueError):
        return None


class BrowserRuntime:
    """The Chromium of one worker thread: launched lazily, replaced after a kill, a failure
    or :data:`RESTART_EVERY` jobs. Only :meth:`kill` may be called from another thread.

    After the driver is gone, a call of Playwright's sync API never returns (it waits on an
    event loop that no longer runs), and neither Dramatiq's time limit nor anything else
    interrupts it. So the watchdog kills Chromium first, which the driver reports as an error
    of the blocked call, and the driver only if that does not help; :meth:`usable` stops any
    further call once either happened.
    """

    def __init__(
        self,
        *,
        proxy: ProxySettings | None = SENTINEL_PROXY,
        args: Sequence[str] = LAUNCH_ARGS,
    ) -> None:
        # The report printer's runtime launches without the sentinel and with its own
        # arguments (architecture.md §21, kalkan.reports.printing).
        self._proxy, self._args = proxy, tuple(args)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._driver: int | None = None
        self._jobs = 0
        self.dirty = False

    @property
    def running(self) -> bool:
        return self._browser is not None

    def usable(self) -> bool:
        """Whether Playwright may still be called (not killed, the driver connected, its
        dispatcher alive: Dramatiq's time limit raised inside it ends it, and every later call
        would then spin forever)."""
        if self.dirty or self._playwright is None:
            return False
        try:
            connection = _connection(self._playwright)
            dispatcher = getattr(connection, "_dispatcher_fiber", None)
            if getattr(dispatcher, "dead", False):
                return False
            return not connection._transport.on_error_future.done()
        except AttributeError:  # pragma: no cover - a Playwright version without the attribute
            return True

    def browser(self) -> Browser:
        if self._browser is not None and (not self.usable() or not self._browser.is_connected()):
            self.close()
        if self._browser is None:
            self._browser = self._launch()
        return self._browser

    def _launch(self) -> Browser:
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        # Known before the launch, so the watchdog can kill a launch that hangs.
        self._playwright, self._driver = playwright, _driver_pid(playwright)
        try:
            # The sandbox is required (§10); without it the launch fails loudly (K104).
            browser = playwright.chromium.launch(
                headless=True,
                chromium_sandbox=True,
                proxy=self._proxy,
                args=list(self._args),
                timeout=LAUNCH_TIMEOUT_MS,
                env=browser_env(),
            )
        except BaseException:
            log.exception("capture.browser_launch_failed")
            self.close()
            raise
        self._jobs = 0  # dirty stays as it is: a kill during the launch still counts
        log.info("capture.browser_started", browser_version=browser.version)
        return browser

    def job_done(self) -> None:
        self._jobs += 1
        if self._jobs >= RESTART_EVERY:
            log.info("capture.browser_restart", jobs=self._jobs)
            self.close()

    def kill(self, *, driver: bool = False) -> None:
        """SIGKILL Chromium (every process below the driver) and, with ``driver``, the driver."""
        self.dirty = True
        pid = self._driver
        if pid is None:
            return
        for victim in [*descendants(pid), *([pid] if driver else [])]:
            with suppress(OSError):
                os.kill(victim, signal.SIGKILL)

    def close(self) -> None:
        playwright, browser = self._playwright, self._browser
        if playwright is None:
            return
        if not self.usable():
            # Killed or disconnected: no further call; stopping only reaps the driver.
            self.kill(driver=True)
        elif browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001 - whatever fails here, the processes are killed
                self.kill(driver=True)
        with suppress(Exception):
            playwright.stop()
        self._playwright = self._browser = None
        self._driver = None
        self.dirty = False


_local = threading.local()


def runtime() -> BrowserRuntime:
    """This thread's browser runtime."""
    current: BrowserRuntime | None = getattr(_local, "runtime", None)
    if current is None:
        current = _local.runtime = BrowserRuntime()
    return current


def shutdown() -> None:
    """Close this thread's browser (tests and process shutdown)."""
    current: BrowserRuntime | None = getattr(_local, "runtime", None)
    if current is not None:
        current.close()


@contextmanager
def watchdog(browser: BrowserRuntime, seconds: float) -> Iterator[threading.Event]:
    """Kill Chromium after ``seconds``, and the driver too if the attempt still runs
    :data:`DRIVER_KILL_SECONDS` later."""
    fired = threading.Event()
    done = threading.Event()

    def kill() -> None:
        fired.set()
        log.warning("capture.watchdog_killed_browser", after_seconds=seconds)
        browser.kill()
        if not done.wait(DRIVER_KILL_SECONDS):
            log.warning("capture.watchdog_killed_driver")
            browser.kill(driver=True)

    timer = threading.Timer(seconds, kill)
    timer.daemon = True
    timer.start()
    try:
        yield fired
    finally:
        done.set()
        timer.cancel()


# --- one profile -------------------------------------------------------------------------------


@cache
def inventory_script() -> str:
    return Path(__file__).with_name("inventory.js").read_text(encoding="utf-8")


def _limits(text: int) -> dict[str, int]:
    return {
        "label": MAX_LABEL,
        "url": MAX_URL,
        "title": MAX_TITLE,
        "text": text,
        "forms": MAX_FORMS,
        "inputs": MAX_INPUTS_PER_FORM,
        "buttons": MAX_BUTTONS,
        "links": MAX_LINKS,
        "scan": SCAN_ELEMENTS,
    }


def _input(raw: Mapping[str, Any]) -> InputInfo:
    maxlength = raw.get("maxlength")
    values = {key: _cut(raw.get(key), MAX_LABEL) for key in ("name", "id", "placeholder", "label")}
    input_type = _cut(raw.get("type"), 40) or "text"
    autocomplete = _cut(raw.get("autocomplete"), 40)
    return InputInfo(
        type=input_type,
        autocomplete=autocomplete,
        inputmode=_cut(raw.get("inputmode"), 40),
        maxlength=maxlength if isinstance(maxlength, int) and maxlength > 0 else None,
        visible=raw.get("visible") is not False,
        kind=input_kind(
            input_type=input_type,
            autocomplete=autocomplete,
            texts=(values["name"], values["id"], values["label"]),
            placeholder=values["placeholder"],
        ),
        **values,
    )


def _button(raw: Mapping[str, Any]) -> ButtonInfo:
    text = _cut(raw.get("text"), MAX_LABEL)
    return ButtonInfo(
        text=text,
        type=_cut(raw.get("type"), 40),
        target=_url(raw.get("target")),
        visible=raw.get("visible") is not False,
        intent=button_intent(text),
    )


def _items(raw: object, limit: int) -> list[Mapping[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw[:limit] if isinstance(item, dict)]


@dataclass
class Inventory:
    title: str | None = None
    text: str = ""
    forms: list[FormInfo] = field(default_factory=list)
    buttons: list[ButtonInfo] = field(default_factory=list)
    links: list[LinkInfo] = field(default_factory=list)

    @property
    def password_fields(self) -> int:
        return sum(1 for form in self.forms for item in form.inputs if item.type == "password")

    def add_frame(self, raw: object, frame_url: str | None) -> None:
        """Add one frame's ``inventory.js`` result; malformed parts are skipped."""
        if not isinstance(raw, dict):
            return
        if frame_url is None:
            self.title = _cut(raw.get("title"), MAX_TITLE) or None
            text = raw.get("text")
            self.text = text[:MAX_TEXT] if isinstance(text, str) else ""
        for form in _items(raw.get("forms"), MAX_FORMS - len(self.forms)):
            with suppress(ValidationError):
                self.forms.append(
                    FormInfo(
                        action=_url(form.get("action")),
                        method=_cut(form.get("method"), 40),
                        frame_url=frame_url,
                        inputs=[
                            _input(item) for item in _items(form.get("inputs"), MAX_INPUTS_PER_FORM)
                        ],
                        buttons=[
                            _button(item) for item in _items(form.get("buttons"), MAX_BUTTONS)
                        ],
                    )
                )
        for button in _items(raw.get("buttons"), MAX_BUTTONS - len(self.buttons)):
            with suppress(ValidationError):
                self.buttons.append(_button(button))
        for link in _items(raw.get("links"), MAX_LINKS * 2):
            url = _url(link.get("url"))
            tag = _cut(link.get("tag"), 40)
            if url and tag:
                text = _cut(link.get("text"), MAX_LABEL)
                self.links.append(LinkInfo(url=url, text=text, tag=tag))


# Which links survive the cap first: where the page sends people, then what it embeds.
_LINK_ORDER = {
    "redirect": 0,
    "navigation": 1,
    "meta-refresh": 2,
    "form": 3,
    "button": 4,
    "onclick": 5,
    "data-href": 6,
    "iframe": 7,
    "frame": 7,
}


def merge_links(links: Sequence[LinkInfo]) -> list[LinkInfo]:
    """Unique (url, tag) pairs, the most telling kinds first, capped at ``MAX_LINKS``."""
    seen: set[tuple[str, str]] = set()
    unique: list[LinkInfo] = []
    for link in links:
        key = (link.url, link.tag)
        if key not in seen:
            seen.add(key)
            unique.append(link)
    unique.sort(key=lambda link: _LINK_ORDER.get(link.tag, 9))  # stable: page order within a kind
    return unique[:MAX_LINKS]


@dataclass
class _PageState:
    """What the event handlers of one page saw."""

    page: Page | None = None
    navigations: int = 0
    answers: int = 0  # main-frame document responses, redirects included
    requests: list[str] = field(default_factory=list)  # main-frame document requests
    committed: list[str] = field(default_factory=list)  # main-frame URLs that loaded
    popups: list[str] = field(default_factory=list)


def _main_document(request: Request, page: Page | None) -> bool:
    if page is None or not request.is_navigation_request():
        return False
    return _frame_of(request) == page.main_frame


def _frame_of(request: Request) -> Frame | None:
    try:
        return request.frame
    except Exception:  # noqa: BLE001 - "not available": a new page's (popup's) first request
        return None


def _route(state: _PageState) -> Callable[[Route], None]:
    def handle(route: Route) -> None:
        request = route.request
        if request.resource_type in BLOCKED_RESOURCES:
            route.abort("blockedbyclient")
            return
        if request.is_navigation_request() and state.page is not None:
            frame = _frame_of(request)
            if frame is None or frame.page != state.page:
                # A popup's first navigation: recorded as a target, never loaded.
                if len(state.popups) < MAX_LINKS:
                    state.popups.append(request.url)
                route.abort("aborted")
                return
            if frame == state.page.main_frame:
                state.navigations += 1
                if state.navigations > MAX_NAVIGATIONS:
                    route.abort("aborted")
                    return
        route.fallback()

    return handle


def _remaining_ms(deadline: float, cap: int) -> int:
    return max(1, min(cap, int((deadline - time.monotonic()) * 1000)))


def _navigate(
    page: Page, url: str, state: _PageState, deadline: float
) -> tuple[PageResponse | None, str | None]:
    """``load`` within the navigation timeout (in two steps, so a page whose load event never
    fires is still captured), then at most 3 s of network idle."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    state.navigations = 0
    started = time.monotonic()
    timeout = _remaining_ms(deadline, NAVIGATION_TIMEOUT_MS)
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except PlaywrightTimeoutError:
        return None, "navigation timeout"
    except PlaywrightError as exc:
        return None, _error(exc)
    spent = int((time.monotonic() - started) * 1000)
    with suppress(PlaywrightTimeoutError):
        page.wait_for_load_state("load", timeout=_remaining_ms(deadline, max(1, timeout - spent)))
    with suppress(PlaywrightTimeoutError):
        page.wait_for_load_state("networkidle", timeout=_remaining_ms(deadline, NETWORK_IDLE_MS))
    return response, None


def _settle(page: Page, deadline: float) -> None:
    """Wait for the error page of a failed navigation: committed after ``goto`` has failed,
    it would interrupt the next entry URL's navigation."""
    from playwright.sync_api import Error as PlaywrightError

    with suppress(PlaywrightError):  # a timeout too: an aborted navigation shows none
        page.wait_for_url(
            lambda url: url.startswith("chrome-error:"),
            wait_until="commit",
            timeout=_remaining_ms(deadline, ERROR_PAGE_MS),
        )


def _scroll(page: Page, deadline: float) -> None:
    """Up to SCROLL_STEPS viewport heights (SCROLL_MAX_PX at most) for lazy content, then up."""
    for _ in range(SCROLL_STEPS):
        if deadline - time.monotonic() < MIN_STEP_SECONDS:
            break
        done = page.evaluate(
            """(cap) => {
                const height = Math.min(document.scrollingElement?.scrollHeight ?? 0, cap);
                window.scrollBy(0, window.innerHeight);
                return window.scrollY + window.innerHeight >= height;
            }""",
            SCROLL_MAX_PX,
        )
        page.wait_for_timeout(SCROLL_PAUSE_MS)
        if done:
            break
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(SCROLL_PAUSE_MS)


def _content(page: Page) -> str:
    from playwright.sync_api import Error as PlaywrightError

    try:
        return page.content()
    except PlaywrightError:  # "page is navigating": wait for the document and try once more
        page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
        return page.content()


def _inventory(page: Page) -> Inventory:
    from playwright.sync_api import Error as PlaywrightError

    inventory = Inventory()
    script = inventory_script()
    inventory.add_frame(page.main_frame.evaluate(script, _limits(MAX_TEXT)), None)
    frames: list[Frame] = [frame for frame in page.frames if frame != page.main_frame]
    for frame in frames[:MAX_FRAMES]:
        frame_url = _url(frame.url)
        if frame_url is None:
            continue
        with suppress(PlaywrightError):  # detached or navigating
            inventory.add_frame(frame.evaluate(script, _limits(0)), frame_url)
    return inventory


def _http_chain(response: PageResponse | None) -> list[str]:
    """Every URL before the final one (as stage 1 records it)."""
    if response is None:
        return []
    chain: list[str] = []
    request = response.request.redirected_from
    while request is not None and len(chain) < 100:
        chain.append(request.url)
        request = request.redirected_from
    return chain[::-1]


def _challenge(html: str, headers: Mapping[str, str], text: str, passwords: int) -> str | None:
    """The bot challenge on the page (K74 rules), named by the signature that matched."""
    known = patterns.patterns()
    lowered = html[:200_000].lower()
    if patterns.challenge(html, headers):
        for key, value in known.challenge_headers:
            if headers.get(key, "").lower() == value:
                return f"{key}: {value}"
        return next((found for found in known.challenge_texts if found in lowered), "challenge")
    if patterns.captcha_wall(html, text, passwords):
        return next((found for found in known.captcha_widgets if found in lowered), "captcha")
    return None


def _write(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)


def _keep(path: Path, kind: ArtifactKind) -> bool:
    """Whether ``path`` exists within its size cap; an oversized file is removed."""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if 0 < size <= FILES[kind].max_bytes:
        return True
    path.unlink(missing_ok=True)
    log.warning("capture.file_dropped", kind=kind.value, bytes=size)
    return False


def _proxy_secrets(proxy: ProxyChoice) -> list[str]:
    if proxy.username is None:
        return []
    pair = f"{proxy.username}:{proxy.password or ''}"
    found = [base64.b64encode(pair.encode()).decode(), pair]
    # A short user name or password could match unrelated text and cost the HAR.
    found += [part for part in (proxy.username, proxy.password or "") if len(part) >= 8]
    return found


def scrub_har(path: Path, proxy: ProxyChoice) -> None:
    """Remove the proxy credentials from a HAR (defence in depth).

    Chromium records the raw request headers, ``Proxy-Authorization`` included, if it ever
    holds the credentials; with the forwarder it does not (K93). The HAR is removed when a
    credential is still found after the headers are dropped.
    """
    found = _proxy_secrets(proxy)
    if not found or not path.exists():
        return
    try:
        har = json.loads(path.read_bytes())
        entries = har["log"]["entries"]
        for entry in entries:
            request = entry["request"]
            request["headers"] = [
                header
                for header in request.get("headers", [])
                if str(header.get("name", "")).lower() != "proxy-authorization"
                and not any(secret in str(header.get("value", "")) for secret in found)
            ]
        data = json.dumps(har, ensure_ascii=False).encode()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        data = b""
    if not data or any(secret.encode() in data for secret in found):
        path.unlink(missing_ok=True)
        log.warning("capture.har_dropped", reason="proxy credentials")
        return
    path.write_bytes(data)


def har_bytes(path: Path) -> int:
    """Approximate bytes through the proxy: request headers and bodies plus response transfer
    sizes (CDP encodedDataLength). TLS and CONNECT overhead are not included."""
    try:
        entries = json.loads(path.read_bytes())["log"]["entries"]
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    total = 0
    for entry in entries if isinstance(entries, list) else ():
        with suppress(AttributeError, TypeError):
            request, response = entry["request"], entry["response"]
            total += max(request.get("headersSize", 0), 0) + max(request.get("bodySize", 0), 0)
            transfer = response.get("_transferSize", -1)
            if isinstance(transfer, int) and transfer > 0:
                total += transfer
            else:
                total += max(response.get("headersSize", 0), 0)
                total += max(response.get("bodySize", 0), 0)
    return total


@dataclass
class _Visit:
    """What one profile's visit produced."""

    response: PageResponse | None = None
    error: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    html: str | None = None
    inventory: Inventory = field(default_factory=Inventory)
    final_url: str | None = None


def _visit(
    page: Page,
    job: CaptureJob,
    names: Mapping[ArtifactKind, Path],
    width: int,
    state: _PageState,
    deadline: float,
    usable: Callable[[], bool],
) -> _Visit:
    from playwright.sync_api import Error as PlaywrightError

    visit = _Visit()
    for index, url in enumerate(job.urls):
        if not usable():
            visit.error = "the browser was stopped after the time budget"
            return visit
        if deadline - time.monotonic() < MIN_STEP_SECONDS:
            visit.error = visit.error or "time budget exhausted"
            break
        if index:
            _settle(page, deadline)
        answers = state.answers
        visit.response, visit.error = _navigate(page, url, state, deadline)
        if visit.response is not None or visit.error == "navigation timeout":
            break
        if state.answers > answers:
            # The site answered (a redirect loop, a download): its failure is the result. The
            # next entry URL could only show the proxy's own error page (K92).
            break
    rendered = visit.response is not None or visit.error == "navigation timeout"
    if not rendered or not page.url.startswith(_HTTP):
        return visit  # nothing rendered (a network error, a download): the HAR is the evidence
    visit.final_url = _url(page.url)
    if visit.response is not None:
        with suppress(PlaywrightError):
            visit.headers = visit.response.all_headers()

    def viewport() -> None:
        page.screenshot(
            path=names[ArtifactKind.SCREENSHOT],
            type="png",
            scale="css",
            timeout=_remaining_ms(deadline, SCREENSHOT_TIMEOUT_MS),
        )

    def full_page() -> None:
        _scroll(page, deadline)
        page.screenshot(
            path=names[ArtifactKind.SCREENSHOT_FULL],
            type="png",
            scale="css",
            full_page=True,
            clip={"x": 0, "y": 0, "width": width, "height": FULL_PAGE_MAX_PX},
            timeout=_remaining_ms(deadline, SCREENSHOT_TIMEOUT_MS),
        )

    def document() -> None:
        visit.html = _content(page)
        _write(names[ArtifactKind.HTML], visit.html.encode("utf-8", "replace"))

    def forms() -> None:
        visit.inventory = _inventory(page)

    # (name, step, required): without the viewport screenshot and the HTML the profile is an
    # error; a missing full-page screenshot or inventory is only logged.
    steps: list[tuple[str, Callable[[], None], bool]] = [
        ("screenshot", viewport, True),
        ("screenshot_full", full_page, False),
        ("html", document, True),
        ("inventory", forms, False),
    ]
    for step, run, required in steps:
        if not usable():
            visit.error = visit.error or "the browser was stopped after the time budget"
            break
        try:
            run()
        except PlaywrightError as exc:
            log.info("capture.step_failed", step=step, error=_error(exc))
            if required:
                visit.error = visit.error or f"{step}: {_error(exc)}"
    if usable():
        visit.final_url = _url(page.url) or visit.final_url
    return visit


def _profile_result(
    job: CaptureJob,
    profile: Profile,
    started: datetime,
    names: Mapping[ArtifactKind, Path],
    state: _PageState,
    visit: _Visit,
) -> ProfileResult:
    files = [kind for kind in FILES if _keep(names[kind], kind)]
    inventory = visit.inventory
    challenge = None
    if visit.html is not None:
        challenge = _challenge(visit.html, visit.headers, inventory.text, inventory.password_fields)
    error = visit.error
    if challenge is not None:
        outcome = Outcome.CHALLENGE
    elif error is None and ArtifactKind.SCREENSHOT in files and ArtifactKind.HTML in files:
        outcome = Outcome.OK
    else:
        outcome = Outcome.ERROR
        error = error or "no screenshot or HTML"
    # Every URL before the final one, as stage 1 records it; without a response, the requests.
    chain = _http_chain(visit.response) if visit.response is not None else state.requests[:-1]
    chain = [url for url in chain if _url(url)][:MAX_REDIRECTS]
    hops = [*chain[1:], visit.final_url] if chain else []
    targets = [LinkInfo(url=url, tag="redirect") for url in hops if url]
    seen = {*job.urls, *chain, visit.final_url}
    targets += [
        LinkInfo(url=url, tag="navigation")
        for url in dict.fromkeys([*state.committed, *state.popups])
        if url not in seen and _url(url)
    ]
    return ProfileResult(
        profile=profile,
        outcome=outcome,
        started_at=started,
        finished_at=_now(),
        http_status=visit.response.status if visit.response is not None else None,
        final_url=visit.final_url,
        redirect_chain=chain,
        title=inventory.title,
        text_excerpt=inventory.text,
        forms=inventory.forms,
        buttons=inventory.buttons,
        links=merge_links([*targets, *inventory.links]),
        challenge=challenge[:MAX_LABEL] if challenge else None,
        error=error[:MAX_ERROR] if error else None,
        files=files,
        proxy_bytes=har_bytes(names[ArtifactKind.HAR]) if ArtifactKind.HAR in files else 0,
    )


def capture_profile(
    browser: Browser,
    job: CaptureJob,
    profile: Profile,
    proxy: ProxyChoice,
    out: Path,
    *,
    route: ProxySettings,
    deadline: float,
    usable: Callable[[], bool] = lambda: True,
) -> ProfileResult:
    """Capture ``job`` with one profile into ``out``, the context going through ``route``
    (``proxy`` itself, or the forwarder that holds its credentials). Page failures become an
    ``error`` result; only a failure of the browser itself raises."""
    started = _now()
    state = _PageState()
    names = {kind: out / file_name(kind, profile) for kind in FILES}
    options = context_options(profile, market_for(proxy.country), chromium_major(browser.version))
    context = browser.new_context(
        proxy=route,
        accept_downloads=False,
        service_workers="block",
        # Evidence of a site with a broken certificate is still evidence.
        ignore_https_errors=True,
        record_har_path=names[ArtifactKind.HAR],
        record_har_content="omit",
        record_har_mode="full",  # "minimal" leaves out the sizes the byte count needs
        **options,
    )
    try:
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        context.route("**/*", _route(state))
        page = context.new_page()
        state.page = page

        def on_page(popup: Page) -> None:
            if popup != page:
                with suppress(Exception):
                    popup.close()

        def on_request(request: Request) -> None:
            if _main_document(request, page) and len(state.requests) < MAX_REDIRECTS * 2:
                state.requests.append(request.url)

        def on_response(response: PageResponse) -> None:
            if _main_document(response.request, page):
                state.answers += 1

        def on_navigated(frame: Frame) -> None:
            if frame == page.main_frame and len(state.committed) < MAX_LINKS:
                state.committed.append(frame.url)

        context.on("page", on_page)
        page.on("request", on_request)
        page.on("response", on_response)
        page.on("framenavigated", on_navigated)
        visit = _visit(page, job, names, options["viewport"]["width"], state, deadline, usable)
    finally:
        if usable():  # after a kill, a call could block forever
            with suppress(Exception):
                context.close()  # writes the HAR
        scrub_har(names[ArtifactKind.HAR], proxy)
    return _profile_result(job, profile, started, names, state, visit)


# --- one attempt ---------------------------------------------------------------------------------


def failed_result(profile: Profile, error: str, *, at: datetime) -> ProfileResult:
    return ProfileResult(
        profile=profile, outcome=Outcome.ERROR, started_at=at, finished_at=_now(), error=error
    )


def fit_meta(meta: CaptureMeta) -> bytes:
    """``meta.json`` within META_MAX_BYTES: links, buttons, forms and text give way in turn."""
    data = meta.model_dump_json().encode()
    for trim in ("links", "buttons", "forms", "text_excerpt"):
        if len(data) <= META_MAX_BYTES:
            break
        empty: Any = "" if trim == "text_excerpt" else []
        meta = meta.model_copy(
            update={"profiles": [p.model_copy(update={trim: empty}) for p in meta.profiles]}
        )
        data = meta.model_dump_json().encode()
    return data


def hand_off(partial: Path, meta: CaptureMeta, results: Sequence[ProfileResult]) -> Path:
    """Write ``meta.json`` last, remove every unlisted file and rename ``partial`` to its job
    folder, which ``persist_capture`` then reads."""
    listed = {META_FILE} | {
        file_name(kind, result.profile) for result in results for kind in result.files
    }
    for path in partial.iterdir():
        if path.name not in listed:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
    _write(partial / META_FILE, fit_meta(meta))
    target = partial.with_name(partial.name.removesuffix(PARTIAL_SUFFIX))
    partial.rename(target)
    return target


def browser_env(environ: Mapping[str, str] | None = None) -> dict[str, str | float | bool]:
    """The allow-listed environment Chromium starts with (no secrets)."""
    source = os.environ if environ is None else environ
    return {name: source[name] for name in BROWSER_ENV if name in source}


def run_job(
    job: CaptureJob,
    proxy: ProxyChoice,
    *,
    root: Path,
    final: bool,
    browser: BrowserRuntime | None = None,
    budget: float | None = None,
) -> tuple[Path, CaptureMeta]:
    """One attempt of ``job``: capture every profile and hand off the job folder.

    Raises :class:`CaptureFailedError` (after removing its partial folder) when every profile
    failed and this is not the final attempt; on the final attempt the folder is handed off
    with ``final_attempt=true`` so worker-default records the failure (§7).
    """
    browser = browser or runtime()
    budget = JOB_BUDGET_SECONDS if budget is None else budget
    started = _now()
    deadline = time.monotonic() + budget
    target = job_dir(job.job_id, root)
    partial = target.with_name(target.name + PARTIAL_SUFFIX)
    shutil.rmtree(partial, ignore_errors=True)  # an earlier attempt that died midway
    partial.mkdir(parents=True)
    results: list[ProfileResult] = []
    version = ""
    failure: str | None = None
    try:
        with ExitStack() as stack:
            stop = stack.enter_context(watchdog(browser, budget + KILL_GRACE_SECONDS))
            route = proxy.playwright() if proxy.username is None else None
            if route is None:  # the browser never holds the credentials (K93)
                route = stack.enter_context(CredentialForwarder(proxy)).playwright()
            try:
                chromium = browser.browser()
            except Exception as exc:  # not this site's fault: the job waits instead (§7)
                raise BrowserUnavailableError(_error(exc)) from exc
            version = chromium.version
            for index, profile in enumerate(job.profiles):
                if stop.is_set() or not browser.usable():
                    break
                # Each profile gets an equal share of what is left.
                share = (deadline - time.monotonic()) / (len(job.profiles) - index)
                results.append(
                    capture_profile(
                        chromium,
                        job,
                        profile,
                        proxy,
                        partial,
                        route=route,
                        deadline=time.monotonic() + share,
                        usable=lambda: not stop.is_set() and browser.usable(),
                    )
                )
        browser.job_done()
    except BrowserUnavailableError:
        browser.dirty = True
        browser.close()
        shutil.rmtree(partial, ignore_errors=True)
        raise
    except (Exception, TimeLimitExceeded) as exc:
        # Anything unexpected (a launch failure, Dramatiq's time limit): the browser may be in
        # any state, so it is replaced on the next job.
        log.exception("capture.attempt_failed", job_id=str(job.job_id))
        browser.dirty = True
        failure = _error(exc)
    if browser.dirty:
        failure = failure or "the browser was stopped after the time budget"
        browser.close()
    done = {result.profile for result in results}
    results += [
        failed_result(profile, failure or "not captured", at=started)
        for profile in job.profiles
        if profile not in done
    ]
    failed = all(result.outcome is Outcome.ERROR for result in results)
    if failed and not final:
        shutil.rmtree(partial, ignore_errors=True)
        raise CaptureFailedError(failure or "every profile failed", results=results)
    meta = CaptureMeta(
        job_id=job.job_id,
        final_attempt=failed,
        proxy_country=proxy.country,
        browser_version=version[:MAX_LABEL],
        profiles=results,
        error=(failure or results[0].error) if failed else failure,
    )
    return hand_off(partial, meta, results), meta
