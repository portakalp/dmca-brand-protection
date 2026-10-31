"""Matcher throughput: at least 2,000 domains per second on one core (architecture.md §8).

Run with ``make bench``; not part of ``make test``.
"""

from __future__ import annotations

import random
import time
import uuid

from kalkan.discovery.matcher import BrandProfile, Matcher
from kalkan.discovery.permutations import generate

TARGET_PER_SECOND = 2_000
DOMAINS = 20_000
WORDS = (
    "shop", "cloud", "mail", "login", "verify", "account", "card", "app", "secure", "news",
    "best", "online", "store", "media", "refund", "wallet", "promo", "pay", "support", "my",
)  # fmt: skip
TLDS = ("com", "net", "org", "xyz", "top", "online", "site", "com.pl", "co.uk", "io", "app")
BRANDS = (
    "mintera", "paylumo", "kredo", "finvora", "banqora", "crestpay", "novalend", "zentrik",
    "lumibank", "quorafin", "trelvo", "monexa", "kassovia", "verdanix", "pennyra", "orbitpay",
    "solvexa", "brightfund", "kapitalo", "walletra",
)  # fmt: skip


def _domains(rng: random.Random) -> list[str]:
    domains = []
    for index in range(DOMAINS):
        parts = [rng.choice(WORDS) for _ in range(rng.randint(1, 3))]
        if index % 50 == 0:  # about 2% carry a brand, like a noisy CT stream
            parts.insert(rng.randint(0, len(parts)), rng.choice(BRANDS))
        label = rng.choice(["-", ""]).join(parts) + str(rng.randint(0, 999))
        subdomain = rng.choice(["", "www.", "mail.", "api.", "cdn.login."])
        domains.append(f"{subdomain}{label}.{rng.choice(TLDS)}")
    return domains


def test_matcher_throughput() -> None:
    profiles = [
        BrandProfile(
            brand_id=uuid.UUID(int=index),
            customer_id=uuid.UUID(int=index),
            name=name.title(),
            keywords=(name,),
            permutations=frozenset(p.domain for p in generate(f"{name}.com", ["TR", "PL"])),
        )
        for index, name in enumerate(BRANDS, start=1)
    ]
    matcher = Matcher(profiles)
    domains = _domains(random.Random(3))

    started = time.perf_counter()
    matched = sum(1 for domain in domains if (found := matcher.match_host(domain)) and found[1])
    elapsed = time.perf_counter() - started
    rate = len(domains) / elapsed
    print(f"\nmatcher: {rate:,.0f} domains/s, {matched} matched of {len(domains)}")  # noqa: T201
    assert matched >= DOMAINS // 50
    assert rate >= TARGET_PER_SECOND
