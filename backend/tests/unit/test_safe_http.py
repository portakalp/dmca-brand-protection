"""SSRF-safe HTTP client (architecture.md §10)."""

from __future__ import annotations

import gzip
import ipaddress
from collections.abc import Iterator

import httpx2
import pytest

from kalkan.net.safe_http import (
    FetchError,
    NoSuchHostError,
    SafeHttpClient,
    SsrfBlockedError,
    is_public,
)

PUBLIC = "93.184.215.14"


class _Raw(httpx2.SyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks


def _client(
    names: dict[str, list[str]],
    handler: object = None,
    **options: object,
) -> tuple[SafeHttpClient, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        response: httpx2.Response = (
            handler(request) if callable(handler) else httpx2.Response(200, text="ok")
        )
        if not response.is_stream_consumed:
            return response
        # The client reads raw bytes, as a real transport delivers them.
        return httpx2.Response(
            response.status_code, headers=response.headers, stream=_Raw(response.content)
        )

    def resolve(host: str) -> list[str]:
        if host not in names:
            raise NoSuchHostError(host)
        return names[host]

    client = SafeHttpClient(
        resolver=resolve,
        transport=httpx2.MockTransport(respond),
        **options,  # type: ignore[arg-type]
    )
    return client, seen


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.1.2.3/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://127.0.0.1:8000/",
        "http://100.64.0.1/",  # CGNAT
        "http://224.0.0.1/",  # multicast
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fd00::1]/",  # ULA
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
    ],
)
def test_private_targets_are_refused(url: str) -> None:
    client, seen = _client({})
    with pytest.raises(SsrfBlockedError):
        client.get(url)
    assert seen == []


def test_a_name_that_resolves_to_a_private_address_is_refused() -> None:
    # DNS rebinding: one public and one private answer is refused as a whole.
    client, seen = _client(
        {"rebind.example": [PUBLIC, "10.0.0.7"], "internal.example": ["169.254.169.254"]}
    )
    for url in ("https://rebind.example/", "https://internal.example/"):
        with pytest.raises(SsrfBlockedError):
            client.get(url)
    assert seen == []


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/",
        "http://10.0.0.1/admin",
        "http://internal.example/",
        "http://[::1]/",
        "http://[::ffff:169.254.169.254]/",
        "file:///etc/passwd",
    ],
)
def test_redirects_to_private_targets_are_refused(target: str) -> None:
    def redirect(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"Location": target})

    client, seen = _client({"phish.example": [PUBLIC], "internal.example": ["10.9.9.9"]}, redirect)
    with pytest.raises(SsrfBlockedError):
        client.get("https://phish.example/")
    assert len(seen) == 1


def test_every_hop_is_resolved_and_checked_again() -> None:
    # Rebinding between hops: the second answer for the same name is private.
    answers = iter([[PUBLIC], ["10.0.0.1"]])

    def redirect(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"Location": "/next"}, stream=_Raw(b""))

    client = SafeHttpClient(
        resolver=lambda host: next(answers), transport=httpx2.MockTransport(redirect)
    )
    with pytest.raises(SsrfBlockedError):
        client.get("https://rebind.example/")


def test_the_connection_is_pinned_to_the_checked_address() -> None:
    client, seen = _client({"phish.example": [PUBLIC]})
    response = client.get("https://phish.example/login?next=1", {"User-Agent": "test"})
    assert response.status == 200
    assert response.address == PUBLIC
    request = seen[0]
    assert request.url.host == PUBLIC  # no second DNS lookup by the HTTP library
    assert request.url.path == "/login"
    assert request.url.query == b"next=1"
    assert request.headers["Host"] == "phish.example"
    assert request.extensions["sni_hostname"] == "phish.example"
    assert request.headers["User-Agent"] == "test"


