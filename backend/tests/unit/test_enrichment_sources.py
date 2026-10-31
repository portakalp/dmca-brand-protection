"""Enrichment sources without network: DNS, RDAP, Cloudflare, Web Risk (architecture.md §9)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest
import requests
import responses
import whoisit

from kalkan.enrichment import dns as dnslookup
from kalkan.enrichment.cloudflare import (
    IPS_URL,
    RANGES_KEY,
    fetch_ranges,
    in_ranges,
    is_cloudflare,
    load_ranges,
)
from kalkan.enrichment.rdap import (
    BOOTSTRAP_KEY,
    RdapClient,
    RdapSession,
    RdapThrottledError,
    RdapUnavailableError,
    parse_domain,
    parse_ip,
)
from kalkan.enrichment.service import Footprint, shared_infrastructure
from kalkan.enrichment.webrisk import SEARCH_URL, WebRiskClient, WebRiskError
from kalkan.logging import configure_logging

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "rdap"
CLOUDFLARE = ["104.16.0.0/13", "2606:4700::/32"]


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def fresh_bootstrap() -> str:
    """The recorded IANA bootstrap, stamped as downloaded now."""
    data = fixture("bootstrap.json")
    data["timestamp"] = int(time.time())
    return json.dumps(data)


class MemoryCache:
    def __init__(self, **values: str) -> None:
        self.values: dict[str, str] = dict(values)
        self.expiry: dict[str, int] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str, *, ex: int | None = None) -> None:
        self.values[name] = value
        if ex is not None:
            self.expiry[name] = ex


class FreeBucket:
    def __init__(self, waits: list[float] | None = None) -> None:
        self.waits = list(waits or [])
        self.keys: list[str] = []

    def acquire(self, key: str, *, rate: float, burst: int) -> float:
        self.keys.append(key)
        return self.waits.pop(0) if self.waits else 0.0


@pytest.fixture
def rdap() -> RdapClient:
    whoisit.clear_bootstrapping()
    return RdapClient(MemoryCache(**{BOOTSTRAP_KEY: fresh_bootstrap()}), FreeBucket())


# --- RDAP --------------------------------------------------------------------------------------


def test_parse_registry_answers() -> None:
    assert parse_domain(fixture("domain-namecheap.com.json")) == {
        "registrar": "NameCheap, Inc.",
        "registrar_iana_id": 1068,
        "registrar_abuse_email": "abuse@namecheap.com",
        "registered_at": "2000-08-11T16:15:25Z",
        "nameservers": [
            "edns3.registrar-servers.com",
            "edns4.registrar-servers.com",
            "edns4.ultradns.biz",
            "edns4.ultradns.com",
            "edns4.ultradns.net",
            "edns4.ultradns.org",
        ],
        "status": [
            "client transfer prohibited",
            "server delete prohibited",
            "server transfer prohibited",
            "server update prohibited",
        ],
    }
    org = parse_domain(fixture("domain-wikipedia.org.json"))
    assert (org["registrar"], org["registrar_iana_id"]) == ("MarkMonitor Inc.", 292)
    assert org["registrar_abuse_email"] == "abusecomplaints@markmonitor.com"


def test_parse_rir_answers() -> None:
    assert parse_ip(fixture("ip-88.198.0.1.json"))["org"] == "Hetzner Online GmbH"
    assert parse_ip(fixture("ip-88.198.0.1.json"))["abuse_email"] == "abuse@hetzner.com"
    assert parse_ip(fixture("ip-1.1.1.1.json"))["country"] == "AU"
    assert parse_domain({}) == {
        "registrar": None,
        "registrar_iana_id": None,
        "registrar_abuse_email": None,
        "registered_at": None,
        "nameservers": [],
        "status": [],
    }


@responses.activate
def test_domain_lookup_uses_the_bootstrap_server(rdap: RdapClient) -> None:
    url = "https://rdap.verisign.com/com/v1/domain/namecheap.com"
    responses.get(url, json=fixture("domain-namecheap.com.json"))
    assert rdap.domain("namecheap.com") == fixture("domain-namecheap.com.json")
    assert [call.request.url for call in responses.calls] == [url]


@responses.activate
def test_unknown_domain_and_missing_rdap_service(rdap: RdapClient) -> None:
    responses.get("https://rdap.verisign.com/com/v1/domain/nothing-here.com", status=404)
    assert rdap.domain("nothing-here.com") is None
    for name in ("mintera.com.tr", "mintera.ru", "mintera.de"):
        with pytest.raises(RdapUnavailableError):
            rdap.domain(name)
    assert len(responses.calls) == 1


@responses.activate
def test_rate_limited_server(rdap: RdapClient) -> None:
    responses.get("https://rdap.verisign.com/com/v1/domain/busy.com", status=429)
    with pytest.raises(RdapThrottledError):
        rdap.domain("busy.com")


@responses.activate
def test_ip_answers_are_cached(rdap: RdapClient) -> None:
    responses.get("https://rdap.db.ripe.net/ip/88.198.0.1", json=fixture("ip-88.198.0.1.json"))
    assert rdap.ip("88.198.0.1") == fixture("ip-88.198.0.1.json")
    assert rdap.ip("88.198.0.1") == fixture("ip-88.198.0.1.json")
    assert len(responses.calls) == 1


def test_throttle_waits_for_a_slot_then_gives_up() -> None:
    whoisit.clear_bootstrapping()
    slept: list[float] = []
    cache = MemoryCache(**{BOOTSTRAP_KEY: fresh_bootstrap()})
    patient = RdapClient(cache, FreeBucket([0.5, 0.25]), sleep=slept.append)
    with responses.RequestsMock() as mocked:
        mocked.get("https://rdap.verisign.com/com/v1/domain/namecheap.com", json={})
        patient.domain("namecheap.com")
    assert slept == [0.5, 0.25]

    starved = RdapClient(cache, FreeBucket([10.0, 10.0]), sleep=slept.append)
    with pytest.raises(RdapThrottledError):
        starved.domain("namecheap.com")


def test_bootstrap_comes_from_the_shared_cache() -> None:
    whoisit.clear_bootstrapping()
    cache = MemoryCache(**{BOOTSTRAP_KEY: fresh_bootstrap()})
    RdapClient(cache, FreeBucket()).ensure_bootstrap()
    assert whoisit.is_bootstrapped()
    assert not whoisit.bootstrap_is_older_than(1)


# --- DNS ----------------------------------------------------------------------------------------


class FakeResolver:
    def __init__(self, answers: dict[tuple[str, str], Any]) -> None:
        self.answers = answers

    def resolve(self, name: str, rdtype: str) -> list[str]:
        answer = self.answers.get((name, rdtype), [])
        if isinstance(answer, Exception):
            raise answer
        return list(answer)


def test_dns_lookup() -> None:
    resolver = FakeResolver(
        {
            ("login.mintera-x.com", "A"): ["203.0.113.10"],
            ("login.mintera-x.com", "AAAA"): dnslookup.LookupFailedError("AAAA timeout"),
            ("mintera-x.com", "NS"): ["NS1.Example-DNS.net.", "ns2.example-dns.net."],
        }
    )
    result = dnslookup.lookup("login.mintera-x.com", "mintera-x.com", resolver)
    assert result.ips == ["203.0.113.10"]
    assert result.nameservers == ["ns1.example-dns.net", "ns2.example-dns.net"]
    assert result.errors == {"AAAA": "AAAA timeout"}
    assert not result.nxdomain

    gone = FakeResolver({("gone.com", "NS"): dnslookup.NoSuchDomainError("gone.com")})
    assert dnslookup.lookup("gone.com", "gone.com", gone).nxdomain


def test_origin_from_team_cymru() -> None:
    resolver = FakeResolver(
        {
            ("1.0.198.88.origin.asn.cymru.com", "TXT"): ['"24940 | 88.198.0.0/16 | DE | ripencc"'],
            ("1.0.16.104.origin.asn.cymru.com", "TXT"): ['"13335 | 104.16.0.0/12 | US | arin"'],
            ("4.3.2.1.origin.asn.cymru.com", "TXT"): dnslookup.NoSuchDomainError("x"),
            ("9.9.9.9.origin.asn.cymru.com", "TXT"): ['"NA | 9.9.9.0/24 | | other"'],
        }
    )
    assert dnslookup.origin_for_ip("88.198.0.1", resolver) == dnslookup.Origin(24940, "DE")
    with pytest.raises(dnslookup.LookupFailedError):
        dnslookup.origin_for_ip(
            "5.6.7.8",
            FakeResolver(
                {("8.7.6.5.origin.asn.cymru.com", "TXT"): dnslookup.LookupFailedError("t")}
            ),
        )
    assert dnslookup.origin_for_ip("104.16.0.1", resolver) == dnslookup.Origin(13335, "US")
    assert dnslookup.origin_for_ip("1.2.3.4", resolver) is None
    assert dnslookup.origin_for_ip("9.9.9.9", resolver) is None
    assert dnslookup.origin_for_ip("2606:4700::1", FakeResolver({})) is None


def test_arin_answers_carry_no_country() -> None:
    parsed = parse_ip(fixture("ip-104.16.0.1.json"))
    assert parsed["org"] == "Cloudflare, Inc."
    assert parsed["country"] is None
    assert parsed["abuse_email"] == "abuse@cloudflare.com"


# --- Cloudflare and Web Risk --------------------------------------------------------------------


def test_cloudflare_by_ip_or_name_server() -> None:
    assert is_cloudflare(["104.18.1.1"], [], CLOUDFLARE)
    assert is_cloudflare(["2606:4700::6810:1"], [], CLOUDFLARE)
    assert is_cloudflare(["203.0.113.5"], ["lara.ns.cloudflare.com."], CLOUDFLARE)
    assert not is_cloudflare(["203.0.113.5"], ["ns1.example-dns.net"], CLOUDFLARE)
    assert not in_ranges("104.24.0.1", CLOUDFLARE)


def test_cloudflare_ranges_are_fetched_once_and_cached() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        assert str(request.url) == IPS_URL
        assert "authorization" not in request.headers
        result = {"ipv4_cidrs": CLOUDFLARE[:1], "ipv6_cidrs": CLOUDFLARE[1:], "etag": "x"}
        return httpx2.Response(200, json={"success": True, "result": result})

    store = MemoryCache()
    transport = httpx2.MockTransport(handler)
    assert load_ranges(store, transport=transport) == sorted(CLOUDFLARE)
    assert load_ranges(store, transport=transport) == sorted(CLOUDFLARE)
    assert calls == 1
    assert json.loads(store.values[RANGES_KEY]) == sorted(CLOUDFLARE)


def test_cloudflare_refuses_an_empty_answer() -> None:
    transport = httpx2.MockTransport(
        lambda _: httpx2.Response(200, json={"success": True, "result": {}})
    )
    with pytest.raises(ValueError, match="no IP ranges"):
        fetch_ranges(transport=transport)


WEBRISK = Path(__file__).resolve().parents[3] / "fixtures" / "webrisk"


def test_web_risk_lookup(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.params["uri"] == "http://phish.example/":
            return httpx2.Response(200, content=(WEBRISK / "uris-search-match.json").read_bytes())
        if request.url.params["uri"] == "http://broken.example/":
            return httpx2.Response(403)
        return httpx2.Response(200, content=(WEBRISK / "uris-search-empty.json").read_bytes())

    configure_logging("test", "DEBUG")
    client = WebRiskClient("secret-key", transport=httpx2.MockTransport(handler))
    with caplog.at_level(logging.DEBUG):
        assert client.lookup("phish.example") == {
            "threat_types": ["MALWARE"],
            "expire_time": "2019-07-17T15:01:23.045123456Z",
        }
        assert client.lookup("clean.example") == {"threat_types": [], "expire_time": None}
    assert str(seen[0].url).startswith(SEARCH_URL)
    assert seen[0].url.params.get_list("threatTypes") == [
        "SOCIAL_ENGINEERING",
        "MALWARE",
        "UNWANTED_SOFTWARE",
    ]
    # The key travels in a header, never in the URL that HTTP clients log.
    assert seen[0].headers["X-Goog-Api-Key"] == "secret-key"
    assert all("secret-key" not in str(request.url) for request in seen)
    assert "secret-key" not in caplog.text
    with pytest.raises(WebRiskError) as raised:
        client.lookup("broken.example")
    assert "secret-key" not in str(raised.value)


# --- official infrastructure ---------------------------------------------------------------------

OFFICIAL = Footprint(
    "mintera.com",
    frozenset({"203.0.113.10", "104.18.1.1", "172.30.255.80", "127.0.0.1"}),
    frozenset({"ns1.mintera.com", "dns1.registrar-servers.com", "lara.ns.cloudflare.com"}),
)


def test_unknown_infrastructure_is_not_reported_as_false() -> None:
    unknown_official = Footprint("mintera.com", frozenset(), frozenset(), unknown=True)
    detail = shared_infrastructure(["198.51.100.7"], [], [unknown_official], CLOUDFLARE)
    assert detail["match"] is None
    assert detail["unknown_official_domains"] == ["mintera.com"]
    no_ranges = shared_infrastructure(["198.51.100.7"], [], [OFFICIAL], None)
    assert no_ranges["match"] is None
    assert not no_ranges["cloudflare_ranges"]
    # A real match stays a match even when something else is unknown.
    assert shared_infrastructure(["203.0.113.10"], [], [OFFICIAL], None)["match"] is True


@pytest.mark.parametrize(
    ("ips", "nameservers", "match", "shared_ips", "shared_ns"),
    [
        (["203.0.113.10"], [], True, ["203.0.113.10"], []),  # the official site's own server
        ([], ["ns1.mintera.com"], True, [], ["ns1.mintera.com"]),  # its vanity name server
        (["104.18.1.1"], [], False, [], []),  # a Cloudflare IP is shared by many sites
        ([], ["lara.ns.cloudflare.com"], False, [], []),
        ([], ["dns1.registrar-servers.com"], False, [], []),  # a registrar's default DNS
        (["198.51.100.7"], ["ns1.other.net"], False, [], []),
        (["172.30.255.80"], [], False, [], []),  # a private address is nobody's server
        (["127.0.0.1"], [], False, [], []),
    ],
)
def test_shared_infrastructure(
    ips: list[str], nameservers: list[str], match: bool, shared_ips: list[str], shared_ns: list[str]
) -> None:
    detail = shared_infrastructure(ips, nameservers, [OFFICIAL], CLOUDFLARE)
    assert detail["match"] is match
    assert detail["shared_ips"] == shared_ips
    assert detail["shared_nameservers"] == shared_ns
    assert detail["official_domains"] == (["mintera.com"] if match else [])


# --- review fixes --------------------------------------------------------------------------------


@responses.activate
def test_rdap_redirects_only_to_rir_servers(rdap: RdapClient) -> None:
    responses.get(
        "https://rdap.verisign.com/com/v1/domain/moved.com",
        status=302,
        headers={"Location": "http://attacker.example/rdap"},
    )
    with pytest.raises(Exception, match="302"):
        rdap.domain("moved.com")
    assert [call.request.url for call in responses.calls] == [
        "https://rdap.verisign.com/com/v1/domain/moved.com"
    ]


def test_rdap_session_allows_rir_referrals() -> None:
    session = RdapSession()
    referral = requests.Response()
    referral.status_code = 301
    referral.url = "https://rdap.arin.net/registry/ip/88.198.0.1"
    referral.headers["Location"] = "https://rdap.db.ripe.net/ip/88.198.0.1"
    assert session.get_redirect_target(referral) == "https://rdap.db.ripe.net/ip/88.198.0.1"
    referral.headers["Location"] = "https://rdap.attacker.example/ip/88.198.0.1"
    assert session.get_redirect_target(referral) is None


def test_private_ips_are_never_queried(rdap: RdapClient) -> None:
    for address in ("127.0.0.1", "10.0.0.8", "0.0.0.0", "::1", "192.168.1.1"):  # noqa: S104
        assert rdap.ip(address) is None


def test_a_failed_bootstrap_refresh_keeps_the_old_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    whoisit.clear_bootstrapping()
    stale = fixture("bootstrap.json")
    stale["timestamp"] = int(time.time()) - 3 * 24 * 3600
    whoisit.load_bootstrap_data(json.dumps(stale))
    cache = MemoryCache(**{BOOTSTRAP_KEY: json.dumps(stale)})

    def fail(self: object) -> None:
        raise ConnectionError("IANA is down")

    monkeypatch.setattr("kalkan.enrichment.rdap.Bootstrap.bootstrap", fail)
    RdapClient(cache, FreeBucket()).ensure_bootstrap()
    assert whoisit.is_bootstrapped()
    assert whoisit.build_query(query_type="domain", query_value="mintera.com")[1].startswith(
        "https://rdap.verisign.com/"
    )
