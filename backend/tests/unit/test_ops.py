"""The ops panel without services: settings validation, re-queue rules, its process
and its ports."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from kalkan.db.settings_store import SPECS, SettingError, parse_value
from kalkan.domain.enums import CaseState
from kalkan.ops import auth
from kalkan.ops.requeue import why_not
from kalkan.scheduler.outbox import ACTORS
from kalkan.workers.specs import WORKERS

COMPOSE_DIR = Path(__file__).resolve().parents[3] / "infra" / "compose"


def _service(name: str, file: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((COMPOSE_DIR / file).read_text())
    service: dict[str, Any] = data["services"][name]
    return service


@pytest.mark.parametrize(
    ("key", "raw", "value"),
    [
        ("sending_enabled", "true", True),
        ("sending_enabled", "off", False),
        ("visual_similarity_min", "0,8", 0.8),
        ("triage_min_confidence", 1, 1.0),
        ("channel_daily_limits", {"hosting": "7", "registrar": 0}, {"hosting": 7, "registrar": 0}),
    ],
)
def test_setting_values_are_parsed(key: str, raw: Any, value: Any) -> None:
    assert parse_value(SPECS[key], raw) == value


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("sending_enabled", "maybe"),
        ("visual_similarity_min", "1.01"),
        ("visual_similarity_min", "high"),
        ("channel_daily_limits", {"hosting": "-1"}),
        ("channel_daily_limits", {"hosting": "1.5"}),
        ("channel_daily_limits", "50"),
    ],
)
def test_invalid_setting_values_are_refused(key: str, raw: Any) -> None:
    with pytest.raises(SettingError):
        parse_value(SPECS[key], raw)


def test_only_the_send_switch_lifts_a_gate() -> None:
    assert [key for key, spec in SPECS.items() if spec.lifts_block] == ["sending_enabled"]


@pytest.mark.parametrize(
    ("job", "state", "allowed"),
    [
        ("http_precheck", CaseState.DETECTED, True),
        ("compute_signals", CaseState.DETECTED, True),
        ("compute_signals", CaseState.DISMISSED, False),
        ("enrich_domain", CaseState.WATCHING, True),
        ("browser_capture", CaseState.DETECTED, True),
        ("persist_capture", CaseState.IN_REVIEW, False),
        ("ai_triage", CaseState.DETECTED, False),
    ],
)
def test_which_failed_jobs_can_be_requeued(job: str, state: CaseState, allowed: bool) -> None:
    assert (why_not(job, state) is None) is allowed


def test_requeued_jobs_reach_their_actors() -> None:
    declared = set(WORKERS["default"].queues)
    assert {"http_precheck", "compute_signals", "hash_brand_assets", "clear_blocked_cases"} <= set(
        ACTORS.values()
    )
    assert "notify" in declared
    assert "kalkan.workers.notify" in WORKERS["default"].actor_modules


def test_csrf_tokens_belong_to_one_session() -> None:
    assert auth.csrf_for("a") != auth.csrf_for("b")
    assert auth.same(auth.csrf_for("a"), auth.csrf_for("a"))
    assert not auth.same(auth.csrf_for("a"), None)
    assert not auth.same(None, None)


def test_ops_is_its_own_process_bound_to_localhost() -> None:
    # Locally the host port can move (OPS_PORT); the binding stays on localhost.
    ports = {
        "docker-compose.yml": "127.0.0.1:${OPS_PORT:-8081}:8081",
        "docker-compose.vm.yml": "127.0.0.1:8081:8081",
    }
    for file, port in ports.items():
        service = _service("ops", file)
        assert service["command"][:2] == ["uvicorn", "kalkan.ops.main:app"]
        assert service["ports"] == [port], file
        assert "expose" not in service
    assert _service("ops", "docker-compose.vm.yml")["profiles"] == ["ops"]
    # The customer API does not serve the panel.
    from kalkan.api.app import create_app
    from tests.support import make_settings

    api = create_app(make_settings(env="local"))
    paths = {getattr(route, "path", "") for route in api.routes}
    assert not {"/login", "/cases", "/labels", "/settings"} & paths


def test_no_local_service_publishes_a_port_beyond_localhost() -> None:
    """Every host port of the local stack is a variable with the old port as its default."""
    data: dict[str, Any] = yaml.safe_load((COMPOSE_DIR / "docker-compose.yml").read_text())
    published = re.compile(r"^127\.0\.0\.1:\$\{[A-Z_]+_PORT:-\d+\}:\d+$")
    for name, service in data["services"].items():
        for port in service.get("ports", []):
            assert published.match(str(port)), (name, port)


def test_no_vm_service_publishes_a_port_beyond_localhost() -> None:
    data: dict[str, Any] = yaml.safe_load((COMPOSE_DIR / "docker-compose.vm.yml").read_text())
    for name, service in data["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)
