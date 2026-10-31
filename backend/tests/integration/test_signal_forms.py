"""Form signals of the sample pages in ``fixtures/forms/`` (architecture.md §12).

Each page is opened in Chromium and read by the browser stage's own inventory script
(``scanning/inventory.js`` through ``capture._inventory``), so the test covers what the browser
extracts as well as how the signals stage reads it. Pages are served by a Playwright route; no
network or proxy is used. Chromium: ``uv run playwright install chromium``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from kalkan.config import REPO_ROOT
from kalkan.scanning import capture
from kalkan.signals.forms import FormSignals, form_signals

FORMS = REPO_ROOT / "fixtures" / "forms"
PAGE_URL = "https://mintera-sample.test/"

# The signals that are true on each page; all others are false.
EXPECTED: dict[str, set[str]] = {
    "en-login": {"form_password"},
    "en-card-deposit": {"form_card", "deposit_cta", "external_form_action"},
    "en-register-phone": {"form_password", "form_phone"},
    "en-search": set(),
    "tr-login": {"form_password"},
    "tr-havale-iban": {"form_iban", "deposit_cta"},
    "tr-kart": {"form_card", "form_phone", "deposit_cta", "external_form_action"},
    "tr-deposit-cta": {"deposit_cta"},
    "pl-logowanie": {"form_password"},
    "pl-wplata-karta": {"form_card", "deposit_cta"},
    "pl-przelew": {"form_iban", "form_phone", "external_form_action"},
    "pl-blog": set(),
    "ru-vhod": {"form_password"},
    "ru-popolnit": {"form_card", "deposit_cta", "external_form_action"},
    "ru-telefon": {"form_phone", "deposit_cta"},
    "ru-novosti": set(),
}


def test_every_sample_page_has_an_expectation() -> None:
    assert {path.stem for path in FORMS.glob("*.html")} == set(EXPECTED)
    assert len(EXPECTED) >= 15
    assert {name.split("-")[0] for name in EXPECTED} == {"en", "tr", "pl", "ru"}


@pytest.fixture(scope="module")
def page() -> Iterator[Any]:
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Error as exc:
            pytest.fail(f"Chromium is missing: uv run playwright install chromium ({exc})")
        context = browser.new_context(java_script_enabled=True)
        yield context.new_page()
        browser.close()


def _signals(page: Any, name: str) -> FormSignals:
    html = (FORMS / f"{name}.html").read_text(encoding="utf-8")
    page.route(
        PAGE_URL,
        lambda route: route.fulfill(body=html, content_type="text/html; charset=utf-8"),
    )
    try:
        page.goto(PAGE_URL, wait_until="load")
        inventory = capture._inventory(page)
    finally:
        page.unroute(PAGE_URL)
    return form_signals(inventory.forms, inventory.buttons, PAGE_URL)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_form_signals_of_the_sample_pages(page: Any, name: str) -> None:
    found = _signals(page, name)
    assert {signal for signal, value in found.values().items() if value} == EXPECTED[name]


def test_details_name_what_was_found(page: Any) -> None:
    found = _signals(page, "ru-popolnit")
    assert found.deposit_texts == ["Пополнить счёт"]
    assert found.external_hosts == ["kassa-oplata.test"]
    assert found.kinds == {"amount": 1, "card_number": 1, "card_expiry": 1, "card_cvv": 1}
