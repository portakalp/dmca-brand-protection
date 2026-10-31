"""Labelling with the keyboard only, in Chromium.

The ops app is served by uvicorn on a free loopback port; the browser gets the session cookie
of a sign-in made through the test client. Chromium: ``uv run playwright install chromium``.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import select

from kalkan.db.models import AuditLog, Label
from kalkan.domain.enums import Category
from kalkan.ops import auth
from kalkan.ops.app import create_app
from kalkan.ops.web import Services
from tests.integration.ops_support import captured_case, sign_in


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture
def server(ops_services: Services) -> Iterator[str]:
    port = _free_port()
    # log_config=None: uvicorn would otherwise reconfigure the process's loggers.
    config = uvicorn.Config(
        create_app(ops_services.settings, ops_services),
        host="127.0.0.1",
        port=port,
        log_config=None,
    )
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not instance.started:
        if time.monotonic() > deadline:
            pytest.fail("the ops app did not start")
        time.sleep(0.05)
    yield f"http://localhost:{port}"
    instance.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def ops_page(server: str, ops_client: TestClient, totp_secret: str) -> Iterator[Any]:
    from playwright.sync_api import Error, sync_playwright

    sign_in(ops_client, totp_secret)
    token = ops_client.cookies[auth.SESSION_COOKIE]
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Error as exc:
            pytest.fail(f"Chromium is missing: uv run playwright install chromium ({exc})")
        context = browser.new_context()
        # Secure cookies are kept for http://localhost (a potentially trustworthy origin).
        context.add_cookies(
            [
                {
                    "name": auth.SESSION_COOKIE,
                    "value": token,
                    "url": server,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Strict",
                }
            ]
        )
        yield context.new_page()
        browser.close()


def test_labelling_works_with_the_keyboard_alone(
    ops_page: Any, server: str, ops_services: Services
) -> None:
    page = ops_page
    cases = sorted(
        (
            captured_case(ops_services.database, ops_services.store, ops_services.settings)
            for _ in range(3)
        ),
        key=lambda case: case.number,
    )
    page.goto(f"{server}/labels")
    assert cases[0].number in page.inner_text("#label-case")

    page.keyboard.press("n")  # skip the first case
    page.wait_for_url(f"**/labels?after={cases[0].number}")
    assert cases[1].number in page.inner_text("#label-case")

    page.keyboard.press("/")  # into the note
    page.keyboard.type("login form, 1 digit")  # digits typed in the note do not label
    page.keyboard.press("Escape")
    with page.expect_navigation():
        page.keyboard.press("1")
    assert cases[2].number in page.inner_text("#label-case")

    with page.expect_navigation():
        page.keyboard.press("4")
    # The pass ended; the skipped case is back.
    assert cases[0].number in page.inner_text("#label-case")
    with page.expect_navigation():
        page.keyboard.press("6")
    assert "The queue is empty" in page.inner_text("main")

    with ops_services.database.worker_session() as session:
        labels = {row.case_id: (row.category, row.note) for row in session.scalars(select(Label))}
        audited = session.scalars(select(AuditLog.target).where(AuditLog.action == "label.set"))
        targets = list(audited)
    assert labels == {
        cases[1].id: (Category.PHISHING_CLONE, "login form, 1 digit"),
        cases[2].id: (Category.PARKED_OR_EMPTY, None),
        cases[0].id: (Category.UNCLEAR, None),
    }
    assert targets == [cases[1].number, cases[2].number, cases[0].number]
