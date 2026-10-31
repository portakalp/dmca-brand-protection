"""CZDS client and zone parsing, without network (architecture.md §8)."""

from __future__ import annotations

import gzip
import inspect
import json
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from kalkan.discovery.czds import (
    AUTH_URL,
    LINKS_URL,
    CzdsClient,
    CzdsError,
    gunzip_lines,
    iter_ns_owners,
    run_czds_scan,
    select_links,
    tld_of,
)
from tests.support import make_settings

ZONE = Path(__file__).resolve().parents[3] / "fixtures" / "czds" / "xyz.zone.gz"
LINK = "https://czds-api.icann.org/czds/downloads/{}.zone"


def _chunks(data: bytes, size: int = 64) -> Iterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def test_fixture_owner_names() -> None:
    owners = list(iter_ns_owners(gunzip_lines(_chunks(ZONE.read_bytes())), "xyz"))
    assert len(owners) == 120
    assert len(set(owners)) == 120
    assert "xyz" not in owners
    assert "ns1.mintera-login.xyz" not in owners  # glue A record
    assert "mintera-offers.xyz" not in owners  # DS record only
    assert {"mintera-login.xyz", "m1ntera.xyz", "xn--minter-8nf.xyz"} <= set(owners)


def test_the_parser_streams_instead_of_reading_the_whole_file() -> None:
    consumed = 0
    data = ZONE.read_bytes()

    def chunks() -> Iterator[bytes]:
        nonlocal consumed
        for chunk in _chunks(data, 32):
            consumed += 1
            yield chunk

    total = len(range(0, len(data), 32))
    owners = iter_ns_owners(gunzip_lines(chunks()), "xyz")
    assert inspect.isgenerator(owners)
    assert consumed == 0  # nothing is read until the first name is asked for
    assert next(owners)
    assert 0 < consumed < total


def test_concatenated_gzip_members() -> None:
    data = gzip.compress(b"a.xyz. 60 in ns x.\n", mtime=0) + gzip.compress(
        b"b.xyz. 60 in ns x.\n", mtime=0
    )
    assert list(iter_ns_owners(gunzip_lines(_chunks(data, 7)), "xyz")) == ["a.xyz", "b.xyz"]


def test_last_line_without_newline() -> None:
    data = gzip.compress(b"a.xyz. 60 in ns x.\nb.xyz. 60 in ns x.", mtime=0)
    assert list(iter_ns_owners(gunzip_lines([data]), "xyz")) == ["a.xyz", "b.xyz"]


def test_tld_selection() -> None:
    links = [LINK.format(tld) for tld in ("com", "net", "xyz", "online", "top")]
    assert [tld for tld, _ in select_links(links, [])] == ["online", "top", "xyz"]
    assert [tld for tld, _ in select_links(links, ["com", " .XYZ "])] == ["com", "xyz"]
    # "*" (K106): every approved TLD, .com and .net included.
    assert [tld for tld, _ in select_links(links, ["*"])] == ["com", "net", "online", "top", "xyz"]
    assert tld_of(LINK.format("bank")) == "bank"


def _transport(zone: bytes, *, auth_status: int = 200) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == AUTH_URL:
            assert json.loads(request.content) == {"username": "u", "password": "p"}
            if auth_status != 200:
                return httpx2.Response(auth_status)
            return httpx2.Response(200, json={"accessToken": "token-1"})
        assert request.headers["Authorization"] == "Bearer token-1"
        if url == LINKS_URL:
            return httpx2.Response(200, json=[LINK.format("xyz"), LINK.format("com")])
        if url == LINK.format("xyz"):
            return httpx2.Response(200, content=zone)
        return httpx2.Response(404)

    return httpx2.MockTransport(handler)


def test_client_authenticates_lists_and_streams() -> None:
    with CzdsClient("u", "p", transport=_transport(ZONE.read_bytes())) as client:
        assert client.links() == [LINK.format("xyz"), LINK.format("com")]
        with client.zone(LINK.format("xyz")) as chunks:
            owners = list(iter_ns_owners(gunzip_lines(chunks), "xyz"))
        assert len(owners) == 120
        with pytest.raises(CzdsError, match="404"), client.zone(LINK.format("top")) as chunks:
            list(chunks)


def test_failed_authentication() -> None:
    with (
        CzdsClient("u", "p", transport=_transport(b"", auth_status=401)) as client,
        pytest.raises(CzdsError, match="401"),
    ):
        client.links()


def test_scan_is_skipped_without_credentials(clean_env: pytest.MonkeyPatch) -> None:
    def fail(_: object) -> None:
        raise AssertionError("must not queue anything")

    assert run_czds_scan(make_settings(env="local"), None, fail, None) == []  # type: ignore[arg-type]
