"""worker-browser gets no database, no cloud credentials and no way around its network (K6, K82).

Reads the compose files, vm/secrets.map, vm/bin/firewall.sh --print and the seccomp profile as
text; no Docker needed.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE_DIR = Path(__file__).resolve().parents[3] / "infra" / "compose"
VM = COMPOSE_DIR / "vm"
SERVICE = "worker-browser"
FORBIDDEN_ENV = re.compile(
    r"^(DATABASE_URL.*|GOOGLE_.*|CLOUDSDK_.*|GCS_.*|STORAGE_EMULATOR_HOST|GCP_.*|CLOUDSQL_.*)$"
)
BROWSER_SECRETS = {"REDIS_URL", "PROXY_MAP", "SENTRY_DSN"}
SECCOMP = "seccomp=./vm/seccomp-chromium.json"


def _compose(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((COMPOSE_DIR / name).read_text())
    return data


@pytest.fixture(params=["docker-compose.yml", "docker-compose.vm.yml"])
def compose(request: pytest.FixtureRequest) -> dict[str, Any]:
    return _compose(request.param)


def _environment(service: dict[str, Any]) -> dict[str, Any]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) if "=" in item else (item, None) for item in env)
    return dict(env)


def _env_files(service: dict[str, Any]) -> list[str]:
    files = service.get("env_file") or []
    files = [files] if isinstance(files, str | dict) else files
    return [entry if isinstance(entry, str) else entry["path"] for entry in files]


def _firewall_rules() -> list[str]:
    bash = shutil.which("bash")
    assert bash is not None
    printed = subprocess.run(  # noqa: S603 - our own script, fixed arguments
        [bash, str(VM / "bin" / "firewall.sh"), "--print"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return printed.splitlines()


def test_worker_browser_has_no_database_or_cloud_settings(compose: dict[str, Any]) -> None:
    service = compose["services"][SERVICE]
    assert [key for key in _environment(service) if FORBIDDEN_ENV.match(key)] == []
    # Only its own secrets file on the VM (never app.env, which carries the deploy settings).
    assert all(path == "/run/kalkan/worker-browser.env" for path in _env_files(service))


def test_worker_browser_mounts_only_the_hand_off_volume(compose: dict[str, Any]) -> None:
    service = compose["services"][SERVICE]
    assert service["volumes"] == ["artifacts-tmp:/artifacts-tmp"]
    assert "ports" not in service
    assert "expose" not in service
    for key in ("privileged", "cap_add", "devices", "pid", "ipc", "network_mode", "userns_mode"):
        assert key not in service, key


def test_worker_browser_keeps_chromium_sandboxed(compose: dict[str, Any]) -> None:
    service = compose["services"][SERVICE]
    assert service["cap_drop"] == ["ALL"]
    assert set(service["security_opt"]) == {"no-new-privileges:true", SECCOMP}
    assert service["command"] == ["python", "-m", "kalkan.workers", "browser"]
    assert "--no-sandbox" not in json.dumps(service)


def test_local_worker_browser_sits_on_an_internal_network() -> None:
    compose = _compose("docker-compose.yml")
    service = compose["services"][SERVICE]
    assert service["networks"] == ["browser", "print"]
    assert compose["networks"]["browser"]["internal"] is True
    # Only Redis and the stand-in proxy share it.
    neighbours = {
        name
        for name, other in compose["services"].items()
        if name != SERVICE and "browser" in (other.get("networks") or [])
    }
    assert neighbours == {"redis", "dev-proxy"}
    # The report print (no proxy) reaches the panel's server alone, on a network with no route out.
    assert compose["networks"]["print"]["internal"] is True
    printing = {
        name
        for name, other in compose["services"].items()
        if name != SERVICE and "print" in (other.get("networks") or [])
    }
    assert printing == {"web"}


def test_vm_worker_browser_is_alone_on_the_browser_network_with_redis_and_prometheus() -> None:
    compose = _compose("docker-compose.vm.yml")
    service = compose["services"][SERVICE]
    assert set(service["networks"]) == {"kalkan-browser"}
    assert service["networks"]["kalkan-browser"]["ipv4_address"] == "172.31.250.11"
    assert "env_file" in service
    neighbours = {
        name: other["networks"]["kalkan-browser"]["ipv4_address"]
        for name, other in compose["services"].items()
        if name != SERVICE and "kalkan-browser" in (other.get("networks") or [])
    }
    assert neighbours == {"redis": "172.31.250.10", "prometheus": "172.31.250.12"}
    network = compose["networks"]["kalkan-browser"]
    assert network["driver_opts"]["com.docker.network.bridge.name"] == "br-kalkan-brw"


def test_vm_worker_browser_receives_only_its_own_secrets() -> None:
    pairs = [
        line.split()[1].split("=", 1)
        for line in (VM / "secrets.map").read_text().splitlines()
        if line.strip() and not line.startswith("#") and line.split()[0] == SERVICE
    ]
    assert {var for var, _ in pairs} <= BROWSER_SECRETS
    # The browser's Redis user, not the application's.
    assert dict(pairs)["REDIS_URL"] == "redis-browser-url"


def test_firewall_keeps_worker_browser_from_the_metadata_server_and_private_networks() -> None:
    rules = _firewall_rules()
    browser = "172.31.250.11/32"
    metadata_clients = [
        rule.split()[3] for rule in rules if rule.startswith("-A KALKAN-METADATA -s")
    ]
    assert metadata_clients
    assert browser not in metadata_clients
    app_network = ipaddress.IPv4Network("172.31.251.0/24")
    assert all(ipaddress.IPv4Network(client).subnet_of(app_network) for client in metadata_clients)
    forward = [rule for rule in rules if rule.startswith("-A KALKAN-BROWSER ")]
    passes = [rule for rule in forward if rule.endswith("-j RETURN")]
    assert passes == [
        "-A KALKAN-BROWSER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN",
        "-A KALKAN-BROWSER -d 172.31.250.10/32 -p tcp -m tcp --dport 6379 -j RETURN",
        # Prometheus's scrape: into worker-browser, never out of it.
        "-A KALKAN-BROWSER -s 172.31.250.12/32 -d 172.31.250.11/32 -p tcp -m tcp --dport 9191 "
        "-j RETURN",
    ]
    rejected = {rule.split()[3] for rule in forward if "-j REJECT" in rule}
    assert rejected == {
        "169.254.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
    }  # fmt: skip
    # The passes come first, then the rejects.
    assert forward.index(passes[-1]) < min(i for i, rule in enumerate(forward) if "REJECT" in rule)


def test_prometheus_scrapes_worker_browser() -> None:
    config = yaml.safe_load((VM / "prometheus" / "prometheus.yml").read_text())
    targets = {
        job["job_name"]: [t for static in job["static_configs"] for t in static["targets"]]
        for job in config["scrape_configs"]
    }
    assert targets["worker-browser"] == ["worker-browser:9191"]


def test_seccomp_profile_is_dockers_default_plus_the_namespace_sandbox() -> None:
    profile = json.loads((VM / "seccomp-chromium.json").read_text())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    first, *rest = profile["syscalls"]
    assert first["names"] == ["chroot", "clone", "setns", "unshare"]
    assert first["action"] == "SCMP_ACT_ALLOW"
    assert [first.get(key) for key in ("args", "includes", "excludes")] == [None, None, None]
    # Docker's own rules follow unchanged; clone3 still fails with ENOSYS, so glibc uses clone.
    clone3 = [rule for rule in rest if "clone3" in rule["names"] and "errnoRet" in rule]
    assert [(rule["action"], rule["errnoRet"]) for rule in clone3] == [("SCMP_ACT_ERRNO", 38)]
    assert all(rule["action"] in {"SCMP_ACT_ALLOW", "SCMP_ACT_ERRNO"} for rule in rest)
