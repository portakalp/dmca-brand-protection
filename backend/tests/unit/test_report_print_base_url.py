"""Where worker-browser opens the report print page (REPORT_PRINT_BASE_URL, self-hosting)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kalkan.reports.printing import AppOrigin
from kalkan.workers import report_print
from tests.support import make_settings


def test_the_print_base_url_falls_back_to_the_app_address() -> None:
    settings = make_settings(env="local", app_base_url="https://app.example.com")
    assert report_print.print_base_url(settings) == "https://app.example.com"


def test_an_internal_print_address_wins_over_the_public_one() -> None:
    settings = make_settings(
        env="prod",
        app_base_url="https://app.example.com",
        report_print_base_url="http://web:3000",
    )
    assert report_print.print_base_url(settings) == "http://web:3000"


def test_without_either_address_there_is_none() -> None:
    assert report_print.print_base_url(make_settings(env="local")) is None


def test_the_actor_prints_from_the_print_address(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(
        env="local", app_base_url="http://localhost:3000", report_print_base_url="http://web:3000"
    )
    calls: list[dict[str, Any]] = []

    def handle(report_id: str, attempt: str, *, base_url: str | None, root: Path) -> None:
        calls.append({"report_id": report_id, "attempt": attempt, "base_url": base_url})

    monkeypatch.setattr(report_print, "get_settings", lambda: settings)
    monkeypatch.setattr(report_print, "handle", handle)
    report_print.print_report.fn("4b0f4a52-0000-4000-8000-000000000001", "2")
    assert calls == [
        {
            "report_id": "4b0f4a52-0000-4000-8000-000000000001",
            "attempt": "2",
            "base_url": "http://web:3000",
        }
    ]


def test_the_printer_is_confined_to_an_internal_service_name() -> None:
    origin = AppOrigin.parse("http://web:3000")
    assert origin.url("/print/report/1") == "http://web:3000/print/report/1"
    assert origin.allows("http://web:3000/_next/static/app.js")
    assert not origin.allows("http://api:8000/api/v1/me")
    assert not origin.allows("https://app.example.com/print/report/1")
    assert "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE web" in origin.launch_args()
