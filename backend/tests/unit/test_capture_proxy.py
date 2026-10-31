from __future__ import annotations

import re

import pytest
from pydantic import SecretStr, ValidationError

from kalkan.scanning.proxy import (
    ProxyTemplateError,
    new_session,
    parse_template,
    select_proxy,
    validate_template,
)
from tests.support import make_settings

pytestmark = pytest.mark.usefixtures("clean_env")


def _map(**values: str) -> dict[str, SecretStr]:
    return {country: SecretStr(value) for country, value in values.items()}


def test_plain_proxy_has_no_credentials() -> None:
    choice = parse_template("PL", "http://dev-proxy:8888", "abcd1234")
    assert choice.server == "http://dev-proxy:8888"
    assert choice.playwright() == {"server": "http://dev-proxy:8888"}
    assert choice.endpoint == "dev-proxy:8888"


def test_session_placeholder_in_the_user_part() -> None:
    template = "http://country-pl-session-{session}:PKGKEY@proxy.soax.com:1337"
    choice = parse_template("PL", template, "0a1b2c3d")
    assert choice.server == "http://proxy.soax.com:1337"
    assert (choice.username, choice.password) == ("country-pl-session-0a1b2c3d", "PKGKEY")
    # Chromium never gets the credentials; the forwarder holds them.
    with pytest.raises(ValueError, match="proxy_forwarder"):
        choice.playwright()


def test_session_placeholder_in_the_password_part() -> None:
    template = "http://USER:PASS_country-pl_session-{session}_lifetime-5m@geo.iproyal.com:12321"
    choice = parse_template("PL", template, "0a1b2c3d")
    assert choice.username == "USER"
    assert choice.password == "PASS_country-pl_session-0a1b2c3d_lifetime-5m"


def test_credentials_are_percent_decoded() -> None:
    choice = parse_template("DE", "http://us%40er:p%3Ass%2Fw%40rd@gate.example:7000", "x")
    assert choice.username == "us@er"
    assert choice.password == "p:ss/w@rd"
    assert choice.server == "http://gate.example:7000"


def test_credentials_never_reach_server_repr_or_endpoint() -> None:
    choice = parse_template("PL", "http://user-{session}:hunter2@proxy.example:1337", "abcd1234")
    assert "hunter2" not in choice.server
    assert "hunter2" not in repr(choice)
    assert "user-abcd1234" not in repr(choice)
    assert "hunter2" not in choice.endpoint


@pytest.mark.parametrize(
    "template",
    [
        "socks5://user:pass@proxy:1080",
        "https://user:pass@proxy:443",
        "http://proxy",  # no port
        "http://user:pass@proxy:8080/path",
        "http://proxy:8080?x=1",
        "http://proxy:8080#frag",
        "http://user-{country}:pass@proxy:8080",  # only {session} is known
        "http://proxy-{session}.example:8080",  # the placeholder belongs in the credentials
        "http://user:pa ss@proxy:8080",
        "http://:pass@proxy:8080",
        "http://proxy:99999",
        "proxy:8080",
        "",
    ],
)
def test_malformed_templates_are_rejected(template: str) -> None:
    with pytest.raises(ProxyTemplateError) as raised:
        validate_template(template)
    assert "pass" not in str(raised.value).replace("password", "")


def test_select_prefers_the_job_country() -> None:
    proxies = _map(PL="http://pl-proxy:1", TR="http://tr-proxy:2")
    choice = select_proxy(proxies, "TR", "PL", session="abcd1234")
    assert choice is not None
    assert (choice.country, choice.server) == ("TR", "http://tr-proxy:2")


def test_select_falls_back_to_the_default_country() -> None:
    proxies = _map(PL="http://pl-proxy:1")
    choice = select_proxy(proxies, "RU", "PL", session="abcd1234")
    assert choice is not None
    assert choice.country == "PL"
    choice = select_proxy(proxies, None, "PL", session="abcd1234")
    assert choice is not None
    assert choice.country == "PL"


@pytest.mark.parametrize(
    ("proxies", "default"),
    [({}, "PL"), ({"TR": "http://tr:1"}, "PL"), ({"TR": "http://tr:1"}, None)],
)
def test_no_proxy_means_none(proxies: dict[str, str], default: str | None) -> None:
    assert select_proxy(_map(**proxies), "RU", default, session="abcd1234") is None


def test_sessions_are_eight_hex_characters_and_differ() -> None:
    sessions = {new_session() for _ in range(50)}
    assert len(sessions) == 50
    assert all(re.fullmatch(r"[0-9a-f]{8}", session) for session in sessions)


def test_settings_reject_a_malformed_proxy_without_showing_it() -> None:
    with pytest.raises(ValidationError) as raised:
        make_settings(env="local", proxy_map={"PL": "socks5://user:topsecret@proxy:1080"})
    assert "PROXY_MAP[PL]" in str(raised.value)
    assert "topsecret" not in str(raised.value)


def test_settings_accept_templates() -> None:
    settings = make_settings(
        env="local",
        proxy_map={
            "PL": "http://country-pl-session-{session}:KEY@proxy.soax.com:1337",
            "DE": "http://dev-proxy:8888",
        },
    )
    assert set(settings.proxy_map) == {"PL", "DE"}
