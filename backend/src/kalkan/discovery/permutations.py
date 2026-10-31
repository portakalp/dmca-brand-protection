"""dnstwist permutations of official domains (architecture.md §8).

Generation is deterministic: each domain is credited to the first fuzzer, in ``FUZZERS``
order, that produces it, and the result is sorted. The official domain is split with the
repository's Public Suffix List, not dnstwist's short built-in list, so ``mintera.waw.pl``
permutes ``mintera``; a subdomain of an official host is dropped (``login.mintera.com``
permutes ``mintera.com``). ``tld-swap`` uses generic TLDs plus the
brand's target markets; the dictionary adds market words to the common ones (K59).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import dnstwist
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kalkan.db.base import new_id
from kalkan.db.models import Brand, BrandPermutation, Customer, OfficialDomain
from kalkan.discovery.normalize import to_unicode
from kalkan.discovery.psl import split_host
from kalkan.domain.enums import CustomerStatus
from kalkan.domain.hostnames import InvalidHostnameError, normalize_hostname

FUZZERS = (
    "addition",
    "bitsquatting",
    "homoglyph",
    "hyphenation",
    "insertion",
    "omission",
    "repetition",
    "replacement",
    "subdomain",
    "transposition",
    "vowel-swap",
    "dictionary",
    "tld-swap",
)
DICTIONARY = (
    "login", "signin", "secure", "verify", "account", "app", "pay", "online", "promo",
    "support", "zaloguj", "vhod",
)  # fmt: skip
MARKET_WORDS: dict[str, tuple[str, ...]] = {
    "TR": ("hesap", "sube", "dogrula", "mobil", "musteri"),
    "PL": ("bank", "konto", "logowanie", "bankowosc"),
    "RU": ("bank", "vhod", "kabinet"),
    "KZ": ("bank", "vhod", "kabinet"),
    "BY": ("bank", "vhod", "kabinet"),
    "DE": ("anmelden", "konto", "banking"),
}
GENERIC_TLDS = (
    "com", "net", "org", "xyz", "top", "online", "site", "live", "info", "support", "help",
    "click", "link", "club", "shop", "icu", "pro", "app",
)  # fmt: skip
MARKET_TLDS: dict[str, tuple[str, ...]] = {
    "TR": ("com.tr",),
    "PL": ("pl", "com.pl"),
    "RU": ("ru",),
    "KZ": ("kz",),
    "BY": ("by",),
    "DE": ("de",),
}
INSERT_BATCH = 1_000


@dataclass(frozen=True)
class Permutation:
    domain: str
    fuzzer: str


def dictionary_for(markets: Iterable[str]) -> list[str]:
    words = list(DICTIONARY)
    for market in markets:
        words += [word for word in MARKET_WORDS.get(market.upper(), ()) if word not in words]
    return words


def tlds_for(markets: Iterable[str]) -> list[str]:
    tlds = list(GENERIC_TLDS)
    for market in markets:
        tlds += [tld for tld in MARKET_TLDS.get(market.upper(), ()) if tld not in tlds]
    return tlds


def generate(domain: str, markets: Sequence[str] = ()) -> list[Permutation]:
    """Permutations of the registered domain of ``domain`` (punycode), sorted by name."""
    _, label, suffix = split_host(domain)
    if not label or not suffix:
        return []
    registered = f"{label}.{suffix}"
    found: dict[str, str] = {}
    for name in FUZZERS:
        fuzzer = dnstwist.Fuzzer(
            registered, dictionary=dictionary_for(markets), tld_dictionary=tlds_for(markets)
        )
        # dnstwist permutes the Unicode form of the label, as its own constructor does.
        fuzzer.subdomain, fuzzer.domain, fuzzer.tld = "", to_unicode(label), suffix
        fuzzer.generate(fuzzers=[name])
        for permutation in fuzzer.domains:
            try:
                host = normalize_hostname(permutation["domain"])
            except InvalidHostnameError:
                continue
            found.setdefault(host, name)
    found.pop(domain, None)
    found.pop(registered, None)
    return [Permutation(host, found[host]) for host in sorted(found)]


@dataclass(frozen=True)
class RefreshResult:
    added: int
    removed: int
    total: int


def refresh_brand_permutations(session: Session, brand: Brand) -> RefreshResult:
    """Bring ``brand_permutations`` of ``brand`` in line with its official domains.

    The brand row is locked first, so two refreshes (a new official domain and the weekly
    job) run one after the other and the later one sees the other's official domains.
    """
    session.execute(select(Brand.id).where(Brand.id == brand.id).with_for_update())
    officials = sorted(
        session.scalars(select(OfficialDomain.domain).where(OfficialDomain.brand_id == brand.id))
    )
    wanted: dict[str, str] = {}
    for official in officials:
        for permutation in generate(official, brand.target_markets):
            wanted.setdefault(permutation.domain, permutation.fuzzer)
    for official in officials:
        wanted.pop(official, None)
        _, label, suffix = split_host(official)
        wanted.pop(f"{label}.{suffix}", None)

    existing = set(
        session.scalars(
            select(BrandPermutation.domain).where(BrandPermutation.brand_id == brand.id)
        )
    )
    stale = existing - wanted.keys()
    if stale:
        session.execute(
            delete(BrandPermutation).where(
                BrandPermutation.brand_id == brand.id, BrandPermutation.domain.in_(stale)
            )
        )
    rows = [
        {
            "id": new_id(),
            "customer_id": brand.customer_id,
            "brand_id": brand.id,
            "domain": domain,
            "fuzzer": fuzzer,
        }
        for domain, fuzzer in sorted(wanted.items())
        if domain not in existing
    ]
    for start in range(0, len(rows), INSERT_BATCH):
        session.execute(
            insert(BrandPermutation)
            .values(rows[start : start + INSERT_BATCH])
            .on_conflict_do_nothing(
                index_elements=[BrandPermutation.brand_id, BrandPermutation.domain]
            )
        )
    return RefreshResult(added=len(rows), removed=len(stale), total=len(wanted))


def watched_brands(session: Session) -> list[uuid.UUID]:
    """Brands whose permutations are generated and checked; the same customers the matcher
    watches: active brands, not demo (K46), not suspended."""
    return list(
        session.scalars(
            select(Brand.id)
            .join(Customer, Customer.id == Brand.customer_id)
            .where(
                Brand.active.is_(True),
                Customer.is_demo.is_(False),
                Customer.status != CustomerStatus.SUSPENDED,
            )
            .order_by(Brand.id)
        )
    )
