"""The browser stage without a browser: inventory parsing, hand-off folder, attempt policy."""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from dramatiq.middleware import TimeLimitExceeded

from kalkan.domain.enums import ArtifactKind, Profile
from kalkan.scanning import capture
from kalkan.scanning.capture import (
    BrowserRuntime,
    BrowserUnavailableError,
    CaptureFailedError,
    Inventory,
    fit_meta,
    hand_off,
    har_bytes,
    merge_links,
    run_job,
)
from kalkan.scanning.capture_contract import (
    META_FILE,
    META_MAX_BYTES,
    PARTIAL_SUFFIX,
    CaptureJob,
    CaptureMeta,
    FormInfo,
    InputInfo,
    LinkInfo,
    Outcome,
    ProfileResult,
    Purpose,
    file_name,
)
from kalkan.scanning.proxy import parse_template

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
PROXY = parse_template("PL", "http://user-{session}:secret@proxy.example:8080", "abcd1234")


def _job(*profiles: Profile) -> CaptureJob:
    return CaptureJob(
        job_id=uuid.uuid4(),
        purpose=Purpose.CASE,
        urls=["http://mintera-login.test/"],
        country="PL",
        profiles=list(profiles or (Profile.DESKTOP, Profile.MOBILE)),
        issued_at=NOW,
    )


def _result(profile: Profile, outcome: Outcome, **values: Any) -> ProfileResult:
    return ProfileResult(
        profile=profile, outcome=outcome, started_at=NOW, finished_at=NOW, **values
    )


# --- inventory -----------------------------------------------------------------------------------


