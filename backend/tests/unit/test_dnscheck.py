"""DNS check of permutations, without network (architecture.md §8)."""

from __future__ import annotations

import asyncio

import dns.exception
import dns.resolver
import pytest

from kalkan.discovery.dnscheck import DnsResolver, RateLimiter, resolve_all


class FakeResolver:
    def __init__(self, answers: dict[str, bool | None]) -> None:
        self.answers = answers
        self.active = 0
        self.peak = 0

    async def registered(self, domain: str) -> bool | None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.001)
        self.active -= 1
        if domain == "boom.com":
            raise RuntimeError("resolver crashed")
        return self.answers.get(domain, False)


def test_resolve_all_limits_concurrency_and_survives_errors() -> None:
    domains = [f"d{i}.com" for i in range(60)] + ["boom.com"]
    resolver = FakeResolver({"d1.com": True, "d2.com": None})
    results = asyncio.run(resolve_all(domains, resolver, concurrency=5, rate=10_000))
    assert resolver.peak <= 5
    assert results["d1.com"] is True
    assert results["d2.com"] is None
    assert results["d3.com"] is False
    assert results["boom.com"] is None


def test_rate_limiter_spaces_calls() -> None:
    clock = [0.0]
    sleeps: list[float] = []

    async def run() -> None:
        limiter = RateLimiter(10, clock=lambda: clock[0])
        original = asyncio.sleep

        async def fake_sleep(delay: float) -> None:
            sleeps.append(round(delay, 3))
            await original(0)

        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            for _ in range(3):
                await limiter.wait()
        finally:
            asyncio.sleep = original

    asyncio.run(run())
    assert sleeps == [0.1, 0.2]


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ({"NS": "answer"}, True),
        ({"NS": dns.resolver.NoAnswer(), "A": "answer"}, True),
        ({"NS": dns.resolver.NXDOMAIN()}, False),
        ({"NS": dns.resolver.NoAnswer(), "A": dns.resolver.NoAnswer()}, False),
        ({"NS": dns.exception.Timeout(), "A": dns.resolver.NoAnswer()}, None),
        ({"NS": dns.resolver.NoNameservers(), "A": "answer"}, True),
    ],
)
def test_dns_resolver_outcomes(
    monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, object], expected: bool | None
) -> None:
    resolver = DnsResolver()

    async def resolve(domain: str, rdtype: str) -> str:
        outcome = outcomes[rdtype]
        if isinstance(outcome, Exception):
            raise outcome
        return str(outcome)

    monkeypatch.setattr(resolver._resolver, "resolve", resolve)
    assert asyncio.run(resolver.registered("minteraa.com")) is expected
