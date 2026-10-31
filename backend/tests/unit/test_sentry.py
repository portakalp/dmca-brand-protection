from __future__ import annotations

from typing import Any

import pytest
import sentry_sdk
from sentry_sdk.types import Event

from kalkan import sentry
from kalkan.sentry import init_sentry
from tests.support import make_settings


def test_sentry_is_not_initialised_without_dsn() -> None:
    assert init_sentry(make_settings(env="local"), "api") is False
    assert sentry_sdk.get_client().is_active() is False


def test_sentry_options_keep_secrets_out(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda *_: None)

    settings = make_settings(
        env="prod", sentry_dsn="https://key@o0.ingest.sentry.io/1", git_sha="abc"
    )
    assert init_sentry(settings, "api") is True

    assert captured["send_default_pii"] is False
    assert captured["include_local_variables"] is False
    assert captured["max_request_body_size"] == "never"
    assert captured["release"] == "abc"
    assert "x-edge-auth" in captured["event_scrubber"].denylist


def test_scrubber_removes_the_edge_header() -> None:
    scrubber = sentry_sdk.scrubber.EventScrubber(
        denylist=sentry_sdk.scrubber.DEFAULT_DENYLIST + sentry.SCRUBBED_KEYS, recursive=True
    )
    event: Event = {"request": {"headers": {"x-edge-auth": "EDGE-SECRET"}}}
    scrubber.scrub_event(event)
    assert "EDGE-SECRET" not in str(event)


def test_evidence_tokens_are_removed_from_request_urls() -> None:
    event: Event = {"request": {"url": "https://evidence.example.com/e/SECRET-TOKEN/files/7?x=1"}}
    result = sentry._redact_request_url(event, {})
    assert result["request"]["url"] == "https://evidence.example.com/e/[REDACTED]/files/7?x=1"