def _frame(**values: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {"url": "http://x.test/", "title": "", "text": "", "forms": []}
    frame.update({"buttons": [], "links": []})
    frame.update(values)
    return frame


def test_inventory_classifies_inputs_and_buttons() -> None:
    inventory = Inventory()
    inventory.add_frame(
        _frame(
            title="Mintera Bank | Login",
            text="Log in to Mintera Bank",
            forms=[
                {
                    "action": "http://mintera-login.test/collect",
                    "method": "post",
                    "inputs": [
                        {"type": "email", "name": "email", "autocomplete": "username"},
                        {"type": "password", "name": "password", "maxlength": -1},
                        {"type": "text", "name": "card", "autocomplete": "cc-number"},
                    ],
                    "buttons": [{"text": "Deposit and sign in", "type": "submit"}],
                }
            ],
            buttons=[{"text": "Sign in", "type": "a", "target": "http://mintera-login.test/"}],
        ),
        None,
    )
    assert inventory.title == "Mintera Bank | Login"
    [form] = inventory.forms
    assert form.action == "http://mintera-login.test/collect"
    assert [item.kind for item in form.inputs] == ["email", "password", "card_number"]
    assert form.inputs[1].maxlength is None
    assert form.buttons[0].intent == "deposit"
    assert inventory.buttons[0].intent == "login"
    assert inventory.buttons[0].target == "http://mintera-login.test/"
    assert inventory.password_fields == 1


def test_inventory_of_a_child_frame_keeps_the_main_title_and_text() -> None:
    inventory = Inventory()
    inventory.add_frame(_frame(title="Main", text="main text"), None)
    inventory.add_frame(
        _frame(title="Frame", text="frame text", forms=[{"inputs": [{"type": "password"}]}]),
        "http://pay.test/frame",
    )
    assert (inventory.title, inventory.text) == ("Main", "main text")
    assert inventory.forms[0].frame_url == "http://pay.test/frame"
    assert inventory.forms[0].method == ""


def test_inventory_skips_malformed_and_oversized_values() -> None:
    inventory = Inventory()
    inventory.add_frame("not a dict", None)
    inventory.add_frame(
        _frame(
            title=["x"],
            text=None,
            forms=["junk", {"action": "javascript:void(0)", "inputs": [7, {"type": 5}]}],
            buttons=[{"text": "x" * 5000, "target": "http://" + "a" * 3000}],
            links=[
                {"url": "mailto:a@b.c", "tag": "a"},
                {"url": "http://ok.test/", "tag": ""},
                {"url": "http://ok.test/", "tag": "a", "text": "  many   spaces "},
            ],
        ),
        None,
    )
    assert inventory.title is None
    assert inventory.text == ""
    [form] = inventory.forms
    assert form.action is None
    assert [item.type for item in form.inputs] == ["text"]
    assert len(inventory.buttons[0].text) == 200
    assert inventory.buttons[0].target is None
    assert [(link.url, link.text) for link in inventory.links] == [
        ("http://ok.test/", "many spaces")
    ]


def test_merge_links_orders_by_kind_dedupes_and_caps() -> None:
    links = [LinkInfo(url=f"http://a.test/{n}", tag="a") for n in range(250)]
    links += [
        LinkInfo(url="http://a.test/0", tag="a"),
        LinkInfo(url="http://login.test/", tag="button", text="Doğrula"),
        LinkInfo(url="http://next.test/", tag="redirect"),
    ]
    merged = merge_links(links)
    assert len(merged) == 200
    assert [link.tag for link in merged[:2]] == ["redirect", "button"]
    assert len({(link.url, link.tag) for link in merged}) == 200


# --- files ---------------------------------------------------------------------------------------


def test_har_bytes_sums_transfer_sizes(tmp_path: Path) -> None:
    har = tmp_path / "x.har"
    entries = [
        {
            "request": {"headersSize": 100, "bodySize": 0},
            "response": {"headersSize": 50, "bodySize": 900, "_transferSize": 1000},
        },
        {
            "request": {"headersSize": 80, "bodySize": 20},
            "response": {"headersSize": 40, "bodySize": 60, "_transferSize": -1},
        },
        {"request": {"headersSize": -1, "bodySize": -1}, "response": {"_failureText": "x"}},
    ]
    har.write_text(json.dumps({"log": {"entries": entries}}))
    assert har_bytes(har) == 100 + 1000 + 80 + 20 + 40 + 60
    har.write_text("{not json")
    assert har_bytes(har) == 0
    assert har_bytes(tmp_path / "missing.har") == 0


def test_fit_meta_trims_until_it_fits() -> None:
    links = [LinkInfo(url="http://a.test/" + "x" * 2000, tag="a", text="y" * 200)] * 200
    field = InputInfo(type="text", name="n" * 200, label="l" * 200, placeholder="p" * 200)
    forms = [FormInfo(inputs=[field] * 200)] * 5
    results = [
        _result(profile, Outcome.OK, links=links, forms=forms, text_excerpt="t" * 20_000)
        for profile in (Profile.DESKTOP, Profile.MOBILE)
    ]
    meta = CaptureMeta(job_id=uuid.uuid4(), profiles=results)
    small = CaptureMeta(job_id=uuid.uuid4(), profiles=[_result(Profile.DESKTOP, Outcome.OK)])
    assert len(meta.model_dump_json()) > META_MAX_BYTES
    data = fit_meta(meta)
    assert len(data) <= META_MAX_BYTES
    trimmed = CaptureMeta.model_validate_json(data)
    assert trimmed.profiles[0].links == []
    assert trimmed.profiles[0].forms == forms
    assert trimmed.profiles[0].text_excerpt == "t" * 20_000
    assert fit_meta(small) == small.model_dump_json().encode()


def test_hand_off_writes_meta_last_and_removes_unlisted_files(tmp_path: Path) -> None:
    job = _job(Profile.DESKTOP)
    partial = tmp_path / f"{job.job_id}{PARTIAL_SUFFIX}"
    partial.mkdir()
    listed = file_name(ArtifactKind.SCREENSHOT, Profile.DESKTOP)
    (partial / listed).write_bytes(b"\x89PNG")
    (partial / "har-desktop.har").write_text("{}")  # not in the result's files
    (partial / "stray").mkdir()
    result = _result(Profile.DESKTOP, Outcome.OK, files=[ArtifactKind.SCREENSHOT])
    meta = CaptureMeta(job_id=job.job_id, profiles=[result])

    folder = hand_off(partial, meta, [result])

    assert folder == tmp_path / str(job.job_id)
    assert not partial.exists()
    assert sorted(path.name for path in folder.iterdir()) == [META_FILE, listed]
    assert CaptureMeta.model_validate_json((folder / META_FILE).read_bytes()) == meta


# --- one attempt ---------------------------------------------------------------------------------


class FakeChromium:
    version = "153.0.8010.12"


class FakeRuntime(BrowserRuntime):
    def __init__(self, *, fail: BaseException | None = None) -> None:
        super().__init__()
        self.fail = fail
        self.closed = 0
        self.launches = 0

    def browser(self) -> Any:
        self.launches += 1
        if self.fail is not None:
            raise self.fail
        return FakeChromium()

    def usable(self) -> bool:
        return not self.dirty

    def close(self) -> None:
        self.closed += 1
        self.dirty = False

    def kill(self, *, driver: bool = False) -> None:
        self.dirty = True


def _profiles(outcomes: dict[Profile, Outcome], calls: list[Any]) -> Any:
    def fake(
        browser: Any,
        job: CaptureJob,
        profile: Profile,
        proxy: Any,
        out: Path,
        *,
        route: dict[str, str],
        deadline: float,
        usable: Any,
    ) -> ProfileResult:
        calls.append((profile, proxy, route))
        files = []
        if outcomes[profile] is not Outcome.ERROR:
            (out / file_name(ArtifactKind.SCREENSHOT, profile)).write_bytes(b"\x89PNG")
            files = [ArtifactKind.SCREENSHOT]
        error = "net::ERR_NAME_NOT_RESOLVED" if outcomes[profile] is Outcome.ERROR else None
        return _result(profile, outcomes[profile], files=files, error=error, proxy_bytes=10)

    return fake


def test_a_successful_attempt_is_handed_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    outcomes = {Profile.DESKTOP: Outcome.OK, Profile.MOBILE: Outcome.ERROR}
    monkeypatch.setattr(capture, "capture_profile", _profiles(outcomes, calls))
    job = _job()

    folder, meta = run_job(job, PROXY, root=tmp_path, final=False, browser=FakeRuntime())

    assert [call[0] for call in calls] == [Profile.DESKTOP, Profile.MOBILE]
    assert {call[1] for call in calls} == {PROXY}  # one proxy session for both profiles
    # The context goes through the local forwarder, never with the credentials.
    [route] = {call[2]["server"] for call in calls}
    assert route.startswith("http://127.0.0.1:")
    assert all(set(call[2]) == {"server"} for call in calls)
    assert folder == tmp_path / str(job.job_id)
    assert meta.final_attempt is False
    assert meta.proxy_country == "PL"
    assert meta.browser_version == "153.0.8010.12"
    assert [result.outcome for result in meta.profiles] == [Outcome.OK, Outcome.ERROR]
    assert sorted(path.name for path in folder.iterdir()) == [META_FILE, "screenshot-desktop.png"]
    assert "secret" not in (folder / META_FILE).read_text()


def test_every_profile_failing_raises_for_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = {Profile.DESKTOP: Outcome.ERROR, Profile.MOBILE: Outcome.ERROR}
    monkeypatch.setattr(capture, "capture_profile", _profiles(outcomes, []))
    job = _job()

    with pytest.raises(CaptureFailedError) as raised:
        run_job(job, PROXY, root=tmp_path, final=False, browser=FakeRuntime())

    assert [result.outcome for result in raised.value.results] == [Outcome.ERROR] * 2
    assert list(tmp_path.iterdir()) == []  # the partial folder is gone


def test_the_final_failed_attempt_is_handed_off_for_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = {Profile.DESKTOP: Outcome.ERROR, Profile.MOBILE: Outcome.ERROR}
    monkeypatch.setattr(capture, "capture_profile", _profiles(outcomes, []))

    folder, meta = run_job(_job(), PROXY, root=tmp_path, final=True, browser=FakeRuntime())

    assert meta.final_attempt is True
    assert meta.error == "net::ERR_NAME_NOT_RESOLVED"
    assert sorted(path.name for path in folder.iterdir()) == [META_FILE]


def test_a_challenge_is_not_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = {Profile.DESKTOP: Outcome.CHALLENGE, Profile.MOBILE: Outcome.ERROR}
    monkeypatch.setattr(capture, "capture_profile", _profiles(outcomes, []))
    _, meta = run_job(_job(), PROXY, root=tmp_path, final=False, browser=FakeRuntime())
    assert meta.final_attempt is False


def test_a_browser_that_cannot_start_fails_no_case(tmp_path: Path) -> None:
    # Not the site's fault: the worker defers the job (browser_unavailable), even on the last
    # attempt, and nothing is handed off.
    browser = FakeRuntime(fail=RuntimeError("no usable sandbox"))
    for final in (False, True):
        with pytest.raises(BrowserUnavailableError, match="no usable sandbox"):
            run_job(_job(), PROXY, root=tmp_path, final=final, browser=browser)
        assert list(tmp_path.iterdir()) == []
    assert browser.closed == 2


@pytest.mark.parametrize("failure", [TimeLimitExceeded()])
def test_an_unexpected_failure_replaces_the_browser(tmp_path: Path, failure: BaseException) -> None:
    browser = FakeRuntime(fail=failure)
    with pytest.raises(CaptureFailedError):
        run_job(_job(), PROXY, root=tmp_path, final=False, browser=browser)
    assert browser.closed == 1
    assert list(tmp_path.iterdir()) == []

    folder, meta = run_job(_job(), PROXY, root=tmp_path, final=True, browser=browser)
    assert meta.final_attempt is True
    assert {result.outcome for result in meta.profiles} == {Outcome.ERROR}
    assert all(result.error for result in meta.profiles)
    assert meta.error is not None
    assert folder.is_dir()


def test_a_stale_partial_folder_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _job(Profile.DESKTOP)
    stale = tmp_path / f"{job.job_id}{PARTIAL_SUFFIX}"
    stale.mkdir()
    (stale / "html-desktop.html").write_text("old")
    monkeypatch.setattr(capture, "capture_profile", _profiles({Profile.DESKTOP: Outcome.OK}, []))
    folder, _ = run_job(job, PROXY, root=tmp_path, final=False, browser=FakeRuntime())
    assert not (folder / "html-desktop.html").exists()


def test_the_watchdog_kills_a_hung_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    browser = FakeRuntime()
    monkeypatch.setattr(capture, "KILL_GRACE_SECONDS", 0.2)

    def hang(*args: Any, usable: Any, **kwargs: Any) -> ProfileResult:
        deadline = time.monotonic() + 5
        while usable() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not usable(), "the watchdog did not fire"
        raise RuntimeError("Target page, context or browser has been closed")

    monkeypatch.setattr(capture, "capture_profile", hang)
    with pytest.raises(CaptureFailedError):
        run_job(_job(), PROXY, root=tmp_path, final=False, browser=browser, budget=0.1)
    assert browser.closed == 1


def test_the_browser_restarts_after_fifty_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = BrowserRuntime()
    closed: list[int] = []
    monkeypatch.setattr(browser, "close", lambda: closed.append(1))
    for _ in range(capture.RESTART_EVERY - 1):
        browser.job_done()
    assert closed == []
    browser.job_done()
    assert closed == [1]


def test_descendants_walks_the_process_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    table = [(10, 1), (11, 10), (12, 11), (13, 11), (20, 1), (14, 12)]
    monkeypatch.setattr(capture, "_process_table", lambda: table)
    assert sorted(capture.descendants(10)) == [11, 12, 13, 14]
    assert capture.descendants(20) == []


def test_challenge_signature_follows_the_k74_rules() -> None:
    html = "<html><title>Just a moment...</title><script src='/orchestrate/chl_page/'></script>"
    assert capture._challenge(html, {"cf-mitigated": "challenge"}, "", 0) == (
        "cf-mitigated: challenge"
    )
    assert capture._challenge(html, {}, "", 0) == "<title>just a moment...</title>"
    login = '<form><div class="cf-turnstile"></div><input type="password"></form>'
    assert capture._challenge(login, {}, "Log in", 1) is None
    wall = '<div class="g-recaptcha"></div>'
    assert capture._challenge(wall, {}, "Verify you are human", 0) == "g-recaptcha"
    assert capture._challenge("<p>hello</p>", {}, "hello", 0) is None


def test_scrub_har_removes_proxy_credentials(tmp_path: Path) -> None:
    proxy = parse_template(
        "PL", "http://user-{session}:long-secret-1@proxy.example:8080", "ab12cd34"
    )
    token = base64.b64encode(b"user-ab12cd34:long-secret-1").decode()
    har = tmp_path / "x.har"
    headers = [
        {"name": "Host", "value": "site.test"},
        {"name": "Proxy-Authorization", "value": f"Basic {token}"},
        {"name": "Authorization", "value": f"Basic {token}"},  # a site's 401 answered with them
    ]
    entry = {"request": {"url": "http://site.test/", "headers": headers}, "response": {}}
    har.write_text(json.dumps({"log": {"entries": [entry]}}))

    capture.scrub_har(har, proxy)

    kept = json.loads(har.read_text())["log"]["entries"][0]["request"]["headers"]
    assert kept == [{"name": "Host", "value": "site.test"}]


def test_scrub_har_drops_a_har_that_still_holds_them(tmp_path: Path) -> None:
    proxy = parse_template(
        "PL", "http://user-{session}:long-secret-1@proxy.example:8080", "ab12cd34"
    )
    har = tmp_path / "x.har"
    entry = {"request": {"url": "http://site.test/?p=long-secret-1", "headers": []}}
    har.write_text(json.dumps({"log": {"entries": [entry]}}))
    capture.scrub_har(har, proxy)
    assert not har.exists()


def test_scrub_har_leaves_a_har_alone_without_credentials(tmp_path: Path) -> None:
    har = tmp_path / "x.har"
    har.write_text('{"log": {"entries": []}}')
    capture.scrub_har(har, parse_template("PL", "http://dev-proxy:8888", "x"))
    assert har.read_text() == '{"log": {"entries": []}}'


def test_a_watchdog_that_fired_during_the_launch_stops_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    outcomes = {Profile.DESKTOP: Outcome.OK, Profile.MOBILE: Outcome.OK}
    monkeypatch.setattr(capture, "capture_profile", _profiles(outcomes, calls))
    monkeypatch.setattr(capture, "KILL_GRACE_SECONDS", 0.05)

    class SlowLaunch(FakeRuntime):
        def browser(self) -> Any:
            time.sleep(0.3)
            return FakeChromium()

    with pytest.raises(CaptureFailedError, match="stopped after the time budget"):
        run_job(_job(), PROXY, root=tmp_path, final=False, browser=SlowLaunch(), budget=0.05)
    assert calls == []


def test_chromium_gets_no_secrets_from_the_environment() -> None:
    from kalkan.scanning.capture import browser_env

    env = browser_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/pwuser",
            "PROXY_MAP": '{"PL": "http://u:secret@h:1"}',
            "REDIS_URL": "redis://browser:secret@redis:6379/0",
            "SENTRY_DSN": "https://key@sentry.example/1",
        }
    )
    assert env == {"PATH": "/usr/bin", "HOME": "/home/pwuser"}
