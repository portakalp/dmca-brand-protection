"""Whitelist rules (architecture.md §9)."""

from __future__ import annotations

import uuid

import pytest

from kalkan.domain.hostnames import InvalidHostnameError
from kalkan.enrichment.whitelist import WhitelistRule, covers, find_rule, validate_pattern

BRAND = uuid.UUID(int=1)
OTHER_BRAND = uuid.UUID(int=2)


@pytest.mark.parametrize(
    ("pattern", "host", "covered"),
    [
        ("mintera.pl", "mintera.pl", True),
        ("mintera.pl", "www.mintera.pl", True),
        ("mintera.pl", "login.m.mintera.pl", True),
        ("mintera.pl", "mintera.pl.evil.com", False),
        ("mintera.pl", "evilmintera.pl", False),
        ("mintera.pl", "mintera.pl.", False),  # hosts are normalized before matching
        ("partners.mintera-referrals.com", "mintera-referrals.com", False),
        ("partners.mintera-referrals.com", "x.partners.mintera-referrals.com", True),
        ("mintera-referrals.com", "partners.mintera-referrals.com", True),
        # s3.amazonaws.com is a shared platform root: the host is someone else's domain.
        ("amazonaws.com", "evil.s3.amazonaws.com", False),
        ("amazonaws.com", "console.amazonaws.com", True),
    ],
)
def test_covers(pattern: str, host: str, covered: bool) -> None:
    assert covers(pattern, host) is covered


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ("Mintera.PL", "mintera.pl"),
        ("www.mintera.pl", "mintera.pl"),
        ("partners.mintera-referrals.com", "partners.mintera-referrals.com"),
        ("mintera.github.io", "mintera.github.io"),
        ("minter\N{CYRILLIC SMALL LETTER A}.com", "xn--minter-8nf.com"),
    ],
)
def test_valid_patterns(value: str, pattern: str) -> None:
    assert validate_pattern(value) == pattern


@pytest.mark.parametrize(
    "value",
    ["com.pl", "co.uk", "github.io", "pages.dev", "vercel.app", "blogspot.com", "www.com.pl"],
)
def test_suffixes_and_shared_platform_roots_are_refused(value: str) -> None:
    with pytest.raises(InvalidHostnameError, match="public suffix or a shared platform root"):
        validate_pattern(value)


@pytest.mark.parametrize("value", ["https://mintera.pl/", "*.mintera.pl", "203.0.113.7", ""])
def test_non_hosts_are_refused(value: str) -> None:
    with pytest.raises(InvalidHostnameError):
        validate_pattern(value)


def test_rules_are_scoped_to_the_brand_and_official_domains_win() -> None:
    rules = [
        WhitelistRule("mintera.pl", OTHER_BRAND),
        WhitelistRule("reviews.example.org", None),
        WhitelistRule("mintera.com", BRAND),
        WhitelistRule("mintera.com", BRAND, official=True),
    ]
    assert find_rule("www.mintera.pl", BRAND, rules) is None  # another brand's entry
    assert find_rule("www.mintera.pl", OTHER_BRAND, rules) == rules[0]
    assert find_rule("x.reviews.example.org", BRAND, rules) == rules[1]  # every brand
    assert find_rule("login.mintera.com", BRAND, rules) == rules[3]
    assert find_rule("mintera.com.evil.xyz", BRAND, rules) is None
