"""HTTP pre-check pieces without network (architecture.md §10)."""

from __future__ import annotations

import base64
import time
import uuid
from datetime import timedelta
from pathlib import Path

import mmh3
import pytest

from kalkan.discovery.matcher import BrandProfile, Matcher
from kalkan.scanning import patterns
from kalkan.scanning.favicon import favicon_hash, image_type
from kalkan.scanning.html import LinkInfo, decode, parse_page
from kalkan.scanning.precheck import (
    REDIRECT_AGAIN,
    WATCH_AGAIN,
    Facts,
    Outcome,
    Route,
    classify_links,
    contains_brand,
    entry_urls,
    is_call_to_action,
    request_headers,
    route,
    without_hosts,
)

SITES = Path(__file__).resolve().parents[3] / "fixtures" / "sites"


def _page(name: str, base: str = "http://mintera-login.test/") -> str:
    return (SITES / name).read_text(encoding="utf-8")


# --- favicon -----------------------------------------------------------------------------------


def test_murmur3_reference_vectors() -> None:
    # MurmurHash3 x86_32, seed 0: the reference implementation's published values.
    assert mmh3.hash(b"") == 0
    assert mmh3.hash(b"hello") == 613153351
    assert mmh3.hash(b"The quick brown fox jumps over the lazy dog") == 776992547


def test_favicon_hash_uses_shodans_base64_framing() -> None:
    data = bytes(range(256)) * 2
    encoded = base64.encodebytes(data)
    assert all(len(line) <= 76 for line in encoded.split(b"\n"))
    assert encoded.endswith(b"\n")
    assert favicon_hash(data) == mmh3.hash(encoded)
    assert favicon_hash(b"abc") == mmh3.hash(b"YWJj\n")


# --- HTML ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (b"\x00\x00\x01\x00rest", "ico"),
        (b"\x89PNG\r\n", "png"),
        (b"RIFF\x10\x00\x00\x00WEBPVP8 ", "webp"),
        (b"\x00\x00\x00\x1cftypavif", "avif"),
        (b"BM\x36\x00", "bmp"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"/>', "svg"),
        (b"\xef\xbb\xbf<?xml version='1.0'?>\n<!-- logo -->\n<svg\n viewBox='0 0 1 1'/>", "svg"),
        (b"<!doctype html><html>not found</html>", None),
        (b"\xef\xbb\xbf<!-- app --><html><body>SPA</body></html>", None),
    ],
)
def test_image_type(data: bytes, kind: str | None) -> None:
    found = image_type(data)
    assert (found[0] if found else None) == kind


def test_the_clone_page() -> None:
    page = parse_page(_page("clone/index.html"), "http://mintera-login.test/")
    assert page.title == "Mintera Bank | Login"
    assert page.icons == ["http://mintera-login.test/favicon.ico"]
    assert page.password_fields == 1
    form = page.forms[0]
    assert form.action == "http://mintera-login.test/collect"
    assert form.method == "post"
    assert {item.get("autocomplete") for item in form.inputs} >= {"cc-number", "current-password"}
    assert "Verify your account" in page.text
    assert "{" not in page.text  # styles are not text


def test_redirect_hints_links_and_buttons() -> None:
    html = """<html><head><meta http-equiv="Refresh" content="0; URL='/next'">
    <script>setTimeout(function(){
      window.location.replace("https://mintera-new.xyz/"); }, 10)</script>
    </head><body>
    <a href="javascript:void(0)">x</a><a href="mailto:a@b.c">mail</a><a href="#top">top</a>
    <a href="/promo">Promo <b>now</b></a>
    <button onclick="window.open('https://mintera-verify3.com/')">Doğrula</button>
    <form action="https://collect.example/steal"><input name="q"></form>
    </body></html>"""
    page = parse_page(html, "https://mintera-verify.test/start")
    assert page.meta_refresh == "https://mintera-verify.test/next"
    assert page.js_redirects == ["https://mintera-new.xyz/"]
    assert [(link.url, link.text, link.tag) for link in page.links] == [
        ("https://mintera-verify.test/promo", "Promo now", "a"),
        ("https://mintera-verify3.com/", "Doğrula", "button"),
        ("https://collect.example/steal", "", "form"),
    ]


