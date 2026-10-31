"""Stage 2 with a real Chromium (architecture.md §10, §25).

The fixture sites are reached through the local dev-proxy (``make up-infra``, published at
127.0.0.1:18888), exactly like the worker reaches candidate sites through its residential
proxy. A few tests use :class:`LocalProxy` instead, an in-process HTTP proxy that answers
every request itself: it requires credentials, records what arrives and serves pages the
fixture sites cannot (a page that hangs the renderer, a 401). No test leaves the machine.
"""

from __future__ import annotations

import base64
import json
import socket
import socketserver
import struct
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from dramatiq import Worker
from dramatiq.brokers.redis import RedisBroker

from kalkan.config import REPO_ROOT
from kalkan.domain.enums import ArtifactKind, Profile
from kalkan.scanning import capture
from kalkan.scanning.capture import BrowserRuntime, CaptureFailedError, run_job
from kalkan.scanning.capture_contract import (
    META_FILE,
    PERSIST_QUEUE,
    CaptureJob,
    CaptureMeta,
    Outcome,
    ProfileResult,
    Purpose,
    file_name,
)
from kalkan.scanning.profiles import CHROMIUM_MAJOR
from kalkan.scanning.proxy import new_session, parse_template
from kalkan.workers import capture as worker
from tests.support import make_settings

DEV_PROXY = ("127.0.0.1", 18888)
DEV_PROXY_URL = "http://127.0.0.1:18888"
HINT = (
    "start the services with `make up-infra` (dev-proxy on 127.0.0.1:18888) and install "
    "Chromium with `uv run playwright install chromium`"
)
PNG = b"\x89PNG\r\n\x1a\n"
SIZES = {Profile.DESKTOP: (1280, 800), Profile.MOBILE: (390, 844)}
CLONE_HTML = (REPO_ROOT / "fixtures" / "sites" / "clone" / "index.html").read_bytes()

# A page of LocalProxy: (request URL, request headers) -> (status, headers, body).
Page = Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]


# --- helpers -------------------------------------------------------------------------------------


class LocalProxy:
    """An HTTP proxy on 127.0.0.1 that answers every request itself, from ``pages`` by host;
    with ``credentials`` it demands Basic proxy authentication first (407)."""

    def __init__(self, pages: dict[str, Page], credentials: str | None = None) -> None:
        self.pages = pages
        self.credentials = credentials
        self.requests: list[tuple[str, str | None]] = []  # request line, Proxy-Authorization
        self.authorizations: list[str] = []  # Authorization headers meant for a site
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                self.request.settimeout(10)
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = self.request.recv(65536)
                    if not chunk:
                        return
                    data += chunk
                lines = data.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
                headers: dict[str, str] = {}
                for line in lines[1:]:
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()
                proxy.requests.append((lines[0], headers.get("proxy-authorization")))
                if "authorization" in headers:
                    proxy.authorizations.append(headers["authorization"])
                if proxy.credentials is not None:
                    expected = "Basic " + base64.b64encode(proxy.credentials.encode()).decode()
                    if headers.get("proxy-authorization") != expected:
                        self.reply(407, {"Proxy-Authenticate": 'Basic realm="test"'}, b"")
                        return
                target = lines[0].split(" ")[1] if " " in lines[0] else ""
                page = proxy.pages.get(urlsplit(target).hostname or "")
                if page is None:
                    self.reply(404, {"Content-Type": "text/plain"}, b"not here")
                    return
                self.reply(*page(target, headers))

            def reply(self, status: int, headers: dict[str, str], body: bytes) -> None:
                head = [f"HTTP/1.1 {status} Test", f"Content-Length: {len(body)}"]
                head += [
                    "Connection: close",
                    *(f"{key}: {value}" for key, value in headers.items()),
                ]
                self.request.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> LocalProxy:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    def url(self, userinfo: str = "") -> str:
        return f"http://{userinfo + '@' if userinfo else ''}127.0.0.1:{self.port}"


