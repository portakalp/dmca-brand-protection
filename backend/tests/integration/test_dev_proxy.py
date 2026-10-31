"""The local dev-proxy reaches the fixture sites and nothing else (infra/compose/dev-proxy)."""

from __future__ import annotations

import http.client

import pytest

# Host-run tests reach dev-proxy on its published port (PROXY_MAP {"PL": "http://127.0.0.1:18888"}).
PROXY = ("127.0.0.1", 18888)


def _get(url: str) -> tuple[int, bytes]:
    """GET through the proxy (absolute-form request line, as a browser sends to an HTTP proxy)."""
    connection = http.client.HTTPConnection(*PROXY, timeout=10)
    try:
        connection.request("GET", url)
    except ConnectionRefusedError:
        pytest.fail("dev-proxy is not running; start the services with `make up-infra`")
    response = connection.getresponse()
    try:
        return response.status, response.read()
    finally:
        connection.close()


def test_the_fixture_sites_answer_through_the_proxy() -> None:
    status, body = _get("http://mintera-login.test/")
    assert status == 200
    assert b"<title>Mintera Bank | Login</title>" in body


@pytest.mark.parametrize(
    "url",
    [
        "http://kalkan.invalid/",  # anything outside *.test
        "http://172.30.255.80/",  # the fixture sites by address
        "http://redis:6379/",  # the compose services
        "http://fake-gcs:4443/storage/v1/b",
        "http://127.0.0.1:8888/",
    ],
)
def test_everything_else_is_refused(url: str) -> None:
    status, body = _get(url)
    assert status == 403
    assert b"Filtered" in body