def test_broken_markup_and_charsets() -> None:
    page = parse_page("<title> Mintera\n Bank </title><p>unclosed <div>text", "http://x.test/")
    assert page.title == "Mintera Bank"
    assert page.text == "unclosed text"
    assert decode("Doğrula".encode("iso-8859-9"), "text/html; charset=ISO-8859-9") == "Doğrula"
    assert decode('<meta charset="windows-1250">Zaloguj się'.encode("cp1250"), None).endswith("się")
    assert decode(b"\xff\x80 broken", "text/html; charset=nonsense") == "�� broken"


@pytest.mark.parametrize(
    "html",
    [
        '<a href="http://[evil/">x</a>',
        '<base href="http://[evil/"><a href="/x">x</a>',
        '<meta http-equiv="refresh" content="0;url=http://[evil/">',
        "<script>location.href='http://[evil/'</script>",
        '<form action="http://[evil/"><input type=password></form>',
        '<link rel="icon" href="http://[evil/">',
    ],
)
def test_malformed_urls_are_ignored(html: str) -> None:
    page = parse_page(html, "https://mintera-x.test/")
    assert all("[evil" not in link.url for link in page.links)


@pytest.mark.parametrize("label", ["idna", "undefined", "punycode", "rot13", "base64", "x-junk"])
def test_charset_labels_browsers_ignore(label: str) -> None:
    body = "<title>Mintera Bank Doğrula</title>".encode()
    assert decode(body, f"text/html; charset={label}") == "<title>Mintera Bank Doğrula</title>"
    assert decode(f'<meta charset="{label}">'.encode() + body, None).endswith("Doğrula</title>")


def test_a_meta_utf16_label_is_read_as_utf8_and_nul_goes() -> None:
    body = '<meta charset="utf-16"><title>Mintera Bank\x00 Doğrula</title>'.encode()
    text = decode(body, None)
    assert text.endswith("<title>Mintera Bank Doğrula</title>")
    assert parse_page(text, "https://x.test/").title == "Mintera Bank Doğrula"
    assert decode("\ufeffDoğrula".encode("utf-16"), "text/html; charset=latin-1") == "\ufeffDoğrula"


def test_unclosed_tags_parse_in_linear_time() -> None:
    started = time.monotonic()
    page = parse_page("<div><a href=/x>a" * 40_000, "https://x.test/")
    assert time.monotonic() - started < 3
    assert len(page.links) == 200


def test_an_implicitly_closed_head_keeps_the_body_text() -> None:
    page = parse_page(
        "<html><head><title>Mintera Bank Doğrula</title><body><h1>Mintera Bank güvenli oturum</h1>",
        "https://x.test/",
    )
    assert page.title == "Mintera Bank Doğrula"
    assert page.text == "Mintera Bank güvenli oturum"


def test_forms_are_capped_but_password_fields_are_counted() -> None:
    page = parse_page("<form><input>" * 300 + "<input type=password>", "https://x.test/")
    assert len(page.forms) == 50
    assert page.password_fields == 1
    page = parse_page(
        "<form>" + "<input>" * 300 + "<input type=password></form>", "https://x.test/"
    )
    assert sum(len(form.inputs) for form in page.forms) == 200
    assert page.password_fields == 1


def test_redirects_frames_and_buttons_read_like_a_browser() -> None:
    page = parse_page(
        '<base href="https://first.test/"><base href="https://second.test/">'
        '<meta http-equiv="refresh" content="3; https://mintera-new.test/">'
        '<frameset><frame src="https://mintera-alt.test/"></frameset>'
        '<iframe src="/embed"></iframe>'
        '<a href="javascript:void(0)" onclick="window.open(\'https://mintera-verify2.test/\')">'
        "Şimdi Doğrula</a>"
        '<a href="/\\mintera-backslash.test/">Refund</a>'
        '<a href="/unclosed">first<a href="/second">second</a>',
        "https://mintera-x.test/",
    )
    assert page.meta_refresh == "https://mintera-new.test/"
    assert page.frames == ["https://mintera-alt.test/"]
    assert page.iframes == ["https://first.test/embed"]
    urls = [link.url for link in page.links]
    assert "https://mintera-verify2.test/" in urls
    assert "https://mintera-backslash.test/" in urls
    assert "https://first.test/unclosed" in urls
    assert "https://first.test/second" in urls