def test_redirects_are_followed_and_recorded() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers["Host"] == "a.example":
            return httpx2.Response(301, headers={"Location": "http://b.example/x"})
        if request.url.path == "/x":
            return httpx2.Response(302, headers={"Location": "/y"})
        return httpx2.Response(200, text="final")

    client, _ = _client({"a.example": [PUBLIC], "b.example": [PUBLIC]}, handler)
    response = client.get("https://a.example/")
    assert response.url == "http://b.example/y"
    assert response.redirect_chain == ["https://a.example/", "http://b.example/x"]
    assert response.body == b"final"


def test_redirect_loops_stop() -> None:
    def loop(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"Location": "/again"})

    client, seen = _client({"loop.example": [PUBLIC]}, loop)
    with pytest.raises(FetchError, match="redirects"):
        client.get("https://loop.example/")
    assert len(seen) == 11


def test_bodies_are_cut_at_the_limit() -> None:
    client, _ = _client(
        {"big.example": [PUBLIC]},
        lambda request: httpx2.Response(200, content=b"x" * 5000),
        max_bytes=1024,
    )
    response = client.get("https://big.example/")
    assert len(response.body) == 1024
    assert response.truncated


@pytest.mark.parametrize("url", ["ftp://phish.example/", "http://user:pw@phish.example/"])
def test_other_schemes_and_credentials_are_refused(url: str) -> None:
    client, _ = _client({"phish.example": [PUBLIC]})
    with pytest.raises(SsrfBlockedError):
        client.get(url)


def test_unknown_hosts() -> None:
    client, _ = _client({})
    with pytest.raises(NoSuchHostError):
        client.get("https://nothing.example/")


def test_the_deadline_covers_the_whole_fetch() -> None:
    clock = [0.0]

    def slow(request: httpx2.Request) -> httpx2.Response:
        clock[0] += 10
        return httpx2.Response(302, headers={"Location": "/next"})

    client, seen = _client(
        {"slow.example": [PUBLIC]}, slow, total_seconds=15, clock=lambda: clock[0]
    )
    with pytest.raises(FetchError, match="time-out"):
        client.get("https://slow.example/")
    assert len(seen) == 2


def test_allowed_networks_and_the_port_map_are_for_tests_only() -> None:
    fixture = ipaddress.ip_network("127.0.0.1/32")
    client, seen = _client(
        {"mintera.test": ["127.0.0.1"]}, allow_cidrs=[fixture], port_map={80: 18080}
    )
    client.get("http://mintera.test/")
    assert seen[0].url.port == 18080
    assert seen[0].headers["Host"] == "mintera.test"

    # Without allowed networks the port map is ignored.
    plain, seen_plain = _client({"phish.example": [PUBLIC]}, port_map={80: 18080})
    plain.get("http://phish.example/")
    assert seen_plain[0].url.port is None or seen_plain[0].url.port == 80


def test_is_public() -> None:
    assert is_public(ipaddress.ip_address("8.8.8.8"))
    assert is_public(ipaddress.ip_address("2606:4700::1"))
    assert not is_public(ipaddress.ip_address("192.0.2.1"))  # documentation range
    assert not is_public(ipaddress.ip_address("224.0.0.251"))


def _gzip(data: bytes) -> bytes:
    return gzip.compress(data)


def test_compressed_bodies_are_decoded_and_capped() -> None:
    page = b"<html><title>Mintera Bank</title></html>"
    client, seen = _client(
        {"gz.example": [PUBLIC]},
        lambda request: httpx2.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=_Raw(_gzip(page))
        ),
    )
    response = client.get("https://gz.example/", {"Accept-Encoding": "br"})
    assert response.body == page
    assert not response.truncated
    assert seen[0].headers["Accept-Encoding"] == "gzip, deflate"

    # A small body that expands past the cap is cut at the cap, not decoded in full.
    bomb = _gzip(b"x" * 10_000_000)
    client, _ = _client(
        {"bomb.example": [PUBLIC]},
        lambda request: httpx2.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=_Raw(bomb)
        ),
        max_bytes=4096,
    )
    response = client.get("https://bomb.example/")
    assert len(response.body) == 4096
    assert response.truncated


