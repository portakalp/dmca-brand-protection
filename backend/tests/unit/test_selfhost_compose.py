"""The self-hosted stack (deploy/selfhost) keeps the cloud reference's isolation rules.

Reads docker-compose.yml and the Caddyfile as text; no Docker needed. `make infra-check` runs
`docker compose config` and `caddy validate` on them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

SELFHOST = Path(__file__).resolve().parents[3] / "deploy" / "selfhost"
BACKEND_SERVICES = ("api", "worker-default", "scheduler", "ops", "worker-ai", "ct-consumer")
STORAGE_SERVICES = ("api", "worker-default", "ops", "worker-ai")
FORBIDDEN_ENV = re.compile(r"^(DATABASE_URL.*|GCS_.*|STORAGE_.*|TOTP_.*|ANTHROPIC_.*|SMTP_.*)$")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((SELFHOST / "docker-compose.yml").read_text())
    return data


def _members(compose: dict[str, Any], network: str) -> set[str]:
    return {
        name
        for name, service in compose["services"].items()
        if network in (service.get("networks") or [])
    }


def test_backend_services_run_in_prod_mode_without_test_settings(compose: dict[str, Any]) -> None:
    for name in (*BACKEND_SERVICES, "migrate", "egress-proxy", "worker-browser"):
        env = compose["services"][name]["environment"]
        assert env["ENV"] == "prod", name
        assert "STORAGE_EMULATOR_HOST" not in env, name
        assert "SSRF_ALLOW_CIDRS" not in env, name


def test_objects_are_files_in_one_shared_volume(compose: dict[str, Any]) -> None:
    for name in STORAGE_SERVICES:
        service = compose["services"][name]
        assert service["environment"]["STORAGE_BACKEND"] == "filesystem", name
        assert service["environment"]["STORAGE_ROOT"] == "/data/objects", name
        assert "objects:/data/objects" in service["volumes"], name


def test_only_caddy_is_public(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        for port in service.get("ports") or []:
            if name == "caddy":
                assert not str(port).startswith("127.0.0.1:")
            else:
                assert str(port).startswith("127.0.0.1:"), (name, port)
    assert compose["services"]["ops"]["ports"] == ["127.0.0.1:${OPS_PORT:-8081}:8081"]


def test_worker_browser_reaches_only_its_proxy_redis_and_the_print_page(
    compose: dict[str, Any],
) -> None:
    service = compose["services"]["worker-browser"]
    assert set(service["networks"]) == {"browser", "print", "browser-metrics"}
    for network in ("browser", "print", "browser-metrics"):
        assert compose["networks"][network]["internal"] is True, network
    assert _members(compose, "browser") == {"worker-browser", "redis", "egress-proxy"}
    assert _members(compose, "print") == {"worker-browser", "web"}
    assert _members(compose, "browser-metrics") == {"worker-browser", "prometheus"}
    # The proxy alone has a way out, on a network nothing else joins.
    assert _members(compose, "egress") == {"egress-proxy"}
    assert "internal" not in (compose["networks"]["egress"] or {})
    assert "ports" not in compose["services"]["egress-proxy"]


def test_worker_browser_has_no_secrets_but_its_redis_user(compose: dict[str, Any]) -> None:
    service = compose["services"]["worker-browser"]
    env = service["environment"]
    assert not [name for name in env if FORBIDDEN_ENV.match(name)]
    assert env["REDIS_URL"].startswith("redis://browser:")
    assert env["PROXY_MAP"] == '{"XX":"http://egress-proxy:8888"}'
    assert env["PROXY_DEFAULT_COUNTRY"] == "XX"
    assert env["REPORT_PRINT_BASE_URL"] == "http://web:3000"
    assert service["volumes"] == ["artifacts-tmp:/artifacts-tmp"]
    assert service["cap_drop"] == ["ALL"]
    assert "seccomp=../../infra/compose/vm/seccomp-chromium.json" in service["security_opt"]
    assert not any("--no-sandbox" in str(part) for part in service.get("command", []))


def test_egress_proxy_is_the_kalkan_module(compose: dict[str, Any]) -> None:
    service = compose["services"]["egress-proxy"]
    assert service["command"][:3] == ["python", "-m", "kalkan.net.egress_proxy"]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]


def test_application_services_wait_for_the_migration(compose: dict[str, Any]) -> None:
    for name in BACKEND_SERVICES:
        depends = compose["services"][name]["depends_on"]
        assert depends["migrate"] == {"condition": "service_completed_successfully"}, name


def test_caddy_sends_the_edge_secret_with_every_forwarded_request() -> None:
    caddyfile = (SELFHOST / "Caddyfile").read_text()
    lines = [line.strip() for line in caddyfile.splitlines() if not line.strip().startswith("#")]
    proxies = [line for line in lines if line.startswith("reverse_proxy")]
    assert proxies
    assert all(line.endswith("{") for line in proxies)
    assert lines.count("import edge") == len(proxies)
    assert "header_up X-Edge-Auth {$EDGE_SHARED_SECRET}" in lines