# --- patterns ------------------------------------------------------------------------------------


def test_parking_and_challenge_patterns() -> None:
    assert patterns.parked_by_nameserver(["ns1.sedoparking.com.", "ns2.sedoparking.com"])
    assert not patterns.parked_by_nameserver(["ns1.mintera.com", "dns1.registrar-servers.com"])
    assert patterns.parked_by_text(parse_page(_page("parked/index.html"), "http://p.test/").text)
    assert not patterns.parked_by_text("Welcome to Mintera Bank")
    # Generic phrases count on short pages and in titles only, not in a bank's FAQ.
    faq = "Mintera Bank FAQ. " + "Deposits and withdrawals. " * 80 + "Why was my account suspended?"
    assert not patterns.parked_by_text(faq)
    assert patterns.parked_by_text("Account suspended. Contact your hosting provider.")
    assert patterns.parked_by_text(faq, title="Related searches")
    assert patterns.challenge(_page("challenge/challenge.html"), {})
    assert patterns.challenge("<html>hi</html>", {"cf-mitigated": "challenge"})
    assert not patterns.challenge(_page("clone/index.html"), {"server": "nginx"})
    # Cloudflare's JavaScript Detections script is on ordinary pages.
    jsd = '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
    assert not patterns.challenge(f"<html><body>{jsd}Mintera Bank</body></html>", {})


def test_captcha_widgets_count_only_on_a_wall() -> None:
    widget = '<div class="g-recaptcha" data-sitekey="x"></div>'
    assert patterns.captcha_wall(f"<body>{widget}</body>", "Verify you are human", 0)
    assert not patterns.captcha_wall(f"<form>{widget}</form>", "Mintera Bank login", 1)
    assert not patterns.captcha_wall(f"<body>{widget}</body>", "x" * 600, 0)


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Welcome to Mintera Bank", True),
        ("MINT ERA official login", True),
        ("m1nt3r4 login", True),  # leetspeak
        ("minter\N{CYRILLIC SMALL LETTER A}", True),  # look-alike letters
        ("Mint condition, an era of savings", False),
        ("", False),
    ],
)
def test_contains_brand(text: str, found: bool) -> None:
    assert contains_brand(text, ["mintera"]) is found


# --- routing ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("facts", "expected", "again"),
    [
        (Facts(Outcome.NO_DNS), Route.WATCH, WATCH_AGAIN),
        (Facts(Outcome.NO_CONNECTION), Route.WATCH, WATCH_AGAIN),
        (Facts(Outcome.BLOCKED), Route.WATCH, WATCH_AGAIN),
        (Facts(Outcome.OK, parked=True, brand_in_title=True), Route.WATCH, WATCH_AGAIN),
        # Parking text never hides a login form or the brand's icon.
        (Facts(Outcome.OK, parked=True, password_field=True), Route.CAPTURE, None),
        (Facts(Outcome.OK, parked=True, favicon_match=True), Route.CAPTURE, None),
        (Facts(Outcome.OK, empty=True), Route.WATCH, WATCH_AGAIN),
        # A near-empty doorway that sends visitors to a brand-like domain.
        (Facts(Outcome.OK, empty=True, brand_redirect="mintera-new.xyz"), Route.CAPTURE, None),
        (Facts(Outcome.OK, challenge=True), Route.CAPTURE, None),
        (Facts(Outcome.OK, challenge=True, empty=True), Route.CAPTURE, None),
        (Facts(Outcome.OK, favicon_match=True), Route.CAPTURE, None),
        (Facts(Outcome.OK, brand_in_title=True), Route.CAPTURE, None),
        (Facts(Outcome.OK, brand_in_text=True), Route.CAPTURE, None),
        (Facts(Outcome.OK, password_field=True), Route.CAPTURE, None),
        (Facts(Outcome.OK, brand_redirect="mintera-2.xyz"), Route.CAPTURE, None),
        # A redirect to the official site is watched, not trusted, and checked the next day.
        (
            Facts(Outcome.OK, redirects_to_official=True, brand_in_title=True),
            Route.WATCH,
            REDIRECT_AGAIN,
        ),
        (Facts(Outcome.OK, match_score=90, domain_age_days=3), Route.CAPTURE, None),
        (Facts(Outcome.OK, match_score=90, domain_age_days=300), Route.WATCH, WATCH_AGAIN),
        (Facts(Outcome.OK, match_score=70, domain_age_days=3), Route.WATCH, WATCH_AGAIN),
        (Facts(Outcome.OK, match_score=95, domain_age_days=None), Route.WATCH, WATCH_AGAIN),
        (Facts(Outcome.OK), Route.WATCH, WATCH_AGAIN),
    ],
)
def test_route(facts: Facts, expected: Route, again: timedelta | None) -> None:
    decision = route(facts)
    assert decision.route is expected
    assert decision.next_check == again
    assert decision.reason