def test_bytes_after_a_gzip_member_count_towards_the_cap() -> None:
    reads: list[int] = []

    class Endless(httpx2.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield _gzip(b"<html>hi</html>")
            while True:
                reads.append(1)
                yield b"junk" * 1024

    client, _ = _client(
        {"junk.example": [PUBLIC]},
        lambda request: httpx2.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=Endless()
        ),
        max_bytes=64 * 1024,
    )
    response = client.get("https://junk.example/")
    assert response.body == b"<html>hi</html>"
    assert response.truncated
    assert len(reads) <= 17  # stopped at the raw cap


def test_endless_empty_deflate_blocks_stop_at_the_cap() -> None:
    class Blocks(httpx2.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"  # gzip header
            while True:
                yield b"\x00\x00\x00\xff\xff" * 1000  # empty stored blocks

    client, _ = _client(
        {"blocks.example": [PUBLIC]},
        lambda request: httpx2.Response(200, headers={"Content-Encoding": "gzip"}, stream=Blocks()),
        max_bytes=64 * 1024,
    )
    response = client.get("https://blocks.example/")
    assert response.body == b""
    assert response.truncated


def test_a_trickled_body_stops_at_the_deadline() -> None:
    clock = [0.0]

    class Trickle(httpx2.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            while True:
                clock[0] += 1
                yield b"x"

    client, _ = _client(
        {"slow.example": [PUBLIC]},
        lambda request: httpx2.Response(200, stream=Trickle()),
        total_seconds=15,
        clock=lambda: clock[0],
    )
    with pytest.raises(FetchError, match="time-out"):
        client.get("https://slow.example/")
    assert clock[0] <= 17


def test_a_shared_deadline_shortens_the_budget() -> None:
    clock = [100.0]
    client, seen = _client({"phish.example": [PUBLIC]}, clock=lambda: clock[0])
    with pytest.raises(FetchError, match="time-out"):
        client.get("https://phish.example/", deadline=99.0)
    assert seen == []


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"Content-Encoding": "br"}, b"\x0b\x02\x80hello\x03"),  # never asked for
        ({"Content-Encoding": "gzip"}, b"junk"),  # not gzip at all
    ],
)
def test_undecodable_bodies_are_fetch_errors(headers: dict[str, str], body: bytes) -> None:
    client, _ = _client(
        {"enc.example": [PUBLIC]},
        lambda request: httpx2.Response(200, headers=headers, stream=_Raw(body)),
    )
    with pytest.raises(FetchError):
        client.get("https://enc.example/")


@pytest.mark.parametrize(
    "url",
    [
        "https://phish.example/" + "a" * 70_000,  # the HTTP library refuses the URL
        "http://[evil/",
    ],
)
def test_malformed_urls_are_fetch_errors(url: str) -> None:
    client, _ = _client({"phish.example": [PUBLIC]})
    with pytest.raises(FetchError):
        client.get(url)


def test_a_malformed_redirect_is_a_fetch_error() -> None:
    client, _ = _client(
        {"phish.example": [PUBLIC]},
        lambda request: httpx2.Response(302, headers={"Location": "http://[evil/"}),
    )
    with pytest.raises(FetchError):
        client.get("https://phish.example/")


def test_cookies_stay_within_one_fetch_and_one_host() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers["Host"] == "a.example" and request.url.path == "/":
            return httpx2.Response(
                302, headers={"Location": "/in", "Set-Cookie": "check=1; Path=/; HttpOnly"}
            )
        return httpx2.Response(200, headers={"Set-Cookie": "session=secret-of-a"}, text="ok")

    client, seen = _client({"a.example": [PUBLIC], "b.example": [PUBLIC]}, handler)
    client.get("https://a.example/", {"Cookie": "from=caller"})
    assert "cookie" not in seen[0].headers
    assert seen[1].headers["Cookie"] == "check=1"  # the cookie check passes
    client.get("https://b.example/")  # same IP, other site, later fetch
    assert "cookie" not in seen[2].headers