class Listener:
    """A TCP listener on 127.0.0.1 that counts connections (it must never get one)."""

    def __init__(self) -> None:
        self.socket = socket.create_server(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.connections = 0
        self.thread = threading.Thread(target=self._accept, daemon=True)

    def _accept(self) -> None:
        with suppress(OSError):  # closed at the end of the test
            while True:
                connection, _ = self.socket.accept()
                self.connections += 1
                connection.close()

    def __enter__(self) -> Listener:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.socket.close()


class Recorder:
    """Stands in for a module's structlog logger and keeps every line."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def _record(self, event: str, *args: Any, **values: Any) -> None:
        self.lines.append(f"{event} {args!r} {values!r}")
        if sys.exc_info()[0] is not None:
            self.lines.append(traceback.format_exc())

    info = warning = error = debug = exception = _record


def html_page(body: bytes) -> Page:
    return lambda url, headers: (200, {"Content-Type": "text/html; charset=utf-8"}, body)


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    assert data[:8] == PNG
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _job(url: str, *profiles: Profile, country: str | None = "PL") -> CaptureJob:
    return CaptureJob(
        job_id=uuid.uuid4(),
        purpose=Purpose.CASE,
        urls=[url],
        country=country,
        profiles=list(profiles or (Profile.DESKTOP, Profile.MOBILE)),
        issued_at=datetime.now(UTC),
    )


def _capture(
    root: Path, url: str, *, proxy: str = DEV_PROXY_URL, final: bool = False, **values: Any
) -> tuple[Path, CaptureMeta]:
    choice = parse_template("PL", proxy, new_session())
    return run_job(_job(url), choice, root=root, final=final, browser=capture.runtime(), **values)


def _profile(meta: CaptureMeta, profile: Profile) -> ProfileResult:
    return next(result for result in meta.profiles if result.profile is profile)


def _kinds(result: ProfileResult) -> set[str]:
    return {item.kind for form in result.forms for item in form.inputs}


# --- fixtures ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def browser() -> Iterator[BrowserRuntime]:
    try:
        socket.create_connection(DEV_PROXY, timeout=2).close()
    except OSError:
        pytest.fail(f"dev-proxy is not reachable at 127.0.0.1:18888; {HINT}")
    runtime = capture.runtime()
    try:
        runtime.browser()
    except Exception as exc:  # noqa: BLE001 - reported with the setup hint
        pytest.fail(f"Chromium does not start ({type(exc).__name__}); {HINT}")
    yield runtime
    capture.shutdown()


@pytest.fixture
def queues(redis_broker: RedisBroker) -> Iterator[RedisBroker]:
    """The test broker with the persist queue emptied too (no actor declares it here)."""
    keys = [f"dramatiq:{PERSIST_QUEUE}", f"dramatiq:{PERSIST_QUEUE}.msgs"]
    redis_broker.client.delete(*keys)
    yield redis_broker
    redis_broker.client.delete(*keys)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(capture, "log", recorder)
    monkeypatch.setattr(worker, "log", recorder)
    return recorder


# --- the fixture sites ---------------------------------------------------------------------------


def test_clone_is_captured_with_both_profiles(browser: BrowserRuntime, tmp_path: Path) -> None:
    folder, meta = _capture(tmp_path, "http://mintera-login.test/")

    assert meta.final_attempt is False
    assert meta.proxy_country == "PL"
    assert meta.browser_version.split(".")[0] == str(CHROMIUM_MAJOR)
    for profile, size in SIZES.items():
        result = _profile(meta, profile)
        assert result.outcome is Outcome.OK, result.error
        assert result.http_status == 200
        assert result.final_url == "http://mintera-login.test/"
        assert result.title == "Mintera Bank | Login"
        assert set(result.files) == {
            ArtifactKind.SCREENSHOT,
            ArtifactKind.SCREENSHOT_FULL,
            ArtifactKind.HTML,
            ArtifactKind.HAR,
        }
        # Acceptance 1: the viewport screenshot has the profile's size in CSS pixels.
        assert png_size(folder / file_name(ArtifactKind.SCREENSHOT, profile)) == size
        full = png_size(folder / file_name(ArtifactKind.SCREENSHOT_FULL, profile))
        assert full[0] == size[0]
        assert size[1] <= full[1] <= 10_000
        html = (folder / file_name(ArtifactKind.HTML, profile)).read_text()
        assert "Mintera Bank | Login" in html
        har = json.loads((folder / file_name(ArtifactKind.HAR, profile)).read_bytes())
        [document] = [
            entry for entry in har["log"]["entries"] if entry["request"]["url"].endswith("/")
        ]
        assert "text" not in document["response"]["content"]  # content: omit
        assert result.proxy_bytes > 0
        # Acceptance 2: the password and the card field are found.
        assert {"password", "card_number"} <= _kinds(result)
        [form] = result.forms
        assert form.action == "http://mintera-login.test/collect"
        assert form.method == "post"
        assert [button.intent for button in form.buttons] == ["login"]
        assert "Verify your card" in result.text_excerpt
    assert json.loads((folder / META_FILE).read_bytes())["job_id"] == str(meta.job_id)


def test_mobile_cloaking_shows_different_content(browser: BrowserRuntime, tmp_path: Path) -> None:
    _, meta = _capture(tmp_path, "http://mintera-mobile.test/")

    desktop, mobile = _profile(meta, Profile.DESKTOP), _profile(meta, Profile.MOBILE)
    assert (desktop.outcome, mobile.outcome) == (Outcome.OK, Outcome.OK)
    assert desktop.title == "Weekend recipes"
    assert desktop.forms == []
    assert mobile.title is not None
    assert "Mintera Bank" in mobile.title
    assert {"phone", "password"} <= _kinds(mobile)


def test_a_challenge_is_recorded_not_solved(browser: BrowserRuntime, tmp_path: Path) -> None:
    folder, meta = _capture(tmp_path, "http://mintera-challenge.test/")

    for result in meta.profiles:
        assert result.outcome is Outcome.CHALLENGE
        assert result.http_status == 403
        assert result.challenge == "cf-mitigated: challenge"
        assert ArtifactKind.SCREENSHOT in result.files
    assert meta.final_attempt is False
    assert (folder / META_FILE).exists()


def test_a_redirect_to_the_official_site_is_recorded(
    browser: BrowserRuntime, tmp_path: Path
) -> None:
    _, meta = _capture(tmp_path, "http://mintera-redirect.test/")

    for result in meta.profiles:
        assert result.outcome is Outcome.OK
        assert result.redirect_chain == ["http://mintera-redirect.test/"]
        assert result.final_url == "http://mintera.test/"
        assert ("redirect", "http://mintera.test/") in {
            (link.tag, link.url) for link in result.links
        }


def test_buttons_of_a_login_address_page_are_targets(
    browser: BrowserRuntime, tmp_path: Path
) -> None:
    _, meta = _capture(tmp_path, "http://mintera-verify.test/")

    result = _profile(meta, Profile.DESKTOP)
    targets = {(link.tag, link.url) for link in result.links}
    assert ("button", "http://mintera-login.test/") in targets or (
        "a",
        "http://mintera-login.test/",
    ) in targets
    assert any(link.url.startswith("https://quick-loans.example/") for link in result.links)
    assert "login" in {button.intent for button in result.buttons}


def test_an_endless_redirect_fails_and_is_retried(browser: BrowserRuntime, tmp_path: Path) -> None:
    with pytest.raises(CaptureFailedError) as raised:
        _capture(tmp_path, "http://mintera-loop.test/")
    assert all("ERR_TOO_MANY_REDIRECTS" in (r.error or "") for r in raised.value.results)
    assert list(tmp_path.iterdir()) == []

    folder, meta = _capture(tmp_path, "http://mintera-loop.test/", final=True)
    assert meta.final_attempt is True
    assert {result.outcome for result in meta.profiles} == {Outcome.ERROR}
    assert "ERR_TOO_MANY_REDIRECTS" in (meta.error or "")
    assert (folder / META_FILE).exists()


def test_a_script_navigation_loop_is_capped(browser: BrowserRuntime, tmp_path: Path) -> None:
    started = time.monotonic()
    _, meta = _capture(tmp_path, "http://mintera-loop.test/js")

    assert time.monotonic() - started < 60
    for result in meta.profiles:
        assert result.outcome is Outcome.OK, result.error
        navigations = [link for link in result.links if link.tag == "navigation"]
        assert 0 < len(navigations) <= capture.MAX_NAVIGATIONS


def test_a_heavy_page_is_capped_and_its_video_aborted(
    browser: BrowserRuntime, tmp_path: Path
) -> None:
    started = time.monotonic()
    folder, meta = _capture(tmp_path, "http://mintera-heavy.test/")

    assert time.monotonic() - started < capture.JOB_BUDGET_SECONDS
    for profile, (width, _) in SIZES.items():
        result = _profile(meta, profile)
        assert result.outcome is Outcome.OK, result.error
        assert png_size(folder / file_name(ArtifactKind.SCREENSHOT_FULL, profile)) == (
            width,
            10_000,
        )
        har = json.loads((folder / file_name(ArtifactKind.HAR, profile)).read_bytes())
        media = [entry for entry in har["log"]["entries"] if "/media/" in entry["request"]["url"]]
        assert media, "the page's video was never requested"
        for entry in media:
            assert entry["response"]["status"] in (0, -1)
            assert "BLOCKED_BY_CLIENT" in entry["response"].get("_failureText", "")
        assert len(result.links) == 200  # capped


# --- proxy: fail closed, loopback, credentials ---------------------------------------------------


def test_without_a_proxy_no_page_is_opened(
    browser: BrowserRuntime,
    queues: RedisBroker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 5: the job is deferred as a new message; nothing is captured or failed."""

    def no_browser(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a page was opened without a proxy")

    monkeypatch.setattr(capture, "run_job", no_browser)
    monkeypatch.setattr(worker, "get_settings", lambda: make_settings(env="local", proxy_map={}))
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    job = _job("http://mintera-login.test/")

    worker.browser_capture.fn(job.model_dump(mode="json"))

    client = queues.client
    assert client.llen(f"dramatiq:{PERSIST_QUEUE}") == 0
    [encoded] = client.hvals("dramatiq:capture.DQ.msgs")
    message = json.loads(encoded)
    assert message["args"] == [job.model_dump(mode="json")]
    assert "retries" not in message["options"]  # no retry used
    eta = message["options"]["eta"] / 1000
    assert 29 * 60 < eta - time.time() <= 30 * 60
    assert list(tmp_path.iterdir()) == []


def test_the_actor_hands_off_and_counts_bytes(
    browser: BrowserRuntime,
    queues: RedisBroker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(env="local", proxy_map={"PL": DEV_PROXY_URL})
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    client = queues.client
    key = worker.day_key(datetime.now(UTC))
    before = int(client.get(key) or 0)
    job = _job("http://mintera-login.test/", country="TR")  # no TR proxy: PL is used

    try:
        worker.browser_capture.fn(job.model_dump(mode="json"))

        [encoded] = client.hvals(f"dramatiq:{PERSIST_QUEUE}.msgs")
        message = json.loads(encoded)
        assert (message["actor_name"], message["args"]) == ("persist_capture", [str(job.job_id)])
        meta = CaptureMeta.model_validate_json(
            (tmp_path / str(job.job_id) / META_FILE).read_bytes()
        )
        assert meta.proxy_country == "PL"
        assert int(client.get(key) or 0) - before == sum(r.proxy_bytes for r in meta.profiles) > 0
    finally:
        client.decrby(key, int(client.get(key) or 0) - before)


@pytest.mark.parametrize("credentials", [None, "kalkan-user:kalkan-password"])
def test_loopback_goes_through_the_proxy(
    browser: BrowserRuntime, tmp_path: Path, credentials: str | None
) -> None:
    """Also through the credential forwarder, itself on 127.0.0.1 (K93)."""
    with Listener() as listener, LocalProxy({}, credentials=credentials) as proxy:
        loopback = f"http://127.0.0.1:{listener.port}/"
        _capture(tmp_path, loopback, proxy=proxy.url(credentials or ""), final=True)
        lines = [line for line, _ in proxy.requests]
    assert f"GET {loopback} HTTP/1.1" in lines
    assert listener.connections == 0


def test_a_context_without_its_own_proxy_reaches_nothing(browser: BrowserRuntime) -> None:
    from playwright.sync_api import Error as PlaywrightError

    with Listener() as listener:
        context = browser.browser().new_context()
        try:
            page = context.new_page()
            with pytest.raises(PlaywrightError, match="ERR_PROXY_CONNECTION_FAILED"):
                page.goto(f"http://127.0.0.1:{listener.port}/", timeout=10_000)
        finally:
            context.close()
    assert listener.connections == 0


def test_proxy_credentials_never_reach_the_evidence_or_the_logs(
    browser: BrowserRuntime,
    tmp_path: Path,
    recorded: Recorder,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    password = "Pr0xy-s3cret-Passw0rd"  # gitleaks:allow (test value)
    pages = {"mintera-login.test": html_page(CLONE_HTML)}
    with LocalProxy(pages) as proxy:
        session = new_session()
        user = f"kalkan-session-{session}"
        proxy.credentials = f"{user}:{password}"
        choice = parse_template("PL", proxy.url(f"kalkan-session-{{session}}:{password}"), session)
        job = _job("http://mintera-login.test/")
        folder, meta = run_job(job, choice, root=tmp_path, final=False, browser=browser)

    assert {result.outcome for result in meta.profiles} == {Outcome.OK}
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    # The proxy asked for credentials and got this attempt's session in the user name.
    assert ("GET http://mintera-login.test/ HTTP/1.1", f"Basic {token}") in proxy.requests
    files = b"".join(path.read_bytes() for path in folder.iterdir())
    assert ArtifactKind.HAR in meta.profiles[0].files
    logs = "\n".join(recorded.lines) + caplog.text + "".join(capsys.readouterr())
    for secret in (password, user, token):
        assert secret.encode() not in files
        assert secret not in logs


def test_a_site_asking_for_basic_auth_gets_no_proxy_credentials(
    browser: BrowserRuntime, tmp_path: Path
) -> None:
    def basic_auth(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        if not url.endswith("/admin"):  # the page embeds a resource behind Basic auth
            return 200, {"Content-Type": "text/html"}, b"<title>p</title><img src='/admin'>"
        if "authorization" in headers:
            return 200, {"Content-Type": "image/gif"}, b"GIF89a"
        return 401, {"WWW-Authenticate": 'Basic realm="admin"'}, b""

    with LocalProxy({"panel.test": basic_auth}, credentials="kalkan-user:proxy-password") as proxy:
        choice = parse_template("PL", proxy.url("kalkan-user:proxy-password"), new_session())
        run_job(
            _job("http://panel.test/", Profile.DESKTOP),
            choice,
            root=tmp_path,
            final=True,
            browser=browser,
        )
    # Given the credentials, Playwright 1.63 would answer this 401 with them; the forwarder
    # keeps them out of the browser (K93).
    assert proxy.authorizations == []
    assert any(line == "GET http://panel.test/admin HTTP/1.1" for line, _ in proxy.requests)


# --- the time budget -----------------------------------------------------------------------------

# The renderer locks up after load: evaluate() and content() have no timeout of their own.
HANG = (
    b"<!doctype html><title>Hang</title><p>Mintera Bank</p><script>addEventListener('load', () =>"
    b" setTimeout(() => { for (;;) {} }, 50));</script>"
)


def test_a_hung_page_is_killed_retried_once_and_handed_off(
    queues: RedisBroker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded: Recorder
) -> None:
    """Acceptance 6 (browser side): the attempt is killed after its budget, Dramatiq retries
    it once, and the last attempt hands off ``final_attempt`` for persist_capture."""
    own = BrowserRuntime()  # used by the worker thread, killed from here at the end
    monkeypatch.setattr(capture, "runtime", lambda: own)
    monkeypatch.setattr(capture, "JOB_BUDGET_SECONDS", 12.0)
    monkeypatch.setattr(capture, "KILL_GRACE_SECONDS", 3.0)
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    attempts: list[float] = []
    real_run = capture.run_job

    def timed(*args: Any, **kwargs: Any) -> Any:
        attempts.append(time.monotonic())
        return real_run(*args, **kwargs)

    monkeypatch.setattr(capture, "run_job", timed)
    with LocalProxy({"hang.test": html_page(HANG)}) as proxy:
        settings = make_settings(env="local", proxy_map={"PL": proxy.url()})
        monkeypatch.setattr(worker, "get_settings", lambda: settings)
        job = _job("http://hang.test/")
        worker_process = Worker(queues, queues={"capture"}, worker_threads=1)
        worker_process.start()
        try:
            worker.browser_capture.send_with_options(
                args=(job.model_dump(mode="json"),), min_backoff=100, max_backoff=200
            )
            deadline = time.monotonic() + 90
            client = queues.client
            while time.monotonic() < deadline and not client.hlen(f"dramatiq:{PERSIST_QUEUE}.msgs"):
                time.sleep(0.5)
        finally:
            worker_process.stop()
            own.kill()

    assert len(attempts) == 2, "\n".join(recorded.lines)
    meta = CaptureMeta.model_validate_json((tmp_path / str(job.job_id) / META_FILE).read_bytes())
    assert meta.final_attempt is True
    assert {result.outcome for result in meta.profiles} == {Outcome.ERROR}
    assert all(result.error for result in meta.profiles)


# --- a hostile page ------------------------------------------------------------------------------

RTC = b"""<!doctype html><title>rtc</title><script>
window.candidates = null;
(async () => {
  const found = [];
  const peer = new RTCPeerConnection({ iceServers: [] });
  peer.createDataChannel("x");
  peer.onicecandidate = (event) => {
    if (event.candidate) found.push(event.candidate.candidate); else window.candidates = found;
  };
  await peer.setLocalDescription(await peer.createOffer());
  setTimeout(() => { if (window.candidates === null) window.candidates = found; }, 3000);
})();
</script>"""


def test_webrtc_sends_nothing_around_the_proxy(browser: BrowserRuntime) -> None:
    """UDP would bypass the HTTP proxy and show the VM's address (K93)."""
    with LocalProxy({"rtc.test": html_page(RTC)}) as proxy:
        context = browser.browser().new_context(proxy={"server": proxy.url()})
        try:
            page = context.new_page()
            page.goto("http://rtc.test/", timeout=10_000)
            page.wait_for_function("window.candidates !== null", timeout=10_000)
            assert page.evaluate("window.candidates") == []
        finally:
            context.close()


POPUP_AND_DIALOG = b"""<!doctype html><title>Popup</title><p>Mintera Bank</p><script>
addEventListener("load", () => { window.open("http://popup.test/landing"); alert("hi"); });
</script>"""


def test_popups_are_closed_and_recorded_and_dialogs_dismissed(
    browser: BrowserRuntime, tmp_path: Path
) -> None:
    pages = {"opener.test": html_page(POPUP_AND_DIALOG), "popup.test": html_page(b"popup")}
    with LocalProxy(pages) as proxy:
        _, meta = _capture(tmp_path, "http://opener.test/", proxy=proxy.url())
        requested = [line for line, _ in proxy.requests]
    for result in meta.profiles:
        assert result.outcome is Outcome.OK, result.error
        assert ("navigation", "http://popup.test/landing") in {
            (link.tag, link.url) for link in result.links
        }
    assert "GET http://popup.test/landing HTTP/1.1" not in requested  # never loaded


def test_a_download_is_not_saved(browser: BrowserRuntime, tmp_path: Path) -> None:
    def apk(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        disposition = {"Content-Disposition": 'attachment; filename="mintera.apk"'}
        return 200, {"Content-Type": "application/octet-stream", **disposition}, b"PK" * 100

    with LocalProxy({"download.test": apk}) as proxy:
        folder, meta = _capture(tmp_path, "http://download.test/", proxy=proxy.url(), final=True)
    assert meta.final_attempt is True
    assert all("Download is starting" in (result.error or "") for result in meta.profiles)
    names = {path.name for path in folder.iterdir()}
    assert not any(name.endswith(".apk") for name in names)
    assert not {"screenshot-desktop.png", "html-desktop.html"} & names