def test_links_are_classified() -> None:
    brand = BrandProfile(uuid.UUID(int=1), uuid.UUID(int=2), "Mintera Bank", ("mintera",))
    matcher = Matcher([brand])
    page = parse_page(_page("lure/index.html"), "http://mintera-verify.test/")
    links = classify_links(page.links, "mintera-verify.test", ["mintera.test"], matcher)
    assert {target.host: target.kind for target in links} == {
        "mintera-login.test": "candidate",
        "quick-loans.example": "other",
        "www.facebook.com": "other",
    }
    # The lure page's buttons are calls to action; its share link is not.
    actions = {target.host for target in links if is_call_to_action(target, ["mintera"])}
    assert actions == {"mintera-login.test", "quick-loans.example"}
    review = parse_page(_page("affiliate/index.html"), "http://mintera-review.test/")
    assert [
        t.kind
        for t in classify_links(review.links, "mintera-review.test", ["mintera.test"], matcher)
    ] == ["official"]
    # www. and other subdomains of the case's own domain are the same site.
    own = classify_links(
        [LinkInfo("https://www.mintera-x.test/login", "Login", "a")], "mintera-x.test", [], matcher
    )
    assert own[0].kind == "same_site"


def test_own_host_names_are_not_the_brand_on_the_page() -> None:
    title = "mintera-x.com | 522: Connection timed out"
    assert contains_brand(title, ["mintera"])
    assert not contains_brand(without_hosts(title, ["mintera-x.com"]), ["mintera"])
    assert contains_brand(
        without_hosts("Welcome to www.mintera-x.com - Mintera Bank login", ["mintera-x.com"]),
        ["mintera"],
    )


def test_request_headers_and_entry_urls() -> None:
    assert request_headers(["pl"])["Accept-Language"].startswith("pl-PL")
    assert request_headers([])["Accept-Language"].startswith("en-US")
    assert "Chrome/" in request_headers(["TR"])["User-Agent"]
    # Discovery drops www., so a site that only answers there is tried last.
    assert entry_urls("mintera-login.test", None) == [
        "https://mintera-login.test/",
        "http://mintera-login.test/",
        "https://www.mintera-login.test/",
        "http://www.mintera-login.test/",
    ]
    assert entry_urls("login.mintera-x.test", None) == [
        "https://login.mintera-x.test/",
        "http://login.mintera-x.test/",
    ]
    for manual, first in [
        ("https://mintera-login.test/signin?x=1#top", "https://mintera-login.test/signin?x=1"),
        ("https://www.mintera-login.test/signin", "https://www.mintera-login.test/signin"),
        ("mintera-login.test/wp/login.php", "http://mintera-login.test/wp/login.php"),
        ("http://user:pw@mintera-login.test/x", "http://mintera-login.test/x"),
        ("https://other.example/", "https://mintera-login.test/"),
        ("http://[evil/", "https://mintera-login.test/"),
    ]:
        assert entry_urls("mintera-login.test", manual)[0] == first
